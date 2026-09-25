"""Адаптер-реплей: прогон стратегии по историческим данным.

Подменяет ``MarketDataPort``: вместо сети отдаёт свечи из локального
Parquet/DuckDB-хранилища, сдвинутые во времени на «текущий момент бэктеста»
(``FrozenClock``). Стратегия при этом **не знает**, что работает на истории, —
для неё это обычный порт.

Такой подход дает важное свойство: бэктест прогоняет ровно тот же код
принятия решения, что и бой. Расхождение «бэктест сказал хорошо, бой — плохо»
тогда объясняется рынком, а не разницей реализаций.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from core.domain.entities import Instrument
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV, OrderbookSnapshot

logger = structlog.get_logger(__name__)


class BacktestReplayAdapter:
    """Реплей исторических свечей из Parquet."""

    def __init__(
        self,
        *,
        data_dir: Path | str,
        clock: Any = None,
        orderbook: OrderbookSnapshot | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._orderbook = orderbook
        self._cache: dict[tuple[str, Timeframe], tuple[OHLCV, ...]] = {}

    def load_from_rows(
        self,
        instrument_uid: str,
        timeframe: Timeframe,
        rows: list[tuple[Any, ...]],
    ) -> None:
        """Загружает историю напрямую из строк БД (для тестов и small data).

        Формат строки: ``(ts, open, high, low, close, volume)``.
        """
        from decimal import Decimal

        candles = tuple(
            OHLCV(
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=int(row[5]),
                timestamp=row[0]
                if isinstance(row[0], datetime)
                else datetime.fromisoformat(str(row[0])),
                timeframe=timeframe,
            )
            for row in rows
        )
        self._cache[(instrument_uid, timeframe)] = candles

    def set_candles(
        self,
        instrument_uid: str,
        timeframe: Timeframe,
        candles: tuple[OHLCV, ...],
    ) -> None:
        """Задаёт историю напрямую (используется в тестах и бэктестах)."""
        self._cache[(instrument_uid, timeframe)] = candles

    def _parquet_path(self, instrument_uid: str, timeframe: Timeframe) -> Path:
        return self._data_dir / f"{instrument_uid}_{timeframe.value}.parquet"

    def _load(self, instrument_uid: str, timeframe: Timeframe) -> tuple[OHLCV, ...]:
        cached = self._cache.get((instrument_uid, timeframe))
        if cached is not None:
            return cached

        path = self._parquet_path(instrument_uid, timeframe)
        if not path.exists():
            return ()

        try:
            import duckdb
        except ImportError:  # pragma: no cover
            logger.warning("duckdb недоступен, реплей пуст")
            return ()

        rows = duckdb.sql(
            f"SELECT ts, open, high, low, close, volume FROM read_parquet('{path.as_posix()}') "
            "ORDER BY ts"
        ).fetchall()
        self.load_from_rows(instrument_uid, timeframe, rows)
        return self._cache[(instrument_uid, timeframe)]

    def _now(self) -> datetime:
        if self._clock is not None:
            moment: datetime = self._clock.now()
            return moment
        return datetime.now(tz=UTC)

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        from_: datetime,
        to: datetime,
    ) -> list[OHLCV]:
        """Свечи из истории. ``to`` обрезается по «текущему» моменту."""
        all_candles = self._load(instrument.uid, timeframe)
        upper = min(to, self._now())
        return [c for c in all_candles if from_ <= c.timestamp <= upper]

    async def stream_candles(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
    ) -> AsyncIterator[OHLCV]:
        """«Поток»: последовательная выдача исторических свечей."""
        for candle in self._load(instrument.uid, timeframe):
            yield candle

    async def get_orderbook(self, instrument: Instrument, depth: int = 20) -> OrderbookSnapshot:
        if self._orderbook is not None:
            return self._orderbook
        msg = "Стакан недоступен в режиме бэктеста: задайте его явно при создании адаптера"
        raise NotImplementedError(msg)

    async def get_api_indicator(
        self,
        instrument: Instrument,
        indicator: str,
        timeframe: Timeframe,
        params: Mapping[str, Any],
    ) -> dict[str, float | None]:
        """Индикатор считается локально — в бэктесте API недоступен."""
        from core.analysis.atr import ATRIndicator
        from core.domain.value_objects import CandleSeries

        series = CandleSeries(
            timeframe=timeframe,
            candles=self._load(instrument.uid, timeframe),
        )
        if not series.candles:
            return {}

        if indicator == "atr":
            try:
                result = ATRIndicator(period=int(params.get("period", 14))).calculate(series)
            except ValueError:
                return {}
            return {"atr": float(result.value)}

        msg = (
            f"Индикатор {indicator} недоступен в режиме бэктеста: "
            "поддерживается только ATR. Остальные индикаторы предоставляет API."
        )
        raise NotImplementedError(msg)

    async def resolve_instrument(self, ticker: str, class_code: str) -> Instrument:
        msg = (
            "resolve_instrument недоступен в бэктесте: "
            "инструменты задаются явно при сборке контекста"
        )
        raise NotImplementedError(msg)

    def set_clock(self, clock: Any) -> None:
        self._clock = clock

    async def aclose(self) -> None:
        self._cache.clear()


def warm_retention_cutoff(now: datetime, days: int) -> datetime:
    """Граница тёплого слоя — для агрегации свечей."""
    return now - timedelta(days=days)
