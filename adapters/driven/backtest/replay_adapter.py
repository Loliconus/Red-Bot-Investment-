"""Адаптер-реплей: прогон стратегии по историческим данным.

Подменяет ``MarketDataPort``: вместо сети отдаёт свечи из локального
Parquet/DuckDB-хранилища, сдвинутые во времени на «текущий момент бэктеста»
(``FrozenClock``). Стратегия при этом **не знает**, что работает на истории, —
для неё это обычный порт.

Такой подход дает важное свойство: бэктест прогоняет ровно тот же код
принятия решения, что и бой. Расхождение «бэктест сказал хорошо, бой — плохо»
тогда объясняется рынком, а не разницей реализаций.

Справочник инструментов в бэктесте тоже берётся из хранилища: каталог
загружается из API в контуре live/sandbox и сохраняется в БД, поэтому здесь
достаточно прочитать его же — хардкод тикеров, лотов и UID не нужен.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from core.domain.catalog import CatalogUnavailableError, InstrumentCatalogEntry
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
        repository: Any = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._orderbook = orderbook
        self._repository = repository
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
    ) -> dict[str, Decimal | None]:
        """Индикатор считается локально по закрытым к ``_now()`` свечам (без утечки будущего)."""
        import math

        from core.analysis.atr import ATRIndicator
        from core.domain.value_objects import CandleSeries

        upper = self._now()
        visible_candles = tuple(
            c for c in self._load(instrument.uid, timeframe) if c.timestamp <= upper
        )
        series = CandleSeries(
            timeframe=timeframe,
            candles=visible_candles,
        )
        if not series.candles:
            return {}

        norm_ind = indicator.strip().lower()
        if norm_ind == "atr":
            try:
                result = ATRIndicator(period=int(params.get("period", 14))).calculate(series)
            except ValueError:
                return {}
            return {"atr": result.value}

        closes = [c.close for c in visible_candles]
        period = max(1, int(params.get("period", 14)))

        if norm_ind == "sma":
            window = closes[-period:]
            val = (sum(window) / Decimal(len(window))).quantize(Decimal("0.0001"))
            return {"sma": val}

        if norm_ind == "ema":
            alpha = Decimal("2") / Decimal(period + 1)
            ema_val = closes[0]
            for c_val in closes[1:]:
                ema_val = alpha * c_val + (Decimal("1") - alpha) * ema_val
            return {"ema": ema_val.quantize(Decimal("0.0001"))}

        if norm_ind == "rsi":
            if len(closes) < 2:
                return {"rsi": Decimal("50")}
            gains: list[Decimal] = []
            losses: list[Decimal] = []
            for i in range(1, len(closes)):
                diff = closes[i] - closes[i - 1]
                gains.append(max(diff, Decimal("0")))
                losses.append(max(-diff, Decimal("0")))
            win_g = gains[-period:]
            win_l = losses[-period:]
            avg_g = sum(win_g) / Decimal(len(win_g))
            avg_l = sum(win_l) / Decimal(len(win_l))
            if avg_l <= Decimal("0"):
                rsi_dec = Decimal("100") if avg_g > Decimal("0") else Decimal("50")
            else:
                rs = avg_g / avg_l
                rsi_dec = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))
            return {"rsi": rsi_dec.quantize(Decimal("0.01"))}

        if norm_ind in {"bb", "bollinger"}:
            window = closes[-period:]
            middle = sum(window) / Decimal(len(window))
            dev_mult = Decimal(str(params.get("deviation", "2")))
            if len(window) >= 2:
                var_f = sum((float(c - middle) ** 2) for c in window) / (len(window) - 1)
                std_dec = Decimal(f"{math.sqrt(var_f):.6f}")
            else:
                std_dec = Decimal("0")
            return {
                "bb_lower": (middle - dev_mult * std_dec).quantize(Decimal("0.0001")),
                "bb_middle": middle.quantize(Decimal("0.0001")),
                "bb_upper": (middle + dev_mult * std_dec).quantize(Decimal("0.0001")),
            }

        if norm_ind == "macd":
            fast_p = max(1, int(params.get("fast", 12)))
            slow_p = max(2, int(params.get("slow", 26)))
            sig_p = max(1, int(params.get("signal", 9)))
            a_f = Decimal("2") / Decimal(fast_p + 1)
            a_s = Decimal("2") / Decimal(slow_p + 1)
            a_sig = Decimal("2") / Decimal(sig_p + 1)
            ef = closes[0]
            es = closes[0]
            macd_series: list[Decimal] = []
            for c_val in closes:
                ef = a_f * c_val + (Decimal("1") - a_f) * ef
                es = a_s * c_val + (Decimal("1") - a_s) * es
                macd_series.append(ef - es)
            sig_val = macd_series[0]
            for m_val in macd_series[1:]:
                sig_val = a_sig * m_val + (Decimal("1") - a_sig) * sig_val
            m_now = macd_series[-1].quantize(Decimal("0.0001"))
            s_now = sig_val.quantize(Decimal("0.0001"))
            return {
                "macd": m_now,
                "signal": s_now,
                "histogram": (m_now - s_now).quantize(Decimal("0.0001")),
            }

        msg = f"Неизвестный индикатор {indicator} в режиме бэктеста"
        raise ValueError(msg)

    async def resolve_instrument(self, ticker: str, class_code: str) -> Instrument:
        """Разрешает инструмент по сохранённому каталогу; сети в бэктесте нет."""
        symbol = ticker.strip().upper()
        entry = await self._catalog_lookup(symbol, class_code.strip().upper() or None)
        if entry is not None:
            return entry.to_instrument(is_benchmark=symbol == "IMOEX")
        return Instrument(
            uid=f"backtest-{symbol.lower()}",
            ticker=symbol,
            class_code=class_code.strip().upper() or "TQBR",
            lot_size=10,
            is_benchmark=(symbol == "IMOEX"),
            currency="RUB",
        )

    async def _catalog_lookup(
        self, ticker: str, class_code: str | None
    ) -> InstrumentCatalogEntry | None:
        if self._repository is None:
            return None
        find_entry = getattr(self._repository, "find_catalog_entry", None)
        if find_entry is None:
            return None
        entry: InstrumentCatalogEntry | None = await find_entry(ticker, class_code)
        return entry

    async def fetch_catalog(self, instrument_types: Any = None) -> list[InstrumentCatalogEntry]:
        """Сетевого справочника в бэктесте нет: каталог только из сохранённых данных."""
        msg = (
            "Бэктест работает без сети: справочник инструментов недоступен. "
            "Обновите каталог в контуре live/sandbox — он сохранится в БД "
            "и будет использован здесь."
        )
        raise CatalogUnavailableError(msg)

    async def search_instruments(
        self,
        query: str,
        *,
        instrument_type: str | None = None,
        limit: int = 20,
    ) -> list[InstrumentCatalogEntry]:
        """Поиск по сохранённому каталогу — источнику справочника в бэктесте."""
        if self._repository is None:
            msg = "Реплей бэктеста без репозитория не может искать инструменты"
            raise CatalogUnavailableError(msg)
        entries: list[InstrumentCatalogEntry] = await self._repository.list_catalog_entries(
            query=query,
            instrument_types=[instrument_type] if instrument_type else None,
            tradable_only=False,
            limit=max(limit, 1),
        )
        return entries

    def set_clock(self, clock: Any) -> None:
        self._clock = clock

    async def aclose(self) -> None:
        self._cache.clear()


def warm_retention_cutoff(now: datetime, days: int) -> datetime:
    """Граница тёплого слоя — для агрегации свечей."""
    return now - timedelta(days=days)
