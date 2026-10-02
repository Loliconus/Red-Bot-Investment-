"""ISS MOEX через aiomoex (его пагинация), UTC и проверка качества свечей."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import aiohttp
import aiomoex
import numpy as np
import pandas as pd

from core.domain.probability import ResearchInstrument
from synthetic_trader.config import ExperimentConfig

INTERVALS = {"10m": 10, "1h": 60, "1d": 24}
PRICE_COLUMNS = ("open", "high", "low", "close")
# Классификация для группового лимита, не источник лотности / UID.
SECTORS = {
    "SBER": "finance",
    "VTBR": "finance",
    "GAZP": "oil_gas",
    "LKOH": "oil_gas",
    "TATN": "oil_gas",
    "ROSN": "oil_gas",
    "GMKN": "metals",
}


class DataQualityError(ValueError):
    """Неполная, неоднозначная или испорченная история — не результат бэктеста."""


@dataclass(slots=True)
class MarketDataset:
    frames: dict[tuple[str, str], pd.DataFrame]
    instruments: list[ResearchInstrument]
    source: str
    warnings: list[str]


def validate_candles(frame: pd.DataFrame) -> pd.DataFrame:
    """Не исправляет дубликаты и плохой OHLC молча. Цены остаются Decimal text."""
    required = {"begin", "end", "open", "high", "low", "close", "volume"}
    if frame.empty or not required.issubset(frame.columns):
        raise DataQualityError("Нет свечей или обязательных OHLCV-полей")
    result = frame.sort_values("end").reset_index(drop=True).copy()
    for field in ("begin", "end"):
        timestamps = pd.to_datetime(result[field])
        if timestamps.dt.tz is None:
            raise DataQualityError("Наивные timestamps запрещены; явно задайте исходную timezone")
        result[field] = timestamps.dt.tz_convert("UTC")
    if result["begin"].duplicated().any() or result["end"].duplicated().any():
        raise DataQualityError("Дубликаты свечей не могут быть молча удалены")
    if (result["end"] <= result["begin"]).any():
        raise DataQualityError("Некорректный интервал свечи")
    if (
        result["begin"].iloc[1:].reset_index(drop=True)
        < result["end"].iloc[:-1].reset_index(drop=True)
    ).any():
        raise DataQualityError("Пересекающиеся свечи")
    numbers = result[[*PRICE_COLUMNS, "volume"]].astype(float)
    if not np.isfinite(numbers.to_numpy()).all() or (numbers[list(PRICE_COLUMNS)] <= 0).any().any():
        raise DataQualityError("Неконечная / неположительная цена")
    if (numbers["volume"] < 0).any():
        raise DataQualityError("Отрицательный объём")
    if (numbers["low"] > numbers[["open", "close"]].min(axis=1)).any() or (
        numbers["high"] < numbers[["open", "close"]].max(axis=1)
    ).any():
        raise DataQualityError("Нарушена OHLC-геометрия")
    for col in (*PRICE_COLUMNS, "volume"):
        result[col] = result[col].map(lambda value: str(Decimal(str(value))))
    return result[["begin", "end", "open", "high", "low", "close", "volume"]]


def normalize_iss(records: list[dict[str, Any]], *, asof: datetime | None = None) -> pd.DataFrame:
    if not records:
        raise DataQualityError(
            "ISS вернул пустую историю — проверьте ticker, interval и candle borders"
        )
    frame = pd.DataFrame(records)
    for col in ("begin", "end"):
        frame[col] = pd.to_datetime(frame[col]).dt.tz_localize("Europe/Moscow").dt.tz_convert("UTC")
    # ISS end включителен (например 10:59:59); availability = 11:00:00.
    frame["end"] = frame["end"] + pd.Timedelta(seconds=1)
    frame = frame.loc[frame["end"] <= pd.Timestamp(asof or datetime.now(UTC))]
    return validate_candles(frame)


async def _read_with_retry(work: Callable[[], Awaitable[Any]]) -> Any:
    for attempt in range(4):
        try:
            return await work()
        except aiohttp.ClientResponseError as exc:
            if exc.status not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
        except (aiohttp.ClientConnectionError, TimeoutError):
            if attempt == 3:
                raise
        await asyncio.sleep(0.5 * 2**attempt)
    raise RuntimeError("Недостижимое состояние read-only retry")


async def load_moex(config: ExperimentConfig) -> MarketDataset:
    frames: dict[tuple[str, str], pd.DataFrame] = {}
    instruments: list[ResearchInstrument] = []
    semaphore = asyncio.Semaphore(3)
    timeframes = (
        tuple(dict.fromkeys((config.interval, "1h", "1d"))) if config.interval != "1d" else ("1d",)
    )
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=90), raise_for_status=True
    ) as session:

        async def fetch(symbol: str, timeframe: str) -> None:
            async with semaphore:
                if symbol == "IMOEX":
                    rows = await _read_with_retry(
                        lambda: aiomoex.get_market_candles(
                            session,
                            symbol,
                            interval=INTERVALS[timeframe],
                            start=config.start.isoformat(),
                            end=config.end.isoformat(),
                            market="index",
                            engine="stock",
                        )
                    )
                else:
                    rows = await _read_with_retry(
                        lambda: aiomoex.get_board_candles(
                            session,
                            symbol,
                            interval=INTERVALS[timeframe],
                            start=config.start.isoformat(),
                            end=config.end.isoformat(),
                            board="TQBR",
                            market="shares",
                            engine="stock",
                        )
                    )
                frames[(symbol, timeframe)] = normalize_iss(rows)

        async def metadata(symbol: str) -> None:
            if symbol == "IMOEX":
                instruments.append(
                    ResearchInstrument(
                        symbol=symbol, lot_size=1, tick_size=Decimal("0.01"), is_benchmark=True
                    )
                )
                return
            async with semaphore:
                url = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{symbol}.json"
                client = aiomoex.ISSClient(
                    session, url, {"iss.only": "securities", "iss.meta": "off"}
                )
                response = await _read_with_retry(client.get)
                securities = response.get("securities", [])
                row = next(
                    (
                        r
                        for r in securities
                        if r.get("SECID") == symbol and r.get("BOARDID") == "TQBR"
                    ),
                    None,
                )
                if row is None or not row.get("LOTSIZE") or not row.get("MINSTEP"):
                    raise DataQualityError(
                        f"Нет подтверждённой лотности / шага цены TQBR для {symbol}"
                    )
                instruments.append(
                    ResearchInstrument(
                        symbol=symbol,
                        lot_size=int(row["LOTSIZE"]),
                        tick_size=Decimal(str(row["MINSTEP"])),
                        sector=SECTORS.get(symbol, "unclassified"),
                    )
                )

        universe = ("IMOEX", *config.symbols)
        await asyncio.gather(
            *(fetch(s, tf) for s in universe for tf in timeframes), *(metadata(s) for s in universe)
        )
    if sum(len(f) for f in frames.values()) > 2_000_000:
        raise DataQualityError("Пилот ограничен 2 млн свечей; уменьшите корзину / период")
    warnings = [
        "История ISS — price OHLCV, не total return: дивиденды, делистинги и корпоративные действия не скорректированы.",
        "Текущая корзина создаёт survivorship bias; лотность/шаг цены — текущий снэпшот ISS, не исторический справочник.",
        "Секторная карта — конфигурационная классификация; unknown группируются вместе. MOEX closures/праздники не выдумываются.",
        "5m/15m не выводятся из 10m: для них нужен отдельный источник; 4H строится только из полных 1H баров.",
    ]
    cutoff = (
        pd.Timestamp(config.end, tz="Europe/Moscow")
        + pd.Timedelta(days=1)
        - pd.DateOffset(months=config.freeze_months)
    ).tz_convert("UTC")
    for (symbol, tf), frame in frames.items():
        if tf == config.interval and (frame["end"].max() - frame["begin"].min()).days < 7 * 365:
            warnings.append(
                f"{symbol}: доступно менее 7 лет {tf}; кризисные режимы могут быть не покрыты."
            )
        # Не раскрывать даже факт price jump из закрытого holdout в development warnings.
        change = frame.loc[frame["end"] <= cutoff, "close"].astype(float).pct_change().abs()
        if (change > 0.35).any():
            warnings.append(
                f"{symbol}/{tf}: обнаружены скачки >35%; проверьте сплиты/реденоминации до выводов о доходности."
            )
    return MarketDataset(
        frames=frames,
        instruments=sorted(instruments, key=lambda i: i.symbol),
        source="moex",
        warnings=warnings,
    )


def demo_dataset(config: ExperimentConfig) -> MarketDataset:
    """Детерминированные искусственные OHLCV. Никогда не маскируются под MOEX."""
    rng = np.random.default_rng(config.seed)
    days = pd.bdate_range(config.start, config.end, tz="Europe/Moscow")
    # До 900 торговых дней: demo проверяет инфраструктуру, не историю биржи.
    days = days[-900:]
    if config.interval == "1d":
        begins = days
        ends = begins + pd.Timedelta(days=1)
    else:
        step = 60 if config.interval == "1h" else 10
        begins = pd.DatetimeIndex(
            [
                day + pd.Timedelta(minutes=600 + offset)
                for day in days
                for offset in range(0, 420, step)
            ]
        )
        ends = begins + pd.Timedelta(minutes=step)
    n = len(begins)
    phase = np.arange(n) // (50 if config.interval == "1d" else 140)
    drift = np.choose(phase % 4, [0.0018, -0.0016, 0.0, 0.0005])
    noise = np.choose(phase % 4, [0.0025, 0.003, 0.003, 0.008])
    market_returns = drift + rng.normal(0, noise)
    frames: dict[tuple[str, str], pd.DataFrame] = {}
    instruments: list[ResearchInstrument] = []
    for index, symbol in enumerate(("IMOEX", *config.symbols)):
        returns = (
            market_returns if index == 0 else 0.8 * market_returns + rng.normal(0, noise * 0.5)
        )
        base = 3000.0 if index == 0 else 100.0 + index * 50
        close = base * np.exp(np.cumsum(returns))
        open_ = np.r_[base, close[:-1]] * np.exp(rng.normal(0, 0.0003, n))
        spread = np.abs(rng.normal(0.0012, 0.0005, n))
        frame = pd.DataFrame(
            {
                "begin": begins.tz_convert("UTC"),
                "end": ends.tz_convert("UTC"),
                "open": open_,
                "close": close,
                "high": np.maximum(open_, close) * (1 + spread),
                "low": np.minimum(open_, close) * (1 - spread),
                "volume": rng.lognormal(12, 0.4, n),
            }
        )
        for col in PRICE_COLUMNS:
            frame[col] = frame[col].map(lambda x: f"{x:.6f}")
        frame["volume"] = frame["volume"].round().astype(str)
        frames[(symbol, config.interval)] = validate_candles(frame)
        instruments.append(
            ResearchInstrument(
                symbol=symbol,
                lot_size=1 if index == 0 else 10,
                tick_size=Decimal("0.01"),
                is_benchmark=index == 0,
                sector=SECTORS.get(symbol, "demo"),
            )
        )
        if config.interval != "1d":
            frames[(symbol, "1h")] = aggregate_closed_bars(frame, "1h", config.interval)
            frames[(symbol, "1d")] = aggregate_closed_bars(frame, "1d", config.interval)
    return MarketDataset(
        frames=frames,
        instruments=instruments,
        source="demo",
        warnings=[
            "DEMO: искусственные данные, условные лоты и календарь; результат не является инвестиционным свидетельством.",
            "Demo ограничено последними 900 будними днями периода; праздники и реальные сессии MOEX не моделируются.",
        ],
    )


def aggregate_closed_bars(frame: pd.DataFrame, target: str, base: str) -> pd.DataFrame:
    """Консервативная агрегация: incomplete bucket не публикуется досрочно.

    Intraday origin — 10:00 MSK, дневной availability — следующие 00:00 MSK.
    Пропуски внутри intraday bucket исключают его; короткий остаток сессии не
    объявляется полным 4H-баром. 10m → 5m/15m принципиально запрещено.
    """
    minutes = {"10m": 10, "1h": 60, "4h": 240, "1d": 1440}
    if target not in minutes or base not in minutes or minutes[target] < minutes[base]:
        raise DataQualityError(
            "Нельзя повышать разрешение / использовать неподдерживаемый timeframe"
        )
    if minutes[target] % minutes[base]:
        raise DataQualityError("Целевой timeframe не кратен исходному")
    if target == base:
        return validate_candles(frame)
    data = frame.copy()
    data["_index"] = data["begin"].dt.tz_convert("Europe/Moscow")
    data = data.set_index("_index")
    for col in (*PRICE_COLUMNS, "volume"):
        data[col] = data[col].astype(float)
    rule = "1D" if target == "1d" else f"{minutes[target]}min"
    offset = None if target == "1d" else "10h"
    groups = data.resample(rule, origin="start_day", offset=offset, closed="left", label="left")
    result = groups.agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    count = groups["close"].count()
    result["begin"] = result.index.tz_convert("UTC")
    result["end"] = (result.index + pd.Timedelta(minutes=minutes[target])).tz_convert("UTC")
    result = result.loc[count > 0]
    if target != "1d":
        result = result.loc[count == minutes[target] // minutes[base]]
    result = result.loc[result["end"] <= frame["end"].max()].dropna().reset_index(drop=True)
    if result.empty:
        raise DataQualityError(f"Недостаточно закрытых {base}-баров для {target}")
    return validate_candles(result)
