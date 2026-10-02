"""Событийный OHLC-бэктест: сигнал на close → исполнение на следующем open.

Все open одного времени обрабатываются до любого последующего close. При
неизвестном intrabar-порядке STOP имеет приоритет над TAKE. Трейлинг изменяет
стоп только для следующего бара. Kill switch не сбрасывается новым днём.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from core.domain.probability import (
    ONE,
    ZERO,
    MarketProbabilities,
    ProbabilityRegime,
    ProbabilityRiskPolicy,
    ResearchBar,
    ResearchInstrument,
    require_decimal,
)
from core.risk.probability import BPS, Exposure, floor_tick, size_probability_position


@dataclass(slots=True, kw_only=True)
class Position:
    instrument: ResearchInstrument
    lots: int
    entry: Decimal
    opened_at: datetime
    entry_fee: Decimal
    stop: Decimal
    target: Decimal
    atr: Decimal
    held_bars: int = 0

    @property
    def units(self) -> int:
        return self.lots * self.instrument.lot_size


@dataclass(frozen=True, slots=True, kw_only=True)
class SimulatedTrade:
    symbol: str
    opened_at: datetime
    closed_at: datetime
    lots: int
    units: int
    entry_price: Decimal
    exit_price: Decimal
    fees: Decimal
    net_pnl: Decimal
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EquityPoint:
    timestamp: datetime
    equity: Decimal
    cash: Decimal
    gross: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbabilityBacktestResult:
    equity: tuple[EquityPoint, ...]
    trades: tuple[SimulatedTrade, ...]
    rejections: dict[str, int]
    kill_reason: str | None
    ambiguous_execution_bars: int


def execution_price(
    raw: Decimal, instrument: ResearchInstrument, policy: ProbabilityRiskPolicy, *, buy: bool
) -> Decimal:
    adjusted = raw * (ONE + (ONE if buy else -ONE) * policy.slippage_bps / BPS)
    rounding = ROUND_CEILING if buy else ROUND_FLOOR
    return (adjusted / instrument.tick_size).to_integral_value(
        rounding=rounding
    ) * instrument.tick_size


class ProbabilityBacktester:
    """Виртуальный портфель. Никаких брокерских методов и real-money адаптеров."""

    def __init__(
        self,
        *,
        instruments: Sequence[ResearchInstrument],
        policy: ProbabilityRiskPolicy,
        initial_capital: Decimal,
    ) -> None:
        require_decimal(initial_capital, positive=True)
        self.instruments = {i.symbol: i for i in instruments if not i.is_benchmark}
        if len(self.instruments) != len([i for i in instruments if not i.is_benchmark]):
            raise ValueError("Повторяющиеся инструменты")
        self.policy = policy
        self.initial_capital = initial_capital

    def run(
        self,
        bars: Sequence[ResearchBar],
        signals: Sequence[MarketProbabilities],
        *,
        correlations: Mapping[datetime, Mapping[tuple[str, str], Decimal]] | None = None,
    ) -> ProbabilityBacktestResult:
        # Состояние локально: повторный run на том же объекте воспроизводим.
        cash = self.initial_capital
        positions: dict[str, Position] = {}
        marks: dict[str, Decimal] = {}
        latest: dict[str, MarketProbabilities] = {}
        consumed: dict[str, datetime] = {}
        trades: list[SimulatedTrade] = []
        equity_curve: list[EquityPoint] = []
        rejections: Counter[str] = Counter()
        killed: str | None = None
        daily_peak = self.initial_capital
        current_day = ""
        last_value = self.initial_capital
        ambiguous = 0
        opens: dict[datetime, list[ResearchBar]] = defaultdict(list)
        closes: dict[datetime, list[ResearchBar]] = defaultdict(list)
        emissions: dict[datetime, list[MarketProbabilities]] = defaultdict(list)
        seen: set[tuple[str, datetime]] = set()
        previous_end: dict[str, datetime] = {}
        for bar in sorted(bars, key=lambda b: (b.begin, b.symbol)):
            if bar.symbol not in self.instruments:
                continue
            key = (bar.symbol, bar.begin)
            if key in seen or bar.begin < previous_end.get(bar.symbol, bar.begin):
                raise ValueError("Дублирующиеся или пересекающиеся бары")
            seen.add(key)
            previous_end[bar.symbol] = bar.end
            opens[bar.begin].append(bar)
            closes[bar.end].append(bar)
        for queued_signal in signals:
            if queued_signal.symbol in self.instruments:
                emissions[queued_signal.asof].append(queued_signal)
        timeline = sorted(set(opens) | set(closes) | set(emissions))
        if not opens:
            raise ValueError("Нет торгуемых баров")

        def account_value() -> tuple[Decimal, Decimal]:
            gross = sum((marks[s] * p.units for s, p in positions.items()), ZERO)
            return cash + gross, gross

        def exit_position(symbol: str, raw: Decimal, moment: datetime, reason: str) -> None:
            nonlocal cash
            pos = positions.pop(symbol)
            price = execution_price(raw, pos.instrument, self.policy, buy=False)
            proceeds = price * pos.units
            fee = proceeds * self.policy.commission_bps / BPS
            cash += proceeds - fee
            trades.append(
                SimulatedTrade(
                    symbol=symbol,
                    opened_at=pos.opened_at,
                    closed_at=moment,
                    lots=pos.lots,
                    units=pos.units,
                    entry_price=pos.entry,
                    exit_price=price,
                    fees=fee + pos.entry_fee,
                    net_pnl=(price - pos.entry) * pos.units - fee - pos.entry_fee,
                    reason=reason,
                )
            )

        def check_daily_limit(moment: datetime) -> None:
            nonlocal killed, daily_peak, current_day, last_value
            # Для границы суток используется московская торговая дата, не UTC.
            from zoneinfo import ZoneInfo

            day = moment.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
            value, _ = account_value()
            if day != current_day:
                current_day, daily_peak = day, last_value
            daily_peak = max(daily_peak, value)
            if value <= daily_peak * (ONE - self.policy.max_daily_drawdown):
                killed = killed or "daily_drawdown_limit"
            if value <= ZERO:
                killed = killed or "insolvency"
            last_value = value

        first_moment = min(opens)
        equity_curve.append(EquityPoint(timestamp=first_moment, equity=cash, cash=cash, gross=ZERO))
        last_bars: dict[str, ResearchBar] = {}
        for moment in timeline:
            # Сначала закрывается предыдущий бар. Новые сигналы ещё не доступны.
            for bar in sorted(closes.get(moment, ()), key=lambda b: b.symbol):
                pos = positions.get(bar.symbol)
                if pos is not None:
                    stop_hit = bar.low <= pos.stop
                    take_hit = bar.high >= pos.target
                    if stop_hit and take_hit:
                        ambiguous += 1
                    if stop_hit:
                        exit_position(bar.symbol, min(bar.open, pos.stop), moment, "stop")
                    elif take_hit:
                        exit_position(bar.symbol, pos.target, moment, "take_profit")
                    else:
                        pos.held_bars += 1
                        # High/ATR этого бара не могут изменить intrabar-исполнение.
                        pos.stop = max(
                            pos.stop,
                            floor_tick(
                                bar.high - self.policy.trailing_atr * pos.atr,
                                pos.instrument.tick_size,
                            ),
                        )
                marks[bar.symbol] = bar.close
                last_bars[bar.symbol] = bar
            for emission in emissions.get(moment, ()):
                if emission.symbol in latest and latest[emission.symbol].asof >= emission.asof:
                    raise ValueError("Повторный / неупорядоченный сигнал")
                latest[emission.symbol] = emission
            open_bars = opens.get(moment, ())
            for bar in open_bars:
                marks[bar.symbol] = bar.open
            check_daily_limit(moment)
            # Выходы всех бумаг до новых входов (освобождение капитала).
            exited_now: set[str] = set()
            for bar in sorted(open_bars, key=lambda b: b.symbol):
                pos = positions.get(bar.symbol)
                if pos is None:
                    continue
                signal = latest.get(bar.symbol)
                reason: str | None = None
                if bar.open <= pos.stop:
                    reason = "gap_stop"
                elif killed:
                    reason = "kill_switch"
                elif signal is not None and (
                    signal.up_given_trend < Decimal("0.5")
                    or signal.break_within_h >= self.policy.max_break_probability
                    or signal.regime is ProbabilityRegime.PANIC
                ):
                    reason = "signal_reversal"
                elif pos.held_bars >= self.policy.max_holding_bars:
                    reason = "timeout"
                if reason:
                    exit_position(bar.symbol, bar.open, moment, reason)
                    exited_now.add(bar.symbol)
            check_daily_limit(moment)
            ranked = sorted(
                open_bars,
                key=lambda b: (
                    -(latest[b.symbol].trend * latest[b.symbol].up_given_trend)
                    if b.symbol in latest
                    else ZERO,
                    b.symbol,
                ),
            )
            for bar in ranked:
                signal = latest.get(bar.symbol)
                if signal is None or signal.asof > bar.begin:
                    continue
                if consumed.get(bar.symbol) == signal.asof:
                    continue
                consumed[bar.symbol] = signal.asof
                if bar.symbol in positions or bar.symbol in exited_now:
                    continue
                # Не исполняем сигнал через пропущенные бары/несколько сессий.
                previous = last_bars.get(bar.symbol)
                if previous is None or signal.asof != previous.end:
                    rejections["stale_signal"] += 1
                    continue
                inst = self.instruments[bar.symbol]
                entry = execution_price(bar.open, inst, self.policy, buy=True)
                equity, _ = account_value()
                if equity <= ZERO:
                    continue
                exposure = [
                    Exposure(symbol=s, sector=p.instrument.sector, notional=marks[s] * p.units)
                    for s, p in positions.items()
                ]
                correlation = (correlations or {}).get(signal.asof, {})
                sizing = size_probability_position(
                    signal=signal,
                    instrument=inst,
                    entry_price=entry,
                    equity=equity,
                    cash=cash,
                    exposures=exposure,
                    correlations=correlation,
                    policy=self.policy,
                    killed=killed is not None,
                )
                if not sizing.lots:
                    rejections[sizing.reason] += 1
                    continue
                units = sizing.lots * inst.lot_size
                fee = entry * units * self.policy.commission_bps / BPS
                cash -= entry * units + fee
                positions[bar.symbol] = Position(
                    instrument=inst,
                    lots=sizing.lots,
                    entry=entry,
                    opened_at=moment,
                    entry_fee=fee,
                    stop=sizing.stop_price,
                    target=sizing.target_price,
                    atr=signal.atr,
                )
                check_daily_limit(moment)
            if closes.get(moment) or open_bars:
                value, gross = account_value()
                point = EquityPoint(timestamp=moment, equity=value, cash=cash, gross=gross)
                if equity_curve[-1].timestamp == moment:
                    equity_curve[-1] = point
                else:
                    equity_curve.append(point)
        # Явная ликвидация конца выборки с издержками (не невидимая открытая прибыль).
        for symbol in sorted(positions):
            bar = last_bars[symbol]
            exit_position(symbol, bar.close, bar.end, "end_of_sample")
        value, gross = account_value()
        equity_curve[-1] = EquityPoint(
            timestamp=equity_curve[-1].timestamp, equity=value, cash=cash, gross=gross
        )
        return ProbabilityBacktestResult(
            equity=tuple(equity_curve),
            trades=tuple(trades),
            rejections=dict(rejections),
            kill_reason=killed,
            ambiguous_execution_bars=ambiguous,
        )
