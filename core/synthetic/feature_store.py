"""Point-in-Time Feature Store и синхронизация мультитаймфреймовой иерархии без утечек.

Иерархия таймфреймов (раздел 1.3 ТЗ «Синтетический трейдер»)::

    1D / 4H (старший контекст режима)
      → 1H (рабочий горизонт)
      → 15m / 5m (точка входа)

Критичное инженерное правило:
Все старшие фреймы приводятся к базовому шагу через **forward-fill только по
полностью закрытым барам**. На момент времени ``t`` признаки старшего фрейма
(1H / 4H / 1D) берутся исключительно из последнего **закрытого** бара
(``bar.timestamp + duration(tf) <= t``); между закрытиями значение держится
константным. Любое использование ещё не закрытого бара старшего фрейма —
скрытая утечка будущего в прошлое, отлавливаемая ``verify_no_lookahead_leakage``.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV, CandleSeries


class SyntheticTimeframe(StrEnum):
    """Иерархия таймфреймов «Синтетического трейдера» (покрывается T-Invest API)."""

    S5 = "5s"
    M1 = "1m"
    M2 = "2m"
    M3 = "3m"
    M5 = "5m"
    M10 = "10m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"
    MN1 = "1mo"


TIMEFRAME_DURATIONS: dict[str, timedelta] = {
    "5s": timedelta(seconds=5),
    "1m": timedelta(minutes=1),
    "2m": timedelta(minutes=2),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "10m": timedelta(minutes=10),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
    "1mo": timedelta(days=30),
}

#: Рабочая иерархия по умолчанию: старший контекст → рабочий горизонт → вход.
DEFAULT_HIERARCHY: tuple[SyntheticTimeframe, ...] = (
    SyntheticTimeframe.D1,
    SyntheticTimeframe.H4,
    SyntheticTimeframe.H1,
    SyntheticTimeframe.M15,
    SyntheticTimeframe.M5,
)


def timeframe_duration(timeframe: SyntheticTimeframe | Timeframe | str) -> timedelta:
    """Возвращает длительность одного бара для таймфрейма."""
    key = timeframe.value if isinstance(timeframe, (SyntheticTimeframe, Timeframe)) else timeframe
    duration = TIMEFRAME_DURATIONS.get(key)
    if duration is None:
        msg = f"Неизвестный таймфрейм: {timeframe}"
        raise ValueError(msg)
    return duration


def bar_close_timestamp(
    bar: OHLCV,
    timeframe: SyntheticTimeframe | Timeframe | str | None = None,
) -> datetime:
    """Момент полного закрытия бара ``[open_ts, open_ts + duration)``.

    Свеча в T-Invest API маркируется временем открытия ``candle.time``.
    Следовательно, часовая свеча ``10:00`` полностью закрыта только в ``11:00``.
    В ``10:15`` или ``10:45`` она ещё формируется, и её ``high/low/close``
    содержат информацию из будущего относительно момента ``10:15``.
    """
    tf_key: SyntheticTimeframe | Timeframe | str = (
        timeframe if timeframe is not None else bar.timeframe
    )
    return bar.timestamp + timeframe_duration(tf_key)


def is_bar_closed_at(
    bar: OHLCV,
    moment: datetime,
    timeframe: SyntheticTimeframe | Timeframe | str | None = None,
) -> bool:
    """Проверяет, что бар полностью закрылся к моменту ``moment`` (UTC)."""
    if moment.tzinfo is None or bar.timestamp.tzinfo is None:
        msg = "Временные метки в FeatureStore обязаны быть tz-aware (UTC)"
        raise ValueError(msg)
    return bar_close_timestamp(bar, timeframe) <= moment


def _bucket_start(ts: datetime, duration: timedelta) -> datetime:
    """Выравнивает UTC-метку к началу интервала ``duration``."""
    epoch_seconds = int(ts.astimezone(UTC).timestamp())
    step_seconds = int(duration.total_seconds())
    aligned = (epoch_seconds // step_seconds) * step_seconds
    return datetime.fromtimestamp(aligned, tz=UTC)


def resample_candles(
    candles: Sequence[OHLCV],
    target_timeframe: SyntheticTimeframe | Timeframe | str,
    *,
    domain_timeframe: Timeframe = Timeframe.H1,
) -> tuple[OHLCV, ...]:
    """Агрегирует младшие свечи (например, архивные 1m) в старший таймфрейм.

    Используется при бэкфилле годовых ZIP-архивов минутных свечей в рабочие
    интервалы ``5m``, ``15m``, ``1h``, ``4h``, ``1d``.
    """
    if not candles:
        return ()

    duration = timeframe_duration(target_timeframe)
    ordered = sorted(candles, key=lambda c: c.timestamp)

    buckets: dict[datetime, list[OHLCV]] = {}
    for candle in ordered:
        b_start = _bucket_start(candle.timestamp, duration)
        buckets.setdefault(b_start, []).append(candle)

    aggregated: list[OHLCV] = []
    for b_start in sorted(buckets):
        group = buckets[b_start]
        aggregated.append(
            OHLCV(
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
                timestamp=b_start,
                timeframe=domain_timeframe,
            )
        )
    return tuple(aggregated)


@dataclass(frozen=True, slots=True, kw_only=True)
class AlignedMultiTimeframeSnapshot:
    """Point-in-time срез иерархии таймфреймов на момент ``as_of``."""

    instrument_uid: str
    as_of: datetime
    base_timeframe: str
    closed_bars_by_tf: Mapping[str, tuple[OHLCV, ...]]
    last_closed_by_tf: Mapping[str, OHLCV]

    def series_for(
        self,
        tf: SyntheticTimeframe | Timeframe | str,
        *,
        domain_tf: Timeframe = Timeframe.H1,
    ) -> CandleSeries:
        key = tf.value if isinstance(tf, (SyntheticTimeframe, Timeframe)) else tf
        bars = self.closed_bars_by_tf.get(key, ())
        return CandleSeries(timeframe=domain_tf, candles=bars)


class PointInTimeFeatureStore:
    """Хранилище свечей и признаков с защитой от межфреймовых утечек (look-ahead).

    Индексирует серии по времени **полного закрытия** бара ``close_time = open_time + duration``.
    Запрос на момент ``as_of`` возвращает только бары с ``close_time <= as_of``
    за O(log N) через бинарный поиск ``bisect_right``.
    """

    __slots__ = ("_bars", "_close_times")

    def __init__(self) -> None:
        self._bars: dict[tuple[str, str], tuple[OHLCV, ...]] = {}
        self._close_times: dict[tuple[str, str], tuple[datetime, ...]] = {}

    def put_candles(
        self,
        instrument_uid: str,
        timeframe: SyntheticTimeframe | Timeframe | str,
        candles: Sequence[OHLCV],
    ) -> None:
        """Регистрирует упорядоченную серию свечей для инструмента и таймфрейма."""
        tf_key = (
            timeframe.value
            if isinstance(timeframe, (SyntheticTimeframe, Timeframe))
            else str(timeframe)
        )
        duration = timeframe_duration(tf_key)
        dedup: dict[datetime, OHLCV] = {}
        for bar in candles:
            if bar.timestamp.tzinfo is None:
                msg = f"Naive datetime запрещён в FeatureStore: {bar.timestamp}"
                raise ValueError(msg)
            dedup[bar.timestamp.astimezone(UTC)] = bar

        ordered = tuple(dedup[ts] for ts in sorted(dedup))
        close_ts = tuple(bar.timestamp.astimezone(UTC) + duration for bar in ordered)
        self._bars[(instrument_uid, tf_key)] = ordered
        self._close_times[(instrument_uid, tf_key)] = close_ts

    def closed_bars_as_of(
        self,
        instrument_uid: str,
        timeframe: SyntheticTimeframe | Timeframe | str,
        as_of: datetime,
        *,
        max_bars: int | None = None,
    ) -> tuple[OHLCV, ...]:
        """Возвращает исключительно полностью закрытые к моменту ``as_of`` бары."""
        if as_of.tzinfo is None:
            msg = "Момент времени as_of обязан быть tz-aware (UTC)"
            raise ValueError(msg)
        tf_key = (
            timeframe.value
            if isinstance(timeframe, (SyntheticTimeframe, Timeframe))
            else str(timeframe)
        )
        key = (instrument_uid, tf_key)
        bars = self._bars.get(key, ())
        if not bars:
            return ()

        close_times = self._close_times[key]
        cutoff = bisect_right(close_times, as_of.astimezone(UTC))
        eligible = bars[:cutoff]
        if max_bars is not None and len(eligible) > max_bars:
            return eligible[-max_bars:]
        return eligible

    def latest_closed_bar(
        self,
        instrument_uid: str,
        timeframe: SyntheticTimeframe | Timeframe | str,
        as_of: datetime,
    ) -> OHLCV | None:
        """Forward-fill последнего полностью закрытого бара старшего таймфрейма."""
        bars = self.closed_bars_as_of(instrument_uid, timeframe, as_of, max_bars=1)
        return bars[-1] if bars else None

    def snapshot_as_of(
        self,
        instrument_uid: str,
        as_of: datetime,
        *,
        base_timeframe: SyntheticTimeframe | Timeframe | str = SyntheticTimeframe.H1,
        hierarchy: Sequence[SyntheticTimeframe | Timeframe | str] = DEFAULT_HIERARCHY,
        lookback_bars: int = 250,
    ) -> AlignedMultiTimeframeSnapshot:
        """Формирует синхронизированный срез всех уровней иерархии без утечек."""
        base_key = (
            base_timeframe.value
            if isinstance(base_timeframe, (SyntheticTimeframe, Timeframe))
            else str(base_timeframe)
        )
        closed_by_tf: dict[str, tuple[OHLCV, ...]] = {}
        last_by_tf: dict[str, OHLCV] = {}

        for tf in hierarchy:
            tf_key = tf.value if isinstance(tf, (SyntheticTimeframe, Timeframe)) else str(tf)
            bars = self.closed_bars_as_of(instrument_uid, tf_key, as_of, max_bars=lookback_bars)
            closed_by_tf[tf_key] = bars
            if bars:
                last_by_tf[tf_key] = bars[-1]

        return AlignedMultiTimeframeSnapshot(
            instrument_uid=instrument_uid,
            as_of=as_of,
            base_timeframe=base_key,
            closed_bars_by_tf=closed_by_tf,
            last_closed_by_tf=last_by_tf,
        )


def verify_no_lookahead_leakage(
    store: PointInTimeFeatureStore,
    instrument_uid: str,
    probe_timestamps: Sequence[datetime],
    feature_fn: Callable[[AlignedMultiTimeframeSnapshot], Mapping[str, Decimal]],
    *,
    hierarchy: Sequence[SyntheticTimeframe | Timeframe | str] = DEFAULT_HIERARCHY,
) -> bool:
    """Аудит отсутствия утечки будущего в прошлое (раздел 1.3 и 7.1 ТЗ).

    Для каждого момента ``t`` из ``probe_timestamps``:
    1. Считает вектор признаков по полному ``PointInTimeFeatureStore``.
    2. Создаёт второй стор, где все бары с ``close_time > t`` искажены
       (цены умножены на 10, объёмы увеличены в 100 раз).
    3. Проверяет побитовое совпадение всех признаков на момент ``t``.
    """
    for moment in probe_timestamps:
        baseline_snap = store.snapshot_as_of(instrument_uid, moment, hierarchy=hierarchy)
        baseline_features = dict(feature_fn(baseline_snap))

        perturbed_store = PointInTimeFeatureStore()
        for tf in hierarchy:
            tf_key = tf.value if isinstance(tf, (SyntheticTimeframe, Timeframe)) else str(tf)
            all_bars = store._bars.get((instrument_uid, tf_key), ())  # noqa: SLF001
            duration = timeframe_duration(tf_key)
            mutated: list[OHLCV] = []
            for bar in all_bars:
                if bar.timestamp + duration > moment:
                    # Искажаем ещё не закрытый или будущий бар
                    mutated.append(
                        OHLCV(
                            open=bar.open * Decimal("10"),
                            high=bar.high * Decimal("15"),
                            low=bar.low * Decimal("5"),
                            close=bar.close * Decimal("12"),
                            volume=bar.volume * 100 + 99999,
                            timestamp=bar.timestamp,
                            timeframe=bar.timeframe,
                        )
                    )
                else:
                    mutated.append(bar)
            perturbed_store.put_candles(instrument_uid, tf_key, mutated)

        perturbed_snap = perturbed_store.snapshot_as_of(instrument_uid, moment, hierarchy=hierarchy)
        perturbed_features = dict(feature_fn(perturbed_snap))
        if baseline_features != perturbed_features:
            return False
    return True
