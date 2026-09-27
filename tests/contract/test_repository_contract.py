"""Контрактные тесты ``RepositoryPort``: в памяти и на DuckDB.

Один набор проверок на обе реализации — это гарантия, что смена хранилища
не меняет семантику для ядра.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from core.domain.entities import (
    InvalidationRule,
    ReasoningStep,
    StrategyConfig,
    TradePlan,
    TradeThesis,
)
from core.domain.enums import Timeframe, TradePlanStatus, TradeVerdict
from core.journal.snapshots import DecisionSnapshot, MarketSnapshot
from core.journal.trade_review import TradeReview
from tests.fakes import InMemoryRepository, make_instrument

NOW = __import__("datetime").datetime(
    2026, 1, 10, 10, 0, tzinfo=__import__("datetime").timezone.utc
)


def _instrument() -> Any:
    return make_instrument()


def _plan(instrument: Any, *, status: TradePlanStatus = TradePlanStatus.ACTIVE) -> TradePlan:
    thesis = TradeThesis(
        reasoning_chain=(ReasoningStep(module="t", signal="s", weight=Decimal("1")),),
        confluence_score=Decimal("0.8"),
        timeframe_bias={Timeframe.D1: __import__("core.domain.enums", fromlist=["Trend"]).Trend.UP},
    )
    return TradePlan(
        id=uuid4(),
        instrument=instrument,
        entry_price=Decimal("100"),
        hard_stop_price=Decimal("95"),
        target_price=Decimal("120"),
        thesis=thesis,
        thesis_invalidation=InvalidationRule(
            description="тест", check=lambda s: False, code="noop"
        ),
        max_holding_time=timedelta(hours=72),
        created_at=NOW,
        status=status,
        quantity_lots=2,
    )


def _market_snapshot(instrument: Any) -> MarketSnapshot:
    snapshot = MarketSnapshot.create(instrument_uid=instrument.uid, captured_at=NOW)
    snapshot.indicators[Timeframe.H1] = {"atr": Decimal("2")}
    snapshot.signals[Timeframe.H1] = {"fibonacci": "in_golden_zone"}
    return snapshot


def _decision(market_id: Any, plan_id: Any) -> DecisionSnapshot:
    return DecisionSnapshot.create(
        market_snapshot_id=market_id,
        trade_plan_id=plan_id,
        decision=__import__("core.domain.enums", fromlist=["DecisionType"]).DecisionType.ENTER,
        reasoning_chain=(ReasoningStep(module="m", signal="s", weight=Decimal("1")),),
        confluence_score=Decimal("0.8"),
        risk_check_passed=True,
        thought_text="тестовая мысль",
        created_at=NOW,
    )


def _review(plan_id: Any) -> TradeReview:
    return TradeReview(
        trade_plan_id=plan_id,
        entry_price=Decimal("100"),
        exit_price=Decimal("110"),
        mfe=Decimal("0.2"),
        mae=Decimal("-0.05"),
        exit_efficiency=Decimal("0.5"),
        price_at_session_close=Decimal("112"),
        price_at_t_plus_1d=None,
        price_at_t_plus_3d=None,
        post_exit_drift_pct=Decimal("0.018"),
        verdict=TradeVerdict.GOOD_EXIT,
        closed_at=NOW,
        holding_seconds=3600,
        realized_pnl=Decimal("200"),
    )


@pytest.fixture(params=["memory", "duckdb"])
async def repository(request: pytest.FixtureRequest, tmp_path: Any) -> Any:
    if request.param == "memory":
        repo: Any = InMemoryRepository()
    else:
        from adapters.driven.storage.duckdb_repository import DuckDBRepository

        repo = DuckDBRepository(tmp_path / "contract.duckdb", memory_limit_mb=256, threads=1)
    yield repo
    await repo.aclose()


async def test_instrument_round_trip(repository: Any) -> None:
    instrument = _instrument()
    await repository.save_instrument(instrument)
    loaded = await repository.list_instruments()
    assert any(i.uid == instrument.uid for i in loaded)


def _catalog_entries() -> list[Any]:
    from tests.fakes import make_catalog_entry

    return [
        make_catalog_entry(uid="uid-sber", ticker="SBER", name="Сбербанк", lot_size=10),
        make_catalog_entry(uid="uid-vtbr", ticker="VTBR", name="Банк ВТБ", lot_size=10000),
        make_catalog_entry(
            uid="uid-usd",
            ticker="USD000UTSTOM",
            name="Доллар США",
            class_code="CETS",
            lot_size=1,
            instrument_type="currency",
            currency="USD",
            liquidity=False,
        ),
    ]


async def test_catalog_entries_round_trip(repository: Any) -> None:
    await repository.save_catalog_entries(_catalog_entries())

    saved = await repository.list_catalog_entries(limit=10)
    assert {entry.ticker for entry in saved} == {"SBER", "VTBR", "USD000UTSTOM"}

    sber = await repository.find_catalog_entry("SBER", "TQBR")
    assert sber is not None
    assert (sber.uid, sber.name, sber.lot_size, sber.currency) == (
        "uid-sber",
        "Сбербанк",
        10,
        "RUB",
    )
    assert sber.updated_at is not None
    assert (await repository.get_catalog_entry("uid-vtbr")).lot_size == 10000


async def test_catalog_requires_class_code_when_given(repository: Any) -> None:
    await repository.save_catalog_entries(_catalog_entries())

    assert await repository.find_catalog_entry("USD000UTSTOM", "CETS") is not None
    assert await repository.find_catalog_entry("USD000UTSTOM", "TQBR") is None
    assert await repository.find_catalog_entry("USD000UTSTOM") is not None


async def test_catalog_search_matches_ticker_and_name(repository: Any) -> None:
    await repository.save_catalog_entries(_catalog_entries())

    by_ticker = await repository.list_catalog_entries(query="sber", limit=10)
    by_name = await repository.list_catalog_entries(query="банк втб", limit=10)
    by_type = await repository.list_catalog_entries(instrument_types=["currency"], limit=10)
    tradable = await repository.list_catalog_entries(tradable_only=True, limit=10)

    assert [entry.ticker for entry in by_ticker] == ["SBER"]
    assert [entry.ticker for entry in by_name] == ["VTBR"]
    assert [entry.ticker for entry in by_type] == ["USD000UTSTOM"]
    assert {entry.ticker for entry in tradable} == {"SBER", "VTBR", "USD000UTSTOM"}
    assert await repository.count_catalog_entries(["share"]) == 2
    assert await repository.count_catalog_entries() == 3


async def test_catalog_delete_removes_only_given_types(repository: Any) -> None:
    await repository.save_catalog_entries(_catalog_entries())

    await repository.delete_catalog_entries(["share"])

    assert {entry.ticker for entry in await repository.list_catalog_entries(limit=10)} == {
        "USD000UTSTOM"
    }
    assert await repository.count_catalog_entries(["share"]) == 0


async def test_catalog_save_replaces_entry_by_uid(repository: Any) -> None:
    from tests.fakes import make_catalog_entry

    await repository.save_catalog_entries(_catalog_entries())
    await repository.save_catalog_entries(
        [make_catalog_entry(uid="uid-sber", ticker="SBER", name="Сбер Банк", lot_size=1)]
    )

    entries = await repository.list_catalog_entries(limit=10)
    assert len(entries) == 3
    sber = await repository.find_catalog_entry("SBER", "TQBR")
    assert sber is not None
    assert (sber.name, sber.lot_size) == ("Сбер Банк", 1)


async def test_trade_plan_round_trip(repository: Any) -> None:
    instrument = _instrument()
    await repository.save_instrument(instrument)
    plan = _plan(instrument)
    await repository.save_trade_plan(plan)

    loaded = await repository.get_trade_plan(plan.id)
    assert loaded is not None
    assert loaded.entry_price == plan.entry_price
    assert loaded.hard_stop_price == plan.hard_stop_price
    assert loaded.target_price == plan.target_price
    assert loaded.status is TradePlanStatus.ACTIVE


async def test_open_plans_exclude_closed(repository: Any) -> None:
    instrument = _instrument()
    await repository.save_instrument(instrument)
    opened = _plan(instrument)
    closed = _plan(instrument, status=TradePlanStatus.CLOSED_TARGET)
    await repository.save_trade_plan(opened)
    await repository.save_trade_plan(closed)

    open_ids = {p.id for p in await repository.get_open_trade_plans()}
    assert opened.id in open_ids
    assert closed.id not in open_ids


async def test_market_snapshot_round_trip(repository: Any) -> None:
    instrument = _instrument()
    await repository.save_instrument(instrument)
    snapshot = _market_snapshot(instrument)
    saved_id = await repository.save_market_snapshot(snapshot)

    loaded = await repository.get_market_snapshot(saved_id)
    assert loaded is not None
    assert loaded.instrument_uid == instrument.uid
    assert loaded.indicator(Timeframe.H1, "atr") == Decimal("2")
    assert loaded.signal_of("fibonacci", Timeframe.H1) == "in_golden_zone"


async def test_decision_snapshot_saved(repository: Any) -> None:
    instrument = _instrument()
    await repository.save_instrument(instrument)
    snapshot = _market_snapshot(instrument)
    market_id = await repository.save_market_snapshot(snapshot)
    decision = _decision(market_id, uuid4())
    saved_id = await repository.save_decision_snapshot(decision)
    assert saved_id == decision.id


async def test_trade_history_sorted_and_filtered(repository: Any) -> None:
    instrument = _instrument()
    await repository.save_instrument(instrument)
    plan = _plan(instrument)
    await repository.save_trade_plan(plan)
    await repository.save_trade_review(_review(plan.id))

    history = await repository.get_trade_history(None, since=NOW - timedelta(days=1))
    assert len(history) == 1
    assert history[0].verdict is TradeVerdict.GOOD_EXIT
    assert history[0].realized_pnl == Decimal("200")

    empty = await repository.get_trade_history(None, since=NOW + timedelta(days=1))
    assert empty == []


async def test_strategy_config_versioning(repository: Any) -> None:
    assert await repository.get_active_strategy_config() is None

    config = StrategyConfig(
        version=1,
        risk_per_trade_pct=Decimal("0.01"),
        min_viable_target_multiplier=Decimal("2"),
        commission_rate=Decimal("0.003"),
        max_holding_hours=72,
        max_position_notional=Decimal("500000"),
        confluence_threshold=Decimal("0.3"),
    )
    await repository.save_strategy_config(config)
    loaded = await repository.get_active_strategy_config()
    assert loaded is not None
    assert loaded.version == 1
    assert loaded.risk_per_trade_pct == Decimal("0.01")

    newer = StrategyConfig(
        version=2,
        risk_per_trade_pct=Decimal("0.02"),
        min_viable_target_multiplier=Decimal("2"),
        commission_rate=Decimal("0.003"),
        max_holding_hours=72,
        max_position_notional=Decimal("500000"),
        confluence_threshold=Decimal("0.3"),
    )
    await repository.save_strategy_config(newer)
    active = await repository.get_active_strategy_config()
    assert active is not None
    assert active.version == 2


async def test_hypotheses_round_trip(repository: Any) -> None:
    from core.journal.hypothesis_engine import Hypothesis

    hypothesis = Hypothesis.create(
        text="тестовая гипотеза",
        condition_description="x < 1",
        sample_size=40,
        confidence=Decimal("0.75"),
        suggested_action="сделать что-то",
        created_at=NOW,
    )
    await repository.save_hypothesis(hypothesis)
    items = await repository.list_hypotheses()
    assert any(h.text == "тестовая гипотеза" for h in items)

    confirmed = await repository.list_hypotheses(status="confirmed")
    assert confirmed == []


async def test_portfolio_state_persisted(repository: Any) -> None:
    await repository.save_portfolio_state('{"account_id": "x"}')
    # Контракт: запись не падает. Чтение — деталь реализации.
