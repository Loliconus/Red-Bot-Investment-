"""Загрузчик многолетних архивов свечей T-Invest API (раздел 1.1 и 1.2 ТЗ).

Поддерживает два штатных канала бэкфилла глубиной 7–10 лет:
1. Годовые ZIP-архивы минутных свечей ``GET https://invest-public-api.tbank.ru/history-data``
   по параметрам ``instrument_id`` (или ``figi``) и ``year`` с последующей агрегацией
   минуток в рабочую иерархию ``5m``, ``15m``, ``1h``, ``4h``, ``1d``.
2. Пагинированная выгрузка через ``MarketDataService.GetCandles`` с учётом заголовков
   квотирования ``x-ratelimit-limit``, ``x-ratelimit-remaining``, ``x-ratelimit-reset``.
"""

from __future__ import annotations

import asyncio
import io
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from adapters.driven.tbank.retry import AdaptiveRateLimitGovernor
from adapters.driven.tbank.tls import DEFAULT_CA_PATH, resolve_ca_path
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV
from core.synthetic.feature_store import SyntheticTimeframe, resample_candles

logger = structlog.get_logger(__name__)

HISTORY_DATA_ENDPOINT = "https://invest-public-api.tbank.ru/history-data"

#: Тип транспортной функции для чтения ZIP-байтов и заголовков ответа (удобно для тестов).
ArchiveFetchFn = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]


@dataclass(frozen=True, slots=True, kw_only=True)
class YearlyBackfillSummary:
    """Отчёт о загрузке многолетней истории по инструменту."""

    instrument_id: str
    years_requested: tuple[int, ...]
    years_loaded: tuple[int, ...]
    minute_bars_loaded: int
    resampled_counts: dict[str, int]


def parse_history_zip_bytes(
    zip_payload: bytes,
    *,
    domain_timeframe: Timeframe = Timeframe.M1,
) -> list[OHLCV]:
    """Распаковывает годовой ZIP-архив T-Invest и парсит CSV-файлы минутных свечей.

    Формат строк в CSV архива T-Invest ``history-data``::

        uid_or_figi;2024-01-03T07:00:00Z;270.15;270.50;270.60;270.10;14520;
    """
    if not zip_payload:
        return []

    dedup: dict[datetime, OHLCV] = {}
    with zipfile.ZipFile(io.BytesIO(zip_payload), mode="r") as archive:
        for member in sorted(archive.namelist()):
            if member.endswith("/"):
                continue
            raw_text = archive.read(member).decode("utf-8", errors="replace")
            for raw_line in raw_text.splitlines():
                line = raw_line.strip().rstrip(";")
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(";")]
                if len(parts) < 7:
                    continue
                ts_raw = parts[1]
                if ts_raw.lower() in {"time", "timestamp", "date"}:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone(UTC)
                    open_p = Decimal(parts[2])
                    close_p = Decimal(parts[3])
                    high_p = Decimal(parts[4])
                    low_p = Decimal(parts[5])
                    volume = int(float(parts[6]))
                except (ValueError, ArithmeticError):
                    continue

                if high_p < max(open_p, close_p):
                    high_p = max(open_p, close_p, high_p)
                if low_p > min(open_p, close_p):
                    low_p = min(open_p, close_p, low_p)

                dedup[ts] = OHLCV(
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=max(0, volume),
                    timestamp=ts,
                    timeframe=domain_timeframe,
                )

    return [dedup[ts] for ts in sorted(dedup)]


def _default_http_fetch(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    ca_path = resolve_ca_path(DEFAULT_CA_PATH)
    ssl_ctx = ssl.create_default_context(cafile=str(ca_path) if ca_path.exists() else None)
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=30) as resp:  # noqa: S310
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), resp_headers, resp.read()
    except urllib.error.HTTPError as exc:
        err_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return int(exc.code), err_headers, b""


class TInvestHistoryArchiveLoader:
    """Загрузчик годовых ZIP-архивов ``history-data`` с контролем ``x-ratelimit-*``."""

    def __init__(
        self,
        token: str,
        *,
        endpoint: str = HISTORY_DATA_ENDPOINT,
        fetch_fn: ArchiveFetchFn | None = None,
    ) -> None:
        self._token = token
        self._endpoint = endpoint
        self._fetch_fn = fetch_fn or _default_http_fetch
        self._governor = AdaptiveRateLimitGovernor(low_watermark=2)

    async def download_year_minutes(
        self,
        instrument_id: str,
        year: int,
        *,
        use_figi_param: bool = False,
    ) -> list[OHLCV]:
        """Скачивает и распаковывает 1m-свечи за один календарный год ``year``."""
        await self._governor.throttle_if_needed()
        id_key = "figi" if use_figi_param else "instrument_id"
        query = urllib.parse.urlencode({id_key: instrument_id, "year": str(year)})
        url = f"{self._endpoint}?{query}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/zip",
            "User-Agent": "Red-Bot-Investment/SyntheticTrader",
        }

        status, resp_headers, payload = await asyncio.to_thread(self._fetch_fn, url, headers)
        self._governor.observe(resp_headers)

        if status == 404:
            logger.info("history_archive_year_empty", instrument_id=instrument_id, year=year)
            return []
        if status == 429:
            rl = self._governor.last_headers
            wait_sec = rl.reset_seconds if rl is not None and rl.reset_seconds > 0 else 5.0
            await asyncio.sleep(min(wait_sec, 60.0))
            status, resp_headers, payload = await asyncio.to_thread(self._fetch_fn, url, headers)
            self._governor.observe(resp_headers)

        if status != 200 or not payload:
            logger.warning(
                "history_archive_download_failed",
                instrument_id=instrument_id,
                year=year,
                status=status,
            )
            return []

        return parse_history_zip_bytes(payload, domain_timeframe=Timeframe.M1)

    async def backfill_multi_year_hierarchy(
        self,
        instrument_id: str,
        years: Sequence[int],
    ) -> tuple[dict[str, tuple[OHLCV, ...]], YearlyBackfillSummary]:
        """Выгружает минутные архивы за ``years`` (7–10 лет) и агрегирует всю иерархию ТФ."""
        all_minutes: dict[datetime, OHLCV] = {}
        loaded_years: list[int] = []

        for yr in sorted(set(years)):
            bars = await self.download_year_minutes(instrument_id, yr)
            if bars:
                loaded_years.append(yr)
                for b in bars:
                    all_minutes[b.timestamp] = b

        ordered_1m = tuple(all_minutes[ts] for ts in sorted(all_minutes))
        hierarchy_bars: dict[str, tuple[OHLCV, ...]] = {
            SyntheticTimeframe.M1.value: ordered_1m,
            SyntheticTimeframe.M5.value: resample_candles(
                ordered_1m,
                SyntheticTimeframe.M5,
                domain_timeframe=Timeframe.M1,
            ),
            SyntheticTimeframe.M15.value: resample_candles(
                ordered_1m,
                SyntheticTimeframe.M15,
                domain_timeframe=Timeframe.M1,
            ),
            SyntheticTimeframe.H1.value: resample_candles(
                ordered_1m,
                SyntheticTimeframe.H1,
                domain_timeframe=Timeframe.H1,
            ),
            SyntheticTimeframe.H4.value: resample_candles(
                ordered_1m,
                SyntheticTimeframe.H4,
                domain_timeframe=Timeframe.H1,
            ),
            SyntheticTimeframe.D1.value: resample_candles(
                ordered_1m,
                SyntheticTimeframe.D1,
                domain_timeframe=Timeframe.D1,
            ),
        }

        summary = YearlyBackfillSummary(
            instrument_id=instrument_id,
            years_requested=tuple(sorted(set(years))),
            years_loaded=tuple(loaded_years),
            minute_bars_loaded=len(ordered_1m),
            resampled_counts={k: len(v) for k, v in hierarchy_bars.items()},
        )
        return hierarchy_bars, summary
