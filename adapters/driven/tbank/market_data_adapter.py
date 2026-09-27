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
    TIMEFRAME_TO_INDICATOR_INTERVAL,
    TIMEFRAME_TO_SUBSCRIPTION_INTERVAL,
    candle_to_domain,
    datetime_to_proto_timestamp,
    instrument_list_to_catalog,
    instrument_short_to_catalog_entry,
    instrument_to_domain,
    orderbook_to_domain,
)
from adapters.driven.tbank.retry import retry_read
from core.domain.catalog import DEFAULT_CATALOG_TYPES, InstrumentCatalogEntry
from core.domain.entities import Instrument
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV, OrderbookSnapshot

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from adapters.driven.tbank.grpc_client import TInvestChannel

#: Имена enum-значений из текущего proto. Не подставляем голые числа:
#: в API ``BB=1``, ``SMA=5`` (раньше эти значения были перепутаны).
TECH_ANALYSIS_TYPES: dict[str, str] = {
    "sma": "INDICATOR_TYPE_SMA",
    "ema": "INDICATOR_TYPE_EMA",
    "rsi": "INDICATOR_TYPE_RSI",
    "macd": "INDICATOR_TYPE_MACD",
    "bollinger": "INDICATOR_TYPE_BB",
    "bb": "INDICATOR_TYPE_BB",
}

#: Методы ``InstrumentsService``, отдающие списки инструментов по типу.
#: Каждый метод ограничен 15 запросами в минуту — каталог обновляется пачкой
#: и сразу сохраняется в БД, а не читается из сети на каждый экран GUI.
CATALOG_LIST_METHODS: dict[str, str] = {
    "share": "shares",
    "etf": "etfs",
    "bond": "bonds",
    "currency": "currencies",
    "futures": "futures",
}

#: Имена enum-значений ``InstrumentType`` для фильтра ``FindInstrument``.
FIND_INSTRUMENT_KINDS: dict[str, str] = {
    "share": "INSTRUMENT_TYPE_SHARE",
    "etf": "INSTRUMENT_TYPE_ETF",
    "bond": "INSTRUMENT_TYPE_BOND",
    "currency": "INSTRUMENT_TYPE_CURRENCY",
    "futures": "INSTRUMENT_TYPE_FUTURES",
}


logger = structlog.get_logger(__name__)


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
        from t_tech.invest.grpc.schemas import CandleInterval, GetCandlesRequest

        request = GetCandlesRequest(
            instrument_id=instrument.uid,
            from_=datetime_to_proto_timestamp(from_),
            to=datetime_to_proto_timestamp(to),
            interval=getattr(CandleInterval, TIMEFRAME_TO_API_INTERVAL[timeframe]),
            limit=2400,
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
            MarketDataServerSideStreamRequest,
            SubscribeCandlesRequest,
            SubscriptionAction,
            SubscriptionInterval,
        )

        subscription = SubscribeCandlesRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[
                CandleInstrument(
                    instrument_id=instrument.uid,
                    interval=getattr(
                        SubscriptionInterval,
                        TIMEFRAME_TO_SUBSCRIPTION_INTERVAL[timeframe],
                    ),
                )
            ],
        )
        request = MarketDataServerSideStreamRequest(subscribe_candles_request=subscription)
        stream = self._channel.services.market_data_stream.market_data_server_side_stream(
            request=request
        )
        try:
            async for update in stream:
                candle = getattr(update, "candle", None)
                if candle is None or not getattr(candle, "is_complete", True):
                    continue
                yield candle_to_domain(candle, timeframe)
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
            else:
                cancel = getattr(stream, "cancel", None)
                if cancel is not None:
                    cancel()

    async def get_orderbook(self, instrument: Instrument, depth: int = 20) -> OrderbookSnapshot:
        from t_tech.invest.grpc.schemas import GetOrderBookRequest

        if depth not in {1, 10, 20, 30, 40, 50}:
            msg = "Глубина стакана API должна быть одной из: 1, 10, 20, 30, 40, 50"
            raise ValueError(msg)
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
    ) -> dict[str, Decimal | None]:
        """Индикатор из API без потери точности в Decimal."""
        from t_tech.invest.grpc.schemas import GetTechAnalysisRequest

        indicator_type_name = TECH_ANALYSIS_TYPES.get(indicator)
        if indicator_type_name is None:
            msg = (
                f"Индикатор {indicator} не предоставляется API. "
                "Собственные индикаторы считаются в core/analysis"
            )
            raise ValueError(msg)

        normalized = "bb" if indicator in {"bollinger", "bb"} else indicator
        start = _default_from(timeframe)
        end = datetime.now(tz=UTC)
        request_fields: dict[str, Any] = {
            "instrument_id": instrument.uid,
            "from_": datetime_to_proto_timestamp(start),
            "to": datetime_to_proto_timestamp(end),
            "interval": getattr(
                GetTechAnalysisRequest.IndicatorInterval,
                TIMEFRAME_TO_INDICATOR_INTERVAL[timeframe],
            ),
            "indicator_type": getattr(GetTechAnalysisRequest.IndicatorType, indicator_type_name),
            "type_of_price": GetTechAnalysisRequest.TypeOfPrice.TYPE_OF_PRICE_CLOSE,
        }
        period = int(
            params.get("period", {"sma": 200, "ema": 50, "rsi": 14, "bb": 20}.get(normalized, 14))
        )
        if normalized != "macd":
            request_fields["length"] = period
        if normalized == "bb":
            from t_tech.invest.utils import decimal_to_quotation

            deviation = Decimal(str(params.get("deviation", "2")))
            request_fields["deviation"] = GetTechAnalysisRequest.Deviation(
                deviation_multiplier=decimal_to_quotation(deviation)
            )
        elif normalized == "macd":
            request_fields["smoothing"] = GetTechAnalysisRequest.Smoothing(
                fast_length=int(params.get("fast", 12)),
                slow_length=int(params.get("slow", 26)),
                signal_smoothing=int(params.get("signal", 9)),
            )

        request = GetTechAnalysisRequest(**request_fields)
        response = await retry_read(
            lambda: self._market_data.get_tech_analysis(request=request),
            operation_name=f"get_tech_analysis:{indicator}",
        )
        return _parse_tech_analysis(response, normalized)

    async def resolve_instrument(self, ticker: str, class_code: str) -> Instrument:
        """Однозначно разрешает тикер через InstrumentsService API.

        Не подменяем ошибку сети локальной записью каталога: sandbox-проверка
        должна подтверждать, что инструмент действительно найден API.
        """
        from t_tech.invest.grpc.schemas import InstrumentIdType, InstrumentRequest

        request = InstrumentRequest(
            id=ticker,
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            class_code=class_code,
        )
        response = await retry_read(
            lambda: self._channel.services.instruments.get_instrument_by(request=request),
            operation_name="resolve_instrument",
        )
        instrument = getattr(response, "instrument", None)
        if instrument is None:
            msg = f"T-Invest API не нашёл инструмент {ticker}.{class_code}"
            raise ValueError(msg)
        return instrument_to_domain(instrument)

    async def fetch_catalog(
        self, instrument_types: Sequence[str] | None = None
    ) -> list[InstrumentCatalogEntry]:
        """Справочник инструментов из ``InstrumentsService``.

        Списки (Shares/Etfs/Currencies/Futures/Bonds) ограничены 15 запросами в
        минуту, поэтому один refresh — это один запрос на тип, а результат
        сохраняется в БД: GUI и бэктест читают каталог из хранилища.
        """
        from t_tech.invest.grpc.schemas import InstrumentsRequest, InstrumentStatus

        requested = [item.strip().lower() for item in (instrument_types or ()) if item.strip()]
        unknown = sorted({item for item in requested if item not in CATALOG_LIST_METHODS})
        if unknown:
            msg = (
                "Неизвестные типы инструментов каталога: "
                f"{', '.join(unknown)}. Допустимо: {', '.join(sorted(CATALOG_LIST_METHODS))}"
            )
            raise ValueError(msg)

        entries: list[InstrumentCatalogEntry] = []
        services = self._channel.services.instruments
        request = InstrumentsRequest(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE)
        types = requested or list(DEFAULT_CATALOG_TYPES)
        for instrument_type in types:
            method = getattr(services, CATALOG_LIST_METHODS[instrument_type])
            response = await self._fetch_instrument_list(
                method, request, operation_name=f"catalog:{instrument_type}"
            )
            entries.extend(
                instrument_list_to_catalog(
                    getattr(response, "instruments", None) or [], instrument_type
                )
            )
        logger.info("catalog_fetched", types=types, entries=len(entries))
        return entries

    async def _fetch_instrument_list(
        self, method: Any, request: Any, *, operation_name: str
    ) -> Any:
        """Один read-only список инструментов с повтором."""
        return await retry_read(lambda: method(request=request), operation_name=operation_name)

    async def search_instruments(
        self,
        query: str,
        *,
        instrument_type: str | None = None,
        limit: int = 20,
    ) -> list[InstrumentCatalogEntry]:
        """Поиск инструмента через ``InstrumentsService/FindInstrument``."""
        from t_tech.invest.grpc.schemas import FindInstrumentRequest, InstrumentType

        needle = query.strip()
        if len(needle) < 2:
            return []
        if instrument_type:
            enum_name = FIND_INSTRUMENT_KINDS.get(instrument_type.strip().lower())
            if enum_name is None:
                msg = (
                    f"Неизвестный тип инструмента для поиска: {instrument_type}. "
                    f"Допустимо: {', '.join(sorted(FIND_INSTRUMENT_KINDS))}"
                )
                raise ValueError(msg)
            kind = getattr(InstrumentType, enum_name)
        else:
            kind = InstrumentType.INSTRUMENT_TYPE_UNSPECIFIED

        request = FindInstrumentRequest(
            query=needle,
            instrument_kind=kind,
            api_trade_available_flag=True,
        )
        response = await retry_read(
            lambda: self._channel.services.instruments.find_instrument(request=request),
            operation_name="find_instrument",
        )
        raw_items = getattr(response, "instruments", None) or []
        entries: list[InstrumentCatalogEntry] = []
        for item in raw_items:
            if not getattr(item, "uid", None):
                continue
            entries.append(instrument_short_to_catalog_entry(item))
        return entries[: max(limit, 1)]

    async def aclose(self) -> None:
        await self._channel.aclose()


def _default_from(timeframe: Timeframe) -> datetime:
    """Окно истории по умолчанию для запроса индикатора."""
    from datetime import timedelta

    windows = {Timeframe.D1: 365, Timeframe.H1: 30, Timeframe.M1: 1}
    return datetime.now(tz=UTC) - timedelta(days=windows[timeframe])


def _parse_tech_analysis(response: Any, indicator: str) -> dict[str, Decimal | None]:
    """Извлекает последние значения из реальной схемы GetTechAnalysisResponse."""
    items = list(
        getattr(response, "technical_indicators", None)
        or getattr(response, "technical_analysis", [])
        or []
    )
    if not items:
        return dict.fromkeys(_output_names(indicator))

    last = items[-1]
    if indicator in {"bollinger", "bb"}:
        return {
            "bb_lower": _optional_quotation(last, "lower_band", "bb_lower_band"),
            "bb_middle": _optional_quotation(last, "middle_band", "bb_middle_band"),
            "bb_upper": _optional_quotation(last, "upper_band", "bb_upper_band"),
        }
    if indicator == "macd":
        macd = _optional_quotation(last, "macd")
        signal = _optional_quotation(last, "signal")
        return {
            "macd": macd,
            "signal": signal,
            "histogram": macd - signal if macd is not None and signal is not None else None,
        }
    field = {
        "sma": ("middle_band", "sma"),
        "ema": ("middle_band", "ema"),
        "rsi": ("signal", "rsi"),
    }[indicator]
    return {indicator: _optional_quotation(last, *field)}


def _optional_quotation(item: Any, *field_names: str) -> Decimal | None:
    from adapters.driven.tbank.mappers import quotation_to_decimal

    for field_name in field_names:
        value = getattr(item, field_name, None)
        if value is not None:
            return quotation_to_decimal(value)
    return None


def _output_names(indicator: str) -> tuple[str, ...]:
    if indicator in {"bollinger", "bb"}:
        return ("bb_lower", "bb_middle", "bb_upper")
    if indicator == "macd":
        return ("macd", "signal", "histogram")
    return (indicator,)
