"""Фейковые реализации портов для тестов.

Это не «заглушки ради покрытия»: фейки — второй реализации портов, поэтому
контрактные тесты гоняют один и тот же набор проверок и против них, и против
настоящих адаптеров. Расхождение поведения фейка и адаптера означает ошибку
в одном из них.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from core.domain.catalog import DEFAULT_CATALOG_TYPES
from core.domain.entities import (
    Instrument,
    OrderResult,
    OrderState,
    PortfolioState,
    Position,
    StrategyConfig,
    TradePlan,
)
from core.domain.enums import OrderStatus, Timeframe, TradePlanStatus
from core.domain.value_objects import OHLCV, OrderbookSnapshot
from core.journal.hypothesis_engine import Hypothesis
from core.journal.snapshots import DecisionSnapshot, MarketSnapshot
from core.journal.trade_review import TradeReview
from core.ports.clock import FrozenClock
from core.ports.persistence import DecisionRecord, GuiAuditEntry, WsReplayEvent

ZERO = Decimal("0")


class FakeMarketData:
    """Рыночные данные из заранее подготовленных серий."""

    def __init__(
        self,
        candles: dict[tuple[str, Timeframe], Sequence[OHLCV]] | None = None,
        *,
        instruments: dict[tuple[str, str], Instrument] | None = None,
        catalog: Sequence[Any] | None = None,
        orderbook: OrderbookSnapshot | None = None,
        indicators: dict[tuple[str, str, Timeframe], dict[str, Decimal | None]] | None = None,
        raise_on_orderbook: bool = False,
    ) -> None:
        self._candles = candles or {}
        self._instruments = instruments or {}
        self._catalog: dict[str, Any] = {entry.uid: entry for entry in (catalog or ())}
        self._orderbook = orderbook
        self._indicators = indicators or {}
        self._raise_on_orderbook = raise_on_orderbook
        self.calls: list[str] = []

    def set_catalog(self, catalog: Sequence[Any]) -> None:
        """Задаёт справочник инструментов: эмуляция ответа InstrumentsService."""
        self._catalog = {entry.uid: entry for entry in catalog}
        self._instruments = {
            (entry.ticker, entry.class_code): entry.to_instrument() for entry in catalog
        }

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        from_: datetime,
        to: datetime,
    ) -> list[OHLCV]:
        self.calls.append(f"get_candles:{instrument.uid}:{timeframe.value}")
        series = self._candles.get((instrument.uid, timeframe), ())
        return [c for c in series if from_ <= c.timestamp <= to]

    async def stream_candles(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
    ) -> AsyncIterator[OHLCV]:
        for candle in self._candles.get((instrument.uid, timeframe), ()):
            yield candle

    async def get_orderbook(self, instrument: Instrument, depth: int = 20) -> OrderbookSnapshot:
        self.calls.append(f"get_orderbook:{instrument.uid}")
        if self._raise_on_orderbook or self._orderbook is None:
            msg = "стакан недоступен"
            raise RuntimeError(msg)
        return self._orderbook

    async def get_api_indicator(
        self,
        instrument: Instrument,
        indicator: str,
        timeframe: Timeframe,
        params: Mapping[str, Any],
    ) -> dict[str, Decimal | None]:
        values = self._indicators.get((instrument.uid, indicator, timeframe), {})
        return {
            name: Decimal(str(value)) if value is not None else None
            for name, value in values.items()
        }

    async def resolve_instrument(self, ticker: str, class_code: str) -> Instrument:
        key = (ticker, class_code)
        if key in self._instruments:
            return self._instruments[key]
        for entry in self._catalog.values():
            if entry.ticker == ticker and (not class_code or entry.class_code == class_code):
                return entry.to_instrument()
        msg = f"Инструмент {ticker}.{class_code} не найден"
        raise ValueError(msg)

    async def fetch_catalog(self, instrument_types: Any = None) -> list[Any]:
        """Эмуляция InstrumentsService: отдаёт записи только запрошенных типов."""
        requested = [item.strip().lower() for item in (instrument_types or ()) if item.strip()]
        if not requested:
            requested = list(DEFAULT_CATALOG_TYPES)
        self.calls.append(f"fetch_catalog:{','.join(requested)}")
        allowed = set(requested)
        return [entry for entry in self._catalog.values() if entry.instrument_type in allowed]

    async def search_instruments(
        self,
        query: str,
        *,
        instrument_type: str | None = None,
        limit: int = 20,
    ) -> list[Any]:
        self.calls.append(f"search_instruments:{query}")
        needle = query.strip().lower()
        return [
            entry
            for entry in self._catalog.values()
            if entry.matches(needle)
            and (instrument_type is None or entry.instrument_type == instrument_type)
        ][: max(limit, 1)]

    async def aclose(self) -> None:
        self.calls.clear()


class FakeBroker:
    """Исполнение «на месте»: мгновенный fill по цене плана."""

    def __init__(self, *, account_id: str = "fake-account") -> None:
        self.account_id = account_id
        self.placed: list[tuple[TradePlan, int]] = []
        self.cancelled: list[str] = []
        self.closed: list[tuple[Position, str]] = []
        self.positions: list[Position] = []
        self._counter = 0
        self._balance = Decimal("1000000")
        self._accounts: list[dict[str, Any]] = [
            {
                "id": account_id,
                "name": "Основной счёт в песочнице",
                "status": 2,
                "type": 1,
                "is_current": True,
            }
        ]
        if account_id != "test-account":
            self._accounts.append(
                {
                    "id": "test-account",
                    "name": "Тестовый счёт песочницы",
                    "status": 2,
                    "type": 1,
                    "is_current": False,
                }
            )

    async def get_portfolio(self) -> PortfolioState | None:
        return PortfolioState(
            account_id=self.account_id,
            total_value=self._balance,
            available_cash=self._balance,
            positions_value=Decimal("0"),
            updated_at=datetime.now(tz=UTC),
        )

    async def get_sandbox_accounts(self) -> list[dict[str, Any]]:
        for acc in self._accounts:
            acc["is_current"] = acc["id"] == self.account_id
        return list(self._accounts)

    async def get_accounts(self) -> list[dict[str, Any]]:
        return await self.get_sandbox_accounts()

    async def open_sandbox_account(self, name: str = "Счёт в песочнице") -> str:
        new_id = f"fake-sandbox-{len(self._accounts) + 1:02d}"
        self._accounts.append(
            {"id": new_id, "name": name, "status": 2, "type": 1, "is_current": False}
        )
        return new_id

    async def close_sandbox_account(self, account_id: str) -> None:
        self._accounts = [a for a in self._accounts if a["id"] != account_id]

    async def sandbox_pay_in(
        self, account_id: str, amount: Decimal, currency: str = "rub"
    ) -> Decimal:
        self._balance += amount
        return self._balance

    async def place_order(self, plan: TradePlan, quantity: int) -> OrderResult:
        self._counter += 1
        self.placed.append((plan, quantity))
        self.positions.append(
            Position(
                instrument=plan.instrument,
                quantity=quantity * plan.instrument.lot_size,
                average_entry=plan.entry_price,
                opened_at=datetime.now(tz=UTC),
                linked_plan_id=plan.id,
            )
        )
        return OrderResult(
            order_id=f"fake-{self._counter}",
            client_order_id=str(plan.id),
            status=OrderStatus.FILLED,
            filled_lots=quantity,
            filled_price=plan.entry_price,
            message="симулировано",
        )

    async def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)

    async def get_order_status(self, order_id: str) -> OrderState:
        return OrderState(order_id=order_id, status=OrderStatus.FILLED, filled_lots=1)

    async def close_position(self, position: Position, reason: str) -> OrderResult:
        self._counter += 1
        self.closed.append((position, reason))
        self.positions = [p for p in self.positions if p.linked_plan_id != position.linked_plan_id]
        return OrderResult(
            order_id=f"fake-close-{self._counter}",
            client_order_id=f"{position.linked_plan_id}-close",
            status=OrderStatus.FILLED,
            filled_lots=position.lots,
            filled_price=position.average_entry,
            message=reason,
        )

    async def get_open_positions(self) -> list[Position]:
        return list(self.positions)

    async def get_instrument(self, uid: str) -> Instrument | None:
        return Instrument(uid=uid, ticker=uid, lot_size=1)

    async def aclose(self) -> None:
        self.placed.clear()


class InMemoryRepository:
    """Репозиторий в памяти: та же семантика, что у DuckDB-версии."""

    def __init__(self) -> None:
        self.market_snapshots: dict[UUID, MarketSnapshot] = {}
        self.decision_snapshots: dict[UUID, DecisionSnapshot] = {}
        self.plans: dict[UUID, TradePlan] = {}
        self.reviews: dict[UUID, TradeReview] = {}
        self.hypotheses: dict[UUID, Hypothesis] = {}
        self.configs: dict[int, StrategyConfig] = {}
        self.instruments: dict[str, Instrument] = {}
        self.catalog: dict[str, Any] = {}
        self.portfolio_states: list[str] = []
        self.gui_audit: list[GuiAuditEntry] = []
        self.operational_values: dict[str, str] = {}
        self.ws_events: list[WsReplayEvent] = []

    async def save_market_snapshot(self, snapshot: MarketSnapshot) -> UUID:
        self.market_snapshots[snapshot.id] = snapshot
        return snapshot.id

    async def get_market_snapshot(self, snapshot_id: UUID) -> MarketSnapshot | None:
        return self.market_snapshots.get(snapshot_id)

    async def save_decision_snapshot(self, snapshot: DecisionSnapshot) -> UUID:
        self.decision_snapshots[snapshot.id] = snapshot
        return snapshot.id

    async def save_decision_snapshots_bulk(self, snapshots: Sequence[DecisionSnapshot]) -> None:
        for snapshot in snapshots:
            await self.save_decision_snapshot(snapshot)

    def _decision_record(self, item: DecisionSnapshot) -> DecisionRecord:
        return DecisionRecord(
            instrument_uid=self.market_snapshots[item.market_snapshot_id].instrument_uid
            if item.market_snapshot_id in self.market_snapshots
            else "",
            snapshot=item,
        )

    async def list_recent_decisions(self, limit: int = 50) -> list[DecisionRecord]:
        return [
            self._decision_record(item)
            for item in sorted(
                self.decision_snapshots.values(),
                key=lambda decision: decision.created_at,
                reverse=True,
            )[:limit]
        ]

    async def list_decisions_since(self, since: datetime) -> list[DecisionRecord]:
        return [
            self._decision_record(item)
            for item in sorted(
                self.decision_snapshots.values(),
                key=lambda decision: decision.created_at,
                reverse=True,
            )
            if item.created_at >= since
        ]

    async def save_trade_plan(self, plan: TradePlan) -> None:
        self.plans[plan.id] = plan

    async def get_trade_plan(self, plan_id: UUID) -> TradePlan | None:
        plan = self.plans.get(plan_id)
        # Как и в DuckDB: без инструмента в корзине план не материализуется.
        if plan is None or plan.instrument.uid not in self.instruments:
            return None
        return plan

    async def get_open_trade_plans(self) -> list[TradePlan]:
        # Как и в DuckDB: план без инструмента в корзине пропускаем, а не роняем
        # читателя — «сирота» не должна ломать мониторинг и дашборд.
        return [
            p for p in self.plans.values() if p.is_open and p.instrument.uid in self.instruments
        ]

    async def close_orphaned_trade_plans(self, reason: str) -> tuple[str, ...]:
        closed: list[str] = []
        for plan in self.plans.values():
            if not plan.is_open or plan.instrument.uid in self.instruments:
                continue
            plan.close(TradePlanStatus.CLOSED_MANUAL, closed_at=datetime.now(UTC))
            plan.rejection_reason = reason
            closed.append(str(plan.id))
        return tuple(closed)

    async def list_orphaned_trade_plan_ids(self) -> tuple[str, ...]:
        return tuple(
            str(plan.id)
            for plan in self.plans.values()
            if plan.is_open and plan.instrument.uid not in self.instruments
        )

    async def get_trade_history(
        self, instrument: Instrument | None, since: datetime
    ) -> list[TradeReview]:
        reviews = [r for r in self.reviews.values() if r.closed_at >= since]
        return sorted(reviews, key=lambda r: r.closed_at)

    async def save_trade_review(self, review: TradeReview) -> None:
        self.reviews[review.trade_plan_id] = review

    async def save_hypothesis(self, hypothesis: Hypothesis) -> None:
        self.hypotheses[hypothesis.id] = hypothesis

    async def list_hypotheses(self, status: str | None = None) -> list[Hypothesis]:
        items = list(self.hypotheses.values())
        if status:
            items = [h for h in items if h.status.value == status]
        return sorted(items, key=lambda h: h.confidence, reverse=True)

    async def save_strategy_config(self, config: StrategyConfig) -> None:
        self.configs[config.version] = config

    async def get_active_strategy_config(self) -> StrategyConfig | None:
        if not self.configs:
            return None
        return self.configs[max(self.configs)]

    async def save_instrument(self, instrument: Instrument) -> None:
        self.instruments[instrument.uid] = instrument

    async def delete_instrument(self, uid: str) -> None:
        self.instruments.pop(uid, None)

    async def list_instruments(self) -> list[Instrument]:
        return list(self.instruments.values())

    # ------------------------------------------------------------- каталог
    async def save_catalog_entries(self, entries: Sequence[Any]) -> None:
        # Момент сохранения проставляет хранилище — как и в DuckDB-версии.
        stamp = datetime.now(tz=UTC)
        for entry in entries:
            self.catalog[entry.uid] = replace(entry, updated_at=stamp)

    async def delete_catalog_entries(self, instrument_types: Sequence[str]) -> None:
        for uid, entry in list(self.catalog.items()):
            if entry.instrument_type in set(instrument_types):
                self.catalog.pop(uid, None)

    async def list_catalog_entries(
        self,
        *,
        query: str | None = None,
        instrument_types: Sequence[str] | None = None,
        tradable_only: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Any]:
        items = list(self.catalog.values())
        if query:
            items = [entry for entry in items if entry.matches(query)]
        if instrument_types:
            allowed = set(instrument_types)
            items = [entry for entry in items if entry.instrument_type in allowed]
        if tradable_only:
            items = [entry for entry in items if entry.tradable]
        items.sort(
            key=lambda entry: (
                not entry.api_trade_available,
                not entry.liquidity,
                entry.ticker,
            )
        )
        return items[offset : offset + max(limit, 1)]

    async def get_catalog_entry(self, uid: str) -> Any:
        return self.catalog.get(uid)

    async def find_catalog_entry(self, ticker: str, class_code: str | None = None) -> Any:
        symbol = ticker.strip().upper()
        for entry in self.catalog.values():
            if entry.ticker.upper() != symbol:
                continue
            if class_code and entry.class_code.upper() != class_code.strip().upper():
                continue
            return entry
        return None

    async def count_catalog_entries(self, instrument_types: Sequence[str] | None = None) -> int:
        if not instrument_types:
            return len(self.catalog)
        allowed = set(instrument_types)
        return sum(1 for entry in self.catalog.values() if entry.instrument_type in allowed)

    async def save_portfolio_state(self, state_json: str) -> None:
        self.portfolio_states.append(state_json)

    async def get_latest_portfolio_state(self) -> PortfolioState | None:
        if not self.portfolio_states:
            return None
        import json

        payload = json.loads(self.portfolio_states[-1])
        return PortfolioState(
            account_id=payload["account_id"],
            total_value=Decimal(payload["total_value"]),
            available_cash=Decimal(payload["available_cash"]),
            positions_value=Decimal(payload["positions_value"]),
            updated_at=datetime.fromisoformat(payload["updated_at"]),
        )

    async def execute_readonly(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        msg = "SQL-консоль требует DuckDB-репозиторий: фейк SQL не исполняет"
        raise NotImplementedError(msg)

    async def read_query(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        raise NotImplementedError("SQL-консоль требует DuckDB")

    async def append_gui_audit(self, entry: GuiAuditEntry) -> None:
        self.gui_audit.append(entry)

    async def list_gui_audit(
        self,
        *,
        section: str | None = None,
        action: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[GuiAuditEntry]:
        entries = [
            entry
            for entry in self.gui_audit
            if (section is None or entry.section == section)
            and (action is None or entry.action == action)
            and (since is None or entry.ts >= since)
        ]
        return list(reversed(entries))[:limit]

    async def get_operational_value(self, key: str) -> str | None:
        return self.operational_values.get(key)

    async def set_operational_value(self, key: str, value: str) -> None:
        self.operational_values[key] = value

    async def append_ws_event(self, entry: WsReplayEvent) -> None:
        self.ws_events.append(entry)

    async def list_ws_events(
        self, channel: str, since_seq: int, limit: int = 1000
    ) -> list[WsReplayEvent]:
        return [
            entry for entry in self.ws_events if entry.channel == channel and entry.seq > since_seq
        ][:limit]

    async def last_ws_seq(self, channel: str) -> int:
        return max((entry.seq for entry in self.ws_events if entry.channel == channel), default=0)

    async def set_memory_limit_mb(self, limit_mb: int) -> None:
        self.operational_values["duckdb_memory_limit_mb"] = str(limit_mb)

    async def memory_used_bytes(self) -> int | None:
        return None

    async def table_sizes(self) -> dict[str, int]:
        return {
            "instruments": len(self.instruments),
            "instrument_catalog": len(self.catalog),
            "candles": 0,
            "market_snapshots": len(self.market_snapshots),
            "decision_snapshots": len(self.decision_snapshots),
            "orderbook_snapshots": 0,
            "trade_plans": len(self.plans),
            "trades": len(self.reviews),
        }

    async def aclose(self) -> None:
        self.market_snapshots.clear()
        self.plans.clear()
        self.catalog.clear()


class FakeArchive:
    """Архив в памяти."""

    def __init__(self, *, usage: dict[str, int] | None = None) -> None:
        self.calls: list[str] = []
        self._usage = usage or {"hot": 1024, "warm": 2048, "cold": 4096}

    async def archive_snapshots(self, older_than: datetime) -> int:
        self.calls.append(f"archive:{older_than.isoformat()}")
        return 42

    async def compact_cold_archive(self) -> int:
        self.calls.append("compact")
        return 128

    async def usage_by_layer(self) -> dict[str, int]:
        return dict(self._usage)

    async def total_usage_bytes(self) -> int:
        return sum(self._usage.values())

    async def export_backup(self, destination: Path) -> Path:
        await asyncio.to_thread(destination.mkdir, parents=True, exist_ok=True)
        self.calls.append(f"export:{destination}")
        return destination

    async def restore_backup(self, source: Path) -> None:
        self.calls.append(f"restore:{source}")

    async def aclose(self) -> None:
        self.calls.clear()


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.critical: list[str] = []

    async def send(self, message: str, *, level: str = "info") -> None:
        self.messages.append((level, message))

    async def send_critical(self, message: str) -> None:
        self.critical.append(message)


# ------------------------------------------------------------------ генераторы
def make_candles(
    *,
    start: datetime,
    count: int,
    timeframe: Timeframe,
    base_price: Decimal = Decimal("100"),
    step: Decimal = Decimal("0.5"),
    volume: int = 1000,
) -> tuple[OHLCV, ...]:
    """Генерирует монотонно растущий ряд свечей."""
    candles: list[OHLCV] = []
    delta = {"1d": timedelta(days=1), "1h": timedelta(hours=1), "1m": timedelta(minutes=1)}[
        timeframe.value
    ]
    for i in range(count):
        price = base_price + step * Decimal(i)
        candles.append(
            OHLCV(
                open=price,
                high=price + Decimal("1"),
                low=price - Decimal("1"),
                close=price + step / 2,
                volume=volume + i,
                timestamp=start + delta * i,
                timeframe=timeframe,
            )
        )
    return tuple(candles)


def make_orderbook(
    *,
    mid: Decimal = Decimal("100"),
    spread: Decimal = Decimal("0.05"),
    levels: int = 5,
    bid_volume: int = 1000,
    ask_volume: int = 500,
    captured_at: datetime | None = None,
) -> OrderbookSnapshot:
    from core.domain.value_objects import OrderbookLevel

    captured = captured_at or datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    half = spread / 2
    bids = tuple(
        OrderbookLevel(price=mid - half - Decimal(i) * Decimal("0.01"), quantity=bid_volume)
        for i in range(levels)
    )
    asks = tuple(
        OrderbookLevel(price=mid + half + Decimal(i) * Decimal("0.01"), quantity=ask_volume)
        for i in range(levels)
    )
    return OrderbookSnapshot(bids=bids, asks=asks, captured_at=captured)


def make_config(**overrides: Any) -> StrategyConfig:
    from config.seed_defaults import DEFAULT_CONFLUENCE_WEIGHTS

    params: dict[str, Any] = {
        "version": 1,
        "risk_per_trade_pct": Decimal("0.01"),
        "min_viable_target_multiplier": Decimal("2"),
        "commission_rate": Decimal("0.003"),
        "max_holding_hours": 72,
        "max_position_notional": Decimal("1000000"),
        "confluence_threshold": Decimal("0.3"),
        "confluence_weights": dict(DEFAULT_CONFLUENCE_WEIGHTS),
    }
    params.update(overrides)
    return StrategyConfig(**params)


def make_clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 1, 10, 10, 0, tzinfo=UTC))


def make_instrument(
    *,
    uid: str = "uid-sber",
    ticker: str = "SBER",
    lot_size: int = 10,
    is_benchmark: bool = False,
) -> Instrument:
    return Instrument(
        uid=uid,
        ticker=ticker,
        lot_size=lot_size,
        is_benchmark=is_benchmark,
    )


def make_catalog_entry(
    *,
    uid: str = "uid-sber",
    ticker: str = "SBER",
    name: str = "Сбербанк",
    class_code: str = "TQBR",
    lot_size: int = 10,
    instrument_type: str = "share",
    currency: str = "RUB",
    isin: str = "",
    liquidity: bool = True,
    api_trade_available: bool = True,
) -> Any:
    """Запись каталога, какая приходит из InstrumentsService."""
    from core.domain.catalog import InstrumentCatalogEntry

    return InstrumentCatalogEntry(
        uid=uid,
        ticker=ticker,
        class_code=class_code,
        name=name,
        lot_size=lot_size,
        currency=currency,
        instrument_type=instrument_type,
        isin=isin,
        liquidity=liquidity,
        api_trade_available=api_trade_available,
    )
