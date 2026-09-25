"""Адаптер рыночных данных T-Invest (``MarketDataPort``).

Использует исторические свечи, стакан и технический анализ из API —
SMA/EMA/RSI/MACD/Bollinger **не дублируются** в ``core``, а запрашиваются
здесь. Собственные индикаторы (ATR, OBV, VWAP, Фибоначчи, корреляция с IMOEX)
считаются в ``core``.

Идентификация инструмента — по ``instrument_uid``: FIGI объявлен устаревшим.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from adapters.driven.tbank.mappers import (
    TIMEFRAME_TO_API_INTERVAL,
    candle_to_domain,
    datetime_to_proto_timestamp,
    instrument_to_domain,
    orderbook_to_domain,
)
from adapters.driven.tbank.retry import retry_read
from core.domain.entities import Instrument
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV, OrderbookSnapshot

if TYPE_CHECKING:
    from collections.abc import Mapping

    from adapters.driven.tbank.grpc_client import TInvestChannel

logger = structlog.get_logger(__name__)

#: Соответствие имён индикаторов кодам ``TypeOfTechnicalAnalysis``.
TECH_ANALYSIS_TYPES: dict[str, int] = {
    "sma": 1,
    "ema": 2,
    "rsi": 3,
    "macd": 4,
    "bollinger": 5,
    "bb": 5,
}

#: Поля ответа тех. анализа по имени индикатора.
TECH_ANALYSIS_FIELDS: dict[str, tuple[str, ...]] = {
    "sma": ("sma",),
    "ema": ("ema",),
    "rsi": ("rsi",),
    "macd": ("macd", "signal", "histogram"),
    "bollinger": ("bb_upper_band", "bb_middle_band", "bb_lower_band"),
    "bb": ("bb_upper_band", "bb_middle_band", "bb_lower_band"),
}


class TBankMarketDataAdapter:
    """Реализация ``MarketDataPort`` поверх T-Invest API."""

    def __init__(self, channel: TInvestChannel) -> None:
        self._channel = channel

    @property
    def _market_data(self) -> Any:
        return self._channel.services.market_data

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        from_: datetime,
        to: datetime,
    ) -> list[OHLCV]:
        """Исторические свечи. Всегда tz-aware UTC."""
        from t_tech.invest.grpc.schemas import GetCandlesRequest  # pyright: ignore

        request = GetCandlesRequest(
            instrument_id=instrument.uid,
            from_=datetime_to_proto_timestamp(from_),
            to=datetime_to_proto_timestamp(to),
            interval=TIMEFRAME_TO_API_INTERVAL[timeframe],
        )
        response = await retry_read(
            lambda: self._market_data.get_candles(request=request),
            operation_name="get_candles",
        )
        return [candle_to_domain(c, timeframe) for c in response.candles]

    async def stream_candles(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
    ) -> AsyncIterator[OHLCV]:
        """Поток свечей. Переподписка при обрыве — в ``stream_manager``."""
        from t_tech.invest.grpc.schemas import (
            CandleInstrument,
            SubscribeCandlesRequest,
            SubscriptionAction,
        )

        request = SubscribeCandlesRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[
                CandleInstrument(
                    instrument_id=instrument.uid,
                    interval=TIMEFRAME_TO_API_INTERVAL[timeframe],
                )
            ],
        )
        async for update in self._market_data.subscribe_candles(request=request):
            candle = getattr(update, "candle", None)
            if candle is None or not getattr(candle, "is_complete", True):
                continue
            yield candle_to_domain(candle, timeframe)

    async def get_orderbook(self, instrument: Instrument, depth: int = 20) -> OrderbookSnapshot:
        from t_tech.invest.grpc.schemas import GetOrderBookRequest

        request = GetOrderBookRequest(instrument_id=instrument.uid, depth=depth)
        response = await retry_read(
            lambda: self._market_data.get_order_book(request=request),
            operation_name="get_order_book",
        )
        return orderbook_to_domain(response)

    async def get_api_indicator(
        self,
        instrument: Instrument,
        indicator: str,
        timeframe: Timeframe,
        params: Mapping[str, Any],
    ) -> dict[str, float | None]:
        """Индикатор из API: SMA / EMA / RSI / MACD / Bollinger."""
        from t_tech.invest.grpc.schemas import GetTechAnalysisRequest

        indicator_type = TECH_ANALYSIS_TYPES.get(indicator)
        if indicator_type is None:
            msg = (
                f"Индикатор {indicator} не предоставляется API. "
                "Собственные индикаторы считаются в core/analysis"
            )
            raise ValueError(msg)

        start = _default_from(timeframe)
        end = datetime.now(tz=start.tzinfo)
        request = GetTechAnalysisRequest(
            instrument_id=instrument.uid,
            from_=datetime_to_proto_timestamp(start),
            to=datetime_to_proto_timestamp(end),
            interval=TIMEFRAME_TO_API_INTERVAL[timeframe],
            indicator_type=indicator_type,
            **{k: int(v) for k, v in params.items() if isinstance(v, (int, float))},
        )
        response = await retry_read(
            lambda: self._market_data.get_tech_analysis(request=request),
            operation_name=f"get_tech_analysis:{indicator}",
        )
        return _parse_tech_analysis(response, indicator)

    async def resolve_instrument(self, ticker: str, class_code: str) -> Instrument:
        """Однозначно находит инструмент по тикеру и класс-коду."""
        from t_tech.invest.grpc.schemas import InstrumentIdType, InstrumentRequest

        request = InstrumentRequest(
            id=f"{ticker}_{class_code}",
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            class_code=class_code,
        )
        response = await retry_read(
            lambda: self._channel.services.instruments.get_instrument_by(request=request),
            operation_name="resolve_instrument",
        )
        instrument = response.instrument
        if not instrument:
            msg = f"Инструмент {ticker}.{class_code} не найден"
            raise ValueError(msg)
        return instrument_to_domain(instrument)

    async def aclose(self) -> None:
        await self._channel.aclose()


def _default_from(timeframe: Timeframe) -> datetime:
    """Окно истории по умолчанию для запроса индикатора."""
    from datetime import timedelta

    windows = {Timeframe.D1: 365, Timeframe.H1: 30, Timeframe.M1: 1}
    return datetime.now(tz=UTC) - timedelta(days=windows[timeframe])


def _parse_tech_analysis(response: Any, indicator: str) -> dict[str, float | None]:
    """Извлекает последнее значение индикатора из ответа тех. анализа."""
    fields = TECH_ANALYSIS_FIELDS.get(indicator, (indicator,))
    items = list(getattr(response, "technical_analysis", []) or [])
    if not items:
        return dict.fromkeys(_output_names(indicator))

    last = items[-1]
    result: dict[str, float | None] = {}
    for field in fields:
        value = getattr(last, field, None)
        if value is None:
            result[field] = None
            continue
        result[field] = float(_quotation(value))
    return _normalize_names(result, indicator)


def _quotation(value: Any) -> Decimal:
    from adapters.driven.tbank.mappers import quotation_to_decimal

    return quotation_to_decimal(value)


def _output_names(indicator: str) -> tuple[str, ...]:
    if indicator in {"bollinger", "bb"}:
        return ("bb_lower", "bb_middle", "bb_upper")
    return (indicator,)


def _normalize_names(values: dict[str, float | None], indicator: str) -> dict[str, float | None]:
    """Приводит имена полей к тем, что ожидает ``core/strategy/setup_scanner``."""
    if indicator in {"bollinger", "bb"}:
        mapped = {
            "bb_lower": values.get("bb_lower_band"),
            "bb_middle": values.get("bb_middle_band"),
            "bb_upper": values.get("bb_upper_band"),
        }
        return mapped
    if indicator == "macd":
        return {
            "macd": values.get("macd"),
            "signal": values.get("signal"),
            "histogram": values.get("histogram"),
        }
    return values
