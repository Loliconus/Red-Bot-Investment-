"""Порт персистентности.

Единый интерфейс для всех слоёв хранения: ядро не знает о DuckDB, Parquet и
разнице между hot/warm/cold — это детали адаптера. Единственное, что нужно
ядру, — гарантия персистентности через один интерфейс.

Важно: ядро **не сохраняет** снапшоты само, оно их только формирует. Вызов
``save_*`` — ответственность ``application/use_cases``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from core.domain.entities import Instrument, StrategyConfig, TradePlan
from core.journal.hypothesis_engine import Hypothesis
from core.journal.snapshots import DecisionSnapshot, MarketSnapshot
from core.journal.trade_review import TradeReview


@runtime_checkable
class RepositoryPort(Protocol):
    """Хранилище снапшотов, планов, сделок и гипотез."""

    async def save_market_snapshot(self, snapshot: MarketSnapshot) -> UUID: ...

    async def save_decision_snapshot(self, snapshot: DecisionSnapshot) -> UUID: ...

    async def save_trade_plan(self, plan: TradePlan) -> None: ...

    async def get_trade_plan(self, plan_id: UUID) -> TradePlan | None: ...

    async def get_open_trade_plans(self) -> list[TradePlan]: ...

    async def get_trade_history(
        self,
        instrument: Instrument | None,
        since: datetime,
    ) -> list[TradeReview]: ...

    async def save_trade_review(self, review: TradeReview) -> None: ...

    async def save_hypothesis(self, hypothesis: Hypothesis) -> None: ...

    async def list_hypotheses(self, status: str | None = None) -> list[Hypothesis]: ...

    async def save_strategy_config(self, config: StrategyConfig) -> None: ...

    async def get_active_strategy_config(self) -> StrategyConfig | None: ...

    async def save_instrument(self, instrument: Instrument) -> None: ...

    async def list_instruments(self) -> list[Instrument]: ...

    async def save_portfolio_state(self, state_json: str) -> None: ...

    async def get_market_snapshot(self, snapshot_id: UUID) -> MarketSnapshot | None: ...

    async def save_decision_snapshots_bulk(self, snapshots: Sequence[DecisionSnapshot]) -> None: ...

    async def execute_readonly(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        """Read-only SQL: консоль администратора и дата-майнинг.

        Реализация обязана отвергать любые мутирующие запросы.
        """
        ...

    async def table_sizes(self) -> dict[str, int]:
        """Количество строк по основным таблицам — для раздела администрирования."""
        ...
