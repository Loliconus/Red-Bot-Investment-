"""Реализация ``RepositoryPort`` на DuckDB.

Все обращения к БД выносятся в отдельный поток (``asyncio.to_thread``), чтобы
тяжёлые аналитические запросы не блокировали торговый event loop.

Снапшоты хранятся как JSON: структура у них вложенная и меняется от версии
к версии, а DuckDB умеет работать с JSON напрямую — это даёт дата-майнинг
без миграций схемы на каждое новое поле.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from adapters.driven.storage.connection_pool import DuckDBConnectionPool
from adapters.driven.storage.schema import INDEX_STATEMENTS, ddl_script, version_statement
from core.domain.catalog import InstrumentCatalogEntry
from core.domain.entities import (
    Instrument,
    InvalidationRule,
    PortfolioState,
    ReasoningStep,
    StrategyConfig,
    TradePlan,
    TradeThesis,
)
from core.domain.enums import DecisionType, MarketRegime, Timeframe, TradePlanStatus, Trend
from core.domain.value_objects import (
    OHLCV,
    CandleSeries,
    OrderbookLevel,
    OrderbookSnapshot,
)
from core.journal.hypothesis_engine import Hypothesis
from core.journal.snapshots import DecisionSnapshot, MarketSnapshot
from core.journal.trade_review import TradeReview
from core.ports.persistence import DecisionRecord, GuiAuditEntry, WsReplayEvent

logger = structlog.get_logger(__name__)

ZERO = Decimal("0")

#: Размер пачки при пакетном upsert каталога: тысячи записей в одном запросе
#: DuckDB не нужны, а список параметров не должен разрастаться.
_CATALOG_BATCH = 200


# ------------------------------------------------------------------ кодеки
def _default(obj: Any) -> str:
    """JSON-кодек для Decimal / datetime / UUID / Enum."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return str(obj.isoformat())
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, Enum):
        return str(obj.value)
    if isinstance(obj, timedelta):
        return str(obj.total_seconds())
    msg = f"Несериализуемый тип: {type(obj).__name__}"
    raise TypeError(msg)


def _to_json(payload: Any) -> str:
    return json.dumps(payload, default=_default, ensure_ascii=False)


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value))


#: Общий SELECT решений с тикером инструмента из market snapshot.
_DECISION_SELECT = (
    "SELECT d.id, d.market_snapshot_id, d.trade_plan_id, d.decision, "
    "d.confluence_score, d.reasoning, d.risk_check_passed, d.risk_check_reason, "
    "d.thought_text, d.created_at, m.instrument_uid "
    "FROM decision_snapshots d LEFT JOIN market_snapshots m ON m.id = d.market_snapshot_id"
)


def _row_to_decision_record(row: Any) -> DecisionRecord:
    return DecisionRecord(
        instrument_uid=row[10] or "",
        snapshot=DecisionSnapshot(
            id=UUID(row[0]),
            market_snapshot_id=UUID(row[1]),
            trade_plan_id=UUID(row[2]) if row[2] else None,
            decision=DecisionType(row[3]),
            confluence_score=_dec(row[4]),
            reasoning_chain=tuple(
                ReasoningStep(
                    module=step["module"],
                    signal=step["signal"],
                    weight=_dec(step["weight"]),
                    raw_value=_dec(step["raw_value"])
                    if step.get("raw_value") is not None
                    else None,
                    comment=step.get("comment", ""),
                )
                for step in json.loads(row[5])
            ),
            risk_check_passed=bool(row[6]),
            risk_check_reason=row[7],
            thought_text=row[8],
            created_at=_dt(row[9]),
        ),
    )


def _serialize_step(step: ReasoningStep) -> dict[str, Any]:
    """Явная сериализация: у dataclass со ``slots`` нет ``__dict__``."""
    return {
        "module": step.module,
        "signal": step.signal,
        "weight": str(step.weight),
        "raw_value": str(step.raw_value) if step.raw_value is not None else None,
        "comment": step.comment,
    }


def _serialize_thesis(thesis: TradeThesis) -> dict[str, Any]:
    return {
        "confluence_score": str(thesis.confluence_score),
        "summary": thesis.summary,
        "timeframe_bias": {tf.value: b.value for tf, b in thesis.timeframe_bias.items()},
        "reasoning_chain": [
            {
                "module": step.module,
                "signal": step.signal,
                "weight": str(step.weight),
                "raw_value": str(step.raw_value) if step.raw_value is not None else None,
                "comment": step.comment,
            }
            for step in thesis.reasoning_chain
        ],
    }


def _deserialize_thesis(payload: dict[str, Any]) -> TradeThesis:
    return TradeThesis(
        reasoning_chain=tuple(
            ReasoningStep(
                module=step["module"],
                signal=step["signal"],
                weight=_dec(step["weight"]),
                raw_value=_dec(step["raw_value"]) if step.get("raw_value") else None,
                comment=step.get("comment", ""),
            )
            for step in payload.get("reasoning_chain", [])
        ),
        confluence_score=_dec(payload.get("confluence_score", "0")),
        timeframe_bias={
            Timeframe(tf): Trend(bias) for tf, bias in payload.get("timeframe_bias", {}).items()
        },
        summary=payload.get("summary", ""),
    )


def _serialize_snapshot(snapshot: MarketSnapshot) -> dict[str, Any]:
    return {
        "id": str(snapshot.id),
        "instrument_uid": snapshot.instrument_uid,
        "captured_at": snapshot.captured_at.isoformat(),
        "ohlcv": {
            tf.value: {
                "open": str(c.open),
                "high": str(c.high),
                "low": str(c.low),
                "close": str(c.close),
                "volume": c.volume,
                "timestamp": c.timestamp.isoformat(),
            }
            for tf, c in snapshot.ohlcv.items()
        },
        "indicators": {
            tf.value: {k: str(v) for k, v in values.items()}
            for tf, values in snapshot.indicators.items()
        },
        "signals": {tf.value: dict(v) for tf, v in snapshot.signals.items()},
        "market_regime": {tf.value: r.value for tf, r in snapshot.market_regime.items()},
        "imoex_correlation": (
            str(snapshot.imoex_correlation) if snapshot.imoex_correlation is not None else None
        ),
        "orderbook": (
            {
                "captured_at": snapshot.orderbook.captured_at.isoformat(),
                "bids": [
                    {"price": str(level.price), "quantity": level.quantity}
                    for level in snapshot.orderbook.bids
                ],
                "asks": [
                    {"price": str(level.price), "quantity": level.quantity}
                    for level in snapshot.orderbook.asks
                ],
            }
            if snapshot.orderbook
            else None
        ),
    }


def _deserialize_snapshot(payload: dict[str, Any]) -> MarketSnapshot:
    ohlcv: dict[Timeframe, OHLCV] = {}
    for tf, raw in payload.get("ohlcv", {}).items():
        ohlcv[Timeframe(tf)] = OHLCV(
            open=_dec(raw["open"]),
            high=_dec(raw["high"]),
            low=_dec(raw["low"]),
            close=_dec(raw["close"]),
            volume=int(raw["volume"]),
            timestamp=_dt(raw["timestamp"]),
            timeframe=Timeframe(tf),
        )

    orderbook = None
    if payload.get("orderbook"):
        raw_ob = payload["orderbook"]
        orderbook = OrderbookSnapshot(
            bids=tuple(
                OrderbookLevel(price=_dec(level["price"]), quantity=int(level["quantity"]))
                for level in raw_ob.get("bids", [])
            ),
            asks=tuple(
                OrderbookLevel(price=_dec(level["price"]), quantity=int(level["quantity"]))
                for level in raw_ob.get("asks", [])
            ),
            captured_at=_dt(raw_ob["captured_at"]),
        )

    return MarketSnapshot(
        id=UUID(payload["id"]),
        instrument_uid=payload["instrument_uid"],
        captured_at=_dt(payload["captured_at"]),
        ohlcv=ohlcv,
        candles={tf: CandleSeries(timeframe=tf, candles=(c,)) for tf, c in ohlcv.items()},
        indicators={
            Timeframe(tf): {k: _dec(v) for k, v in values.items()}
            for tf, values in payload.get("indicators", {}).items()
        },
        signals={Timeframe(tf): dict(v) for tf, v in payload.get("signals", {}).items()},
        orderbook=orderbook,
        imoex_correlation=(
            _dec(payload["imoex_correlation"])
            if payload.get("imoex_correlation") is not None
            else None
        ),
        market_regime={
            Timeframe(tf): MarketRegime(r) for tf, r in payload.get("market_regime", {}).items()
        },
    )


class DuckDBRepository:
    """Репозиторий на файловой DuckDB."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        memory_limit_mb: int = 1536,
        threads: int = 2,
        pool: DuckDBConnectionPool | None = None,
    ) -> None:
        self._pool = pool or DuckDBConnectionPool(
            Path(db_path), memory_limit_mb=memory_limit_mb, threads=threads
        )
        self._init_schema()

    # ---------------------------------------------------------- жизненный цикл
    def _init_schema(self) -> None:
        self._pool.execute_script(ddl_script())
        for statement in INDEX_STATEMENTS:
            self._pool.execute_script(statement + ";")
        self._pool.execute_script(version_statement() + ";")

    async def aclose(self) -> None:
        await asyncio.to_thread(self._pool.close)

    def _run(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        return self._pool.execute(sql, params)

    async def _arun(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        return await asyncio.to_thread(self._run, sql, params)

    # ------------------------------------------------------------- снапшоты
    async def save_market_snapshot(self, snapshot: MarketSnapshot) -> UUID:
        payload = _serialize_snapshot(snapshot)
        await self._arun(
            "INSERT OR REPLACE INTO market_snapshots (id, instrument_uid, captured_at, payload) "
            "VALUES (?, ?, ?, ?)",
            [str(snapshot.id), snapshot.instrument_uid, snapshot.captured_at, _to_json(payload)],
        )
        return snapshot.id

    async def get_market_snapshot(self, snapshot_id: UUID) -> MarketSnapshot | None:
        rows = await self._arun(
            "SELECT payload FROM market_snapshots WHERE id = ?", [str(snapshot_id)]
        )
        if not rows:
            return None
        return _deserialize_snapshot(json.loads(rows[0][0]))

    async def save_decision_snapshot(self, snapshot: DecisionSnapshot) -> UUID:
        await self._arun(
            "INSERT OR REPLACE INTO decision_snapshots "
            "(id, market_snapshot_id, trade_plan_id, decision, confluence_score, reasoning, "
            " risk_check_passed, risk_check_reason, thought_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(snapshot.id),
                str(snapshot.market_snapshot_id),
                str(snapshot.trade_plan_id) if snapshot.trade_plan_id else None,
                snapshot.decision.value,
                snapshot.confluence_score,
                _to_json([_serialize_step(step) for step in snapshot.reasoning_chain]),
                snapshot.risk_check_passed,
                snapshot.risk_check_reason,
                snapshot.thought_text,
                snapshot.created_at,
            ],
        )
        return snapshot.id

    async def list_recent_decisions(self, limit: int = 50) -> list[DecisionRecord]:
        rows = await self._arun(
            f"{_DECISION_SELECT} ORDER BY d.created_at DESC LIMIT ?",
            [min(max(limit, 1), 500)],
        )
        return [_row_to_decision_record(row) for row in rows]

    async def list_decisions_since(self, since: datetime) -> list[DecisionRecord]:
        rows = await self._arun(
            f"{_DECISION_SELECT} WHERE d.created_at >= ? ORDER BY d.created_at DESC LIMIT 5000",
            [since],
        )
        return [_row_to_decision_record(row) for row in rows]

    async def save_decision_snapshots_bulk(self, snapshots: Any) -> None:
        for snapshot in snapshots:
            await self.save_decision_snapshot(snapshot)

    # ------------------------------------------------------------- планы
    async def save_trade_plan(self, plan: TradePlan) -> None:
        await self._arun(
            "INSERT OR REPLACE INTO trade_plans "
            "(id, instrument_uid, status, entry_price, hard_stop_price, target_price, thesis, "
            " invalidation, max_holding_seconds, quantity_lots, created_at, closed_at, "
            " rejection_reason, config_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(plan.id),
                plan.instrument.uid,
                plan.status.value,
                plan.entry_price,
                plan.hard_stop_price,
                plan.target_price,
                _to_json(_serialize_thesis(plan.thesis)),
                _to_json(
                    {
                        "description": plan.thesis_invalidation.description,
                        "code": plan.thesis_invalidation.code,
                    }
                ),
                int(plan.max_holding_time.total_seconds()),
                plan.quantity_lots,
                plan.created_at,
                plan.closed_at,
                plan.rejection_reason,
                None,
            ],
        )

    async def get_trade_plan(self, plan_id: UUID) -> TradePlan | None:
        rows = await self._arun("SELECT * FROM trade_plans WHERE id = ?", [str(plan_id)])
        if not rows:
            return None
        return await self._plan_from_row(rows[0])

    async def get_open_trade_plans(self) -> list[TradePlan]:
        rows = await self._arun(
            "SELECT * FROM trade_plans "
            f"WHERE status IN ({self._open_plan_statuses()}) ORDER BY created_at"
        )
        # План, чей инструмент удалён из корзины, пропускаем: один такой
        # «сирота» (например, запись от бэктеста со старым идентификатором)
        # не должен ронять ни мониторинг позиций, ни дашборд.
        plans: list[TradePlan] = []
        for row in rows:
            plan = await self._plan_from_row(row)
            if plan is not None:
                plans.append(plan)
        return plans

    @staticmethod
    def _open_plan_statuses() -> str:
        return ", ".join(
            f"'{s.value}'"
            for s in (
                TradePlanStatus.PROPOSED,
                TradePlanStatus.PENDING,
                TradePlanStatus.ACTIVE,
            )
        )

    async def list_orphaned_trade_plan_ids(self) -> tuple[str, ...]:
        """Id открытых планов, чей инструмент отсутствует в корзине."""
        rows = await self._arun(
            "SELECT id FROM trade_plans "
            f"WHERE status IN ({self._open_plan_statuses()}) "
            "AND instrument_uid NOT IN (SELECT uid FROM instruments)"
        )
        return tuple(str(row[0]) for row in rows)

    async def close_orphaned_trade_plans(self, reason: str) -> tuple[str, ...]:
        """Закрывает открытые планы без инструмента в корзине. Возвращает их id.

        План без инструмента нельзя ни исполнить, ни промониторить: нет UID
        для заявки и свечей. Поэтому такие планы закрываются явной причиной,
        а не молча ломают чтение всех остальных.
        """
        plan_ids = await self.list_orphaned_trade_plan_ids()
        if not plan_ids:
            return ()
        placeholders = ", ".join("?" for _ in plan_ids)
        await self._arun(
            "UPDATE trade_plans "
            "SET status = ?, closed_at = now(), rejection_reason = ? "
            f"WHERE id IN ({placeholders})",
            [TradePlanStatus.CLOSED_MANUAL.value, reason, *plan_ids],
        )
        return plan_ids

    async def _plan_from_row(self, row: tuple[Any, ...]) -> TradePlan | None:
        instrument = await self.get_instrument(row[1])
        if instrument is None:
            # Инструмент мог быть удалён из корзины или прийти из другого
            # контура (бэктест со старым идентификатором). Читатель обязан
            # деградировать предупреждением, а не падать ValueError.
            logger.warning(
                "trade_plan_instrument_missing",
                plan_id=str(row[0]),
                instrument_uid=str(row[1]),
            )
            return None

        invalidation_payload = json.loads(row[7])
        plan = TradePlan(
            id=UUID(row[0]),
            instrument=instrument,
            entry_price=_dec(row[3]),
            hard_stop_price=_dec(row[4]),
            target_price=_dec(row[5]),
            thesis=_deserialize_thesis(json.loads(row[6])),
            # Правило инвалидации — вызываемый объект и в БД не хранится:
            # при загрузке подставляется заглушка, реальные правила строятся
            # стратегией при создании плана.
            thesis_invalidation=InvalidationRule(
                description=invalidation_payload.get("description", ""),
                code=invalidation_payload.get("code", "restored"),
                check=lambda snapshot: False,
            ),
            max_holding_time=timedelta(seconds=int(row[8])),
            created_at=_dt(row[10]),
            closed_at=_dt(row[11]) if row[11] else None,
            status=TradePlanStatus(row[2]),
            quantity_lots=int(row[9] or 0),
            rejection_reason=row[12],
        )
        return plan

    # ------------------------------------------------------------- сделки
    async def save_trade_review(self, review: TradeReview) -> None:
        await self._arun(
            "INSERT OR REPLACE INTO trades (trade_plan_id, entry_price, exit_price, mfe, mae, "
            " exit_efficiency, price_at_session_close, price_at_t_plus_1d, price_at_t_plus_3d, "
            " post_exit_drift_pct, verdict, holding_seconds, realized_pnl, closed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(review.trade_plan_id),
                review.entry_price,
                review.exit_price,
                review.mfe,
                review.mae,
                review.exit_efficiency,
                review.price_at_session_close,
                review.price_at_t_plus_1d,
                review.price_at_t_plus_3d,
                review.post_exit_drift_pct,
                review.verdict.value,
                review.holding_seconds,
                review.realized_pnl,
                review.closed_at,
            ],
        )

    async def get_trade_history(
        self,
        instrument: Instrument | None,
        since: datetime,
    ) -> list[TradeReview]:
        if instrument is not None:
            rows = await self._arun(
                "SELECT t.* FROM trades t JOIN trade_plans p ON p.id = t.trade_plan_id "
                "WHERE p.instrument_uid = ? AND t.closed_at >= ? ORDER BY t.closed_at",
                [instrument.uid, since],
            )
        else:
            rows = await self._arun(
                "SELECT * FROM trades WHERE closed_at >= ? ORDER BY closed_at", [since]
            )
        return [self._review_from_row(row) for row in rows]

    def _review_from_row(self, row: tuple[Any, ...]) -> TradeReview:
        from core.domain.enums import TradeVerdict

        return TradeReview(
            trade_plan_id=UUID(row[0]),
            entry_price=_dec(row[1]),
            exit_price=_dec(row[2]),
            mfe=_dec(row[3]),
            mae=_dec(row[4]),
            exit_efficiency=_dec(row[5]),
            price_at_session_close=_dec(row[6]),
            price_at_t_plus_1d=_dec(row[7]) if row[7] is not None else None,
            price_at_t_plus_3d=_dec(row[8]) if row[8] is not None else None,
            post_exit_drift_pct=_dec(row[9]),
            verdict=TradeVerdict(row[10]),
            holding_seconds=int(row[11] or 0),
            realized_pnl=_dec(row[12] or 0),
            closed_at=_dt(row[13]),
        )

    # ------------------------------------------------------------- гипотезы
    async def save_hypothesis(self, hypothesis: Hypothesis) -> None:
        await self._arun(
            "INSERT OR REPLACE INTO hypotheses (id, text, condition_description, sample_size, "
            " confidence, status, suggested_action, walk_forward_efficiency, evidence, "
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(hypothesis.id),
                hypothesis.text,
                hypothesis.condition_description,
                hypothesis.sample_size,
                hypothesis.confidence,
                hypothesis.status.value,
                hypothesis.suggested_action,
                hypothesis.walk_forward_efficiency,
                _to_json(hypothesis.evidence),
                hypothesis.created_at,
            ],
        )

    async def list_hypotheses(self, status: str | None = None) -> list[Hypothesis]:
        if status:
            rows = await self._arun(
                "SELECT * FROM hypotheses WHERE status = ? ORDER BY confidence DESC", [status]
            )
        else:
            rows = await self._arun("SELECT * FROM hypotheses ORDER BY confidence DESC")
        return [self._hypothesis_from_row(row) for row in rows]

    def _hypothesis_from_row(self, row: tuple[Any, ...]) -> Hypothesis:
        from core.domain.enums import HypothesisStatus

        return Hypothesis(
            id=UUID(row[0]),
            text=row[1],
            condition_description=row[2],
            sample_size=int(row[3]),
            confidence=_dec(row[4]),
            status=HypothesisStatus(row[5]),
            suggested_action=row[6],
            walk_forward_efficiency=_dec(row[7]) if row[7] is not None else None,
            evidence=json.loads(row[8]) if row[8] else {},
            created_at=_dt(row[9]) if row[9] else None,
        )

    # ------------------------------------------------------------- конфиг
    async def save_strategy_config(self, config: StrategyConfig) -> None:
        payload = {
            "risk_per_trade_pct": str(config.risk_per_trade_pct),
            "min_viable_target_multiplier": str(config.min_viable_target_multiplier),
            "commission_rate": str(config.commission_rate),
            "max_holding_hours": config.max_holding_hours,
            "max_position_notional": str(config.max_position_notional),
            "confluence_threshold": str(config.confluence_threshold),
            "confluence_weights": {k: str(v) for k, v in config.confluence_weights.items()},
            "allow_counter_trend": config.allow_counter_trend,
            "daily_loss_limit_pct": str(config.daily_loss_limit_pct),
        }
        await self._arun(
            "INSERT OR REPLACE INTO strategy_configs (version, payload, created_at) "
            "VALUES (?, ?, ?)",
            [config.version, _to_json(payload), datetime.now(tz=UTC)],
        )

    async def get_active_strategy_config(self) -> StrategyConfig | None:
        rows = await self._arun(
            "SELECT version, payload FROM strategy_configs ORDER BY version DESC LIMIT 1"
        )
        if not rows:
            return None
        return self._config_from_row(rows[0])

    def _config_from_row(self, row: tuple[Any, ...]) -> StrategyConfig:
        payload = json.loads(row[1])
        return StrategyConfig(
            version=int(row[0]),
            risk_per_trade_pct=_dec(payload["risk_per_trade_pct"]),
            min_viable_target_multiplier=_dec(payload["min_viable_target_multiplier"]),
            commission_rate=_dec(payload["commission_rate"]),
            max_holding_hours=int(payload["max_holding_hours"]),
            max_position_notional=_dec(payload["max_position_notional"]),
            confluence_threshold=_dec(payload["confluence_threshold"]),
            confluence_weights={
                k: _dec(v) for k, v in payload.get("confluence_weights", {}).items()
            },
            allow_counter_trend=bool(payload.get("allow_counter_trend", False)),
            daily_loss_limit_pct=_dec(payload.get("daily_loss_limit_pct", "0.03")),
        )

    # ------------------------------------------------------------- инструменты
    async def save_instrument(self, instrument: Instrument) -> None:
        await self._arun(
            "INSERT OR REPLACE INTO instruments "
            "(uid, ticker, class_code, lot_size, is_benchmark, currency) VALUES (?, ?, ?, ?, ?, ?)",
            [
                instrument.uid,
                instrument.ticker,
                instrument.class_code,
                instrument.lot_size,
                instrument.is_benchmark,
                instrument.currency,
            ],
        )

    async def delete_instrument(self, uid: str) -> None:
        await self._arun("DELETE FROM instruments WHERE uid = ?", [uid])

    async def list_instruments(self) -> list[Instrument]:
        rows = await self._arun(
            "SELECT uid, ticker, class_code, lot_size, is_benchmark, currency "
            "FROM instruments ORDER BY ticker"
        )
        return [
            Instrument(
                uid=row[0],
                ticker=row[1],
                class_code=row[2],
                lot_size=int(row[3]),
                is_benchmark=bool(row[4]),
                currency=row[5],
            )
            for row in rows
        ]

    async def get_instrument(self, uid: str) -> Instrument | None:
        rows = await self._arun(
            "SELECT uid, ticker, class_code, lot_size, is_benchmark, currency "
            "FROM instruments WHERE uid = ?",
            [uid],
        )
        if not rows:
            return None
        row = rows[0]
        return Instrument(
            uid=row[0],
            ticker=row[1],
            class_code=row[2],
            lot_size=int(row[3]),
            is_benchmark=bool(row[4]),
            currency=row[5],
        )

    # ------------------------------------------------------------- каталог
    #: Колонки таблицы ``instrument_catalog`` в порядке вставки и чтения.
    _CATALOG_COLUMNS: tuple[str, ...] = (
        "uid",
        "ticker",
        "class_code",
        "name",
        "lot_size",
        "currency",
        "instrument_type",
        "isin",
        "figi",
        "api_trade_available",
        "buy_available",
        "sell_available",
        "for_iis",
        "for_qual_investor",
        "exchange",
        "sector",
        "country_of_risk",
        "liquidity_flag",
        "min_price_increment",
        "updated_at",
    )

    @staticmethod
    def _catalog_values(entry: InstrumentCatalogEntry, updated_at: datetime) -> list[Any]:
        return [
            entry.uid,
            entry.ticker,
            entry.class_code,
            entry.name,
            entry.lot_size,
            entry.currency,
            entry.instrument_type,
            entry.isin,
            entry.figi,
            entry.api_trade_available,
            entry.buy_available,
            entry.sell_available,
            entry.for_iis,
            entry.for_qual_investor,
            entry.exchange,
            entry.sector,
            entry.country_of_risk,
            entry.liquidity,
            entry.min_price_increment,
            updated_at,
        ]

    @classmethod
    def _catalog_from_row(cls, row: tuple[Any, ...]) -> InstrumentCatalogEntry:
        values = dict(zip(cls._CATALOG_COLUMNS, row, strict=True))
        increment = values["min_price_increment"]
        return InstrumentCatalogEntry(
            uid=str(values["uid"]),
            ticker=str(values["ticker"]),
            class_code=str(values["class_code"]),
            name=str(values["name"] or ""),
            lot_size=int(values["lot_size"]),
            currency=str(values["currency"] or "RUB"),
            instrument_type=str(values["instrument_type"] or "share"),
            isin=str(values["isin"] or ""),
            figi=str(values["figi"] or ""),
            api_trade_available=bool(values["api_trade_available"]),
            buy_available=bool(values["buy_available"]),
            sell_available=bool(values["sell_available"]),
            for_iis=bool(values["for_iis"]),
            for_qual_investor=bool(values["for_qual_investor"]),
            exchange=str(values["exchange"] or ""),
            sector=str(values["sector"] or ""),
            country_of_risk=str(values["country_of_risk"] or ""),
            liquidity=bool(values["liquidity_flag"]),
            min_price_increment=_dec(increment) if increment is not None else None,
            updated_at=_dt(values["updated_at"]) if values["updated_at"] else None,
        )

    async def save_catalog_entries(self, entries: Sequence[InstrumentCatalogEntry]) -> None:
        """Пакетный upsert: каталог из API приходит пачкой на сотни записей."""
        if not entries:
            return
        updated_at = datetime.now(tz=UTC)
        columns = ", ".join(self._CATALOG_COLUMNS)
        row_placeholder = "(" + ", ".join("?" * len(self._CATALOG_COLUMNS)) + ")"
        for start in range(0, len(entries), _CATALOG_BATCH):
            chunk = entries[start : start + _CATALOG_BATCH]
            placeholders = ", ".join(row_placeholder for _ in chunk)
            params: list[Any] = []
            for entry in chunk:
                params.extend(self._catalog_values(entry, updated_at))
            await self._arun(
                f"INSERT OR REPLACE INTO instrument_catalog ({columns}) VALUES {placeholders}",
                params,
            )

    async def delete_catalog_entries(self, instrument_types: Sequence[str]) -> None:
        if not instrument_types:
            return
        placeholders = ", ".join("?" for _ in instrument_types)
        await self._arun(
            f"DELETE FROM instrument_catalog WHERE instrument_type IN ({placeholders})",
            list(instrument_types),
        )

    def _catalog_where(
        self,
        *,
        query: str | None,
        instrument_types: Sequence[str] | None,
        tradable_only: bool,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            needle = f"%{query.strip().upper()}%"
            clauses.append(
                "(UPPER(ticker) LIKE ? OR UPPER(name) LIKE ? OR UPPER(isin) LIKE ? "
                "OR UPPER(figi) LIKE ? OR UPPER(uid) LIKE ?)"
            )
            params.extend([needle] * 5)
        if instrument_types:
            placeholders = ", ".join("?" for _ in instrument_types)
            clauses.append(f"instrument_type IN ({placeholders})")
            params.extend(instrument_types)
        if tradable_only:
            clauses.append("api_trade_available = TRUE AND buy_available = TRUE")
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    async def list_catalog_entries(
        self,
        *,
        query: str | None = None,
        instrument_types: Sequence[str] | None = None,
        tradable_only: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> list[InstrumentCatalogEntry]:
        clause, params = self._catalog_where(
            query=query,
            instrument_types=instrument_types,
            tradable_only=tradable_only,
        )
        columns = ", ".join(self._CATALOG_COLUMNS)
        rows = await self._arun(
            f"SELECT {columns} FROM instrument_catalog{clause} "
            "ORDER BY api_trade_available DESC, liquidity_flag DESC, ticker "
            "LIMIT ? OFFSET ?",
            [*params, min(max(limit, 1), 1000), max(offset, 0)],
        )
        return [self._catalog_from_row(row) for row in rows]

    async def get_catalog_entry(self, uid: str) -> InstrumentCatalogEntry | None:
        columns = ", ".join(self._CATALOG_COLUMNS)
        rows = await self._arun(f"SELECT {columns} FROM instrument_catalog WHERE uid = ?", [uid])
        return self._catalog_from_row(rows[0]) if rows else None

    async def find_catalog_entry(
        self, ticker: str, class_code: str | None = None
    ) -> InstrumentCatalogEntry | None:
        """Точный поиск: класс-код обязателен, если задан — иначе неоднозначность."""
        symbol = ticker.strip().upper()
        columns = ", ".join(self._CATALOG_COLUMNS)
        if class_code:
            rows = await self._arun(
                f"SELECT {columns} FROM instrument_catalog WHERE ticker = ? AND class_code = ? "
                "LIMIT 1",
                [symbol, class_code.strip().upper()],
            )
        else:
            rows = await self._arun(
                f"SELECT {columns} FROM instrument_catalog WHERE ticker = ? LIMIT 1", [symbol]
            )
        return self._catalog_from_row(rows[0]) if rows else None

    async def count_catalog_entries(self, instrument_types: Sequence[str] | None = None) -> int:
        clause, params = self._catalog_where(
            query=None, instrument_types=instrument_types, tradable_only=False
        )
        rows = await self._arun(f"SELECT count(*) FROM instrument_catalog{clause}", params)
        return int(rows[0][0]) if rows else 0

    # ------------------------------------------------------------- портфель
    async def save_portfolio_state(self, state_json: str) -> None:
        await self._arun(
            "INSERT INTO portfolio_states (id, payload, updated_at) VALUES (?, ?, ?)",
            [str(datetime.now(tz=UTC).timestamp()), state_json, datetime.now(tz=UTC)],
        )

    async def get_latest_portfolio_state(self) -> PortfolioState | None:
        rows = await self._arun(
            "SELECT payload FROM portfolio_states ORDER BY updated_at DESC LIMIT 1"
        )
        if not rows:
            return None
        payload = json.loads(rows[0][0])
        return PortfolioState(
            account_id=payload["account_id"],
            total_value=_dec(payload["total_value"]),
            available_cash=_dec(payload["available_cash"]),
            positions_value=_dec(payload["positions_value"]),
            updated_at=_dt(payload["updated_at"]),
            daily_pnl=_dec(payload.get("daily_pnl", "0")),
        )

    # ------------------------------------------------------------- сервисное
    async def save_candles(self, series: CandleSeries, instrument_uid: str) -> int:
        """Пакетная запись свечей. Возвращает количество записанных строк."""
        for candle in series.candles:
            await self._arun(
                "INSERT OR REPLACE INTO candles "
                "(instrument_uid, timeframe, ts, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    instrument_uid,
                    series.timeframe.value,
                    candle.timestamp,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                ],
            )
        return len(series.candles)

    async def get_candles(
        self, instrument_uid: str, timeframe: Timeframe, since: datetime, until: datetime
    ) -> CandleSeries:
        rows = await self._arun(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE instrument_uid = ? AND timeframe = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            [instrument_uid, timeframe.value, since, until],
        )
        candles = tuple(
            OHLCV(
                open=_dec(row[1]),
                high=_dec(row[2]),
                low=_dec(row[3]),
                close=_dec(row[4]),
                volume=int(row[5]),
                timestamp=_dt(row[0]),
                timeframe=timeframe,
            )
            for row in rows
        )
        return CandleSeries(timeframe=timeframe, candles=candles)

    async def execute_readonly(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        """Read-only SQL для консоли администратора и дата-майнинга.

        Мутирующие запросы отвергаются на уровне пула подключений.
        """
        return await asyncio.to_thread(self._pool.query_readonly, sql, params)

    async def read_query(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        return await asyncio.to_thread(self._pool.query_readonly_with_columns, sql)

    async def append_gui_audit(self, entry: GuiAuditEntry) -> None:
        await self._arun(
            "INSERT INTO gui_audit (id, ts, section, action, before_value, after_value, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                str(entry.id),
                entry.ts,
                entry.section,
                entry.action,
                _to_json(entry.before),
                _to_json(entry.after),
                entry.outcome,
            ],
        )

    async def list_gui_audit(
        self,
        *,
        section: str | None = None,
        action: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[GuiAuditEntry]:
        where = []
        params: list[Any] = []
        if section:
            where.append("section = ?")
            params.append(section)
        if action:
            where.append("action = ?")
            params.append(action)
        if since:
            where.append("ts >= ?")
            params.append(since)
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = await self._arun(
            "SELECT id, ts, section, action, before_value, after_value, outcome "
            f"FROM gui_audit{clause} ORDER BY ts DESC LIMIT ?",
            [*params, min(max(limit, 1), 500)],
        )
        return [
            GuiAuditEntry(
                id=UUID(row[0]),
                ts=_dt(row[1]),
                section=row[2],
                action=row[3],
                before=json.loads(row[4]),
                after=json.loads(row[5]),
                outcome=row[6],
            )
            for row in rows
        ]

    async def get_operational_value(self, key: str) -> str | None:
        rows = await self._arun("SELECT value FROM operational_settings WHERE key = ?", [key])
        return str(rows[0][0]) if rows else None

    async def set_operational_value(self, key: str, value: str) -> None:
        await self._arun(
            "INSERT OR REPLACE INTO operational_settings (key, value, updated_at) VALUES (?, ?, ?)",
            [key, value, datetime.now(tz=UTC)],
        )

    async def append_ws_event(self, entry: WsReplayEvent) -> None:
        await self._arun(
            "INSERT INTO ws_replay (channel, seq, ts, event_type, payload) VALUES (?, ?, ?, ?, ?)",
            [entry.channel, entry.seq, entry.ts, entry.event_type, _to_json(entry.payload)],
        )

    async def list_ws_events(
        self, channel: str, since_seq: int, limit: int = 1000
    ) -> list[WsReplayEvent]:
        rows = await self._arun(
            "SELECT seq, ts, event_type, payload FROM ws_replay "
            "WHERE channel = ? AND seq > ? ORDER BY seq LIMIT ?",
            [channel, since_seq, min(max(limit, 1), 5000)],
        )
        return [
            WsReplayEvent(
                channel=channel,
                seq=int(row[0]),
                ts=_dt(row[1]),
                event_type=row[2],
                payload=json.loads(row[3]),
            )
            for row in rows
        ]

    async def last_ws_seq(self, channel: str) -> int:
        rows = await self._arun(
            "SELECT COALESCE(max(seq), 0) FROM ws_replay WHERE channel = ?", [channel]
        )
        return int(rows[0][0]) if rows else 0

    async def set_memory_limit_mb(self, limit_mb: int) -> None:
        await asyncio.to_thread(self._pool.set_memory_limit_mb, limit_mb)

    async def memory_used_bytes(self) -> int | None:
        try:
            rows = await self._arun("SELECT sum(memory_usage_bytes) FROM duckdb_memory()")
            return int(rows[0][0]) if rows and rows[0][0] is not None else 0
        except Exception:  # noqa: BLE001 — разные версии DuckDB дают разные исключения
            return None  # Показываем «н/д», не выдумываем 0

    async def table_sizes(self) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for table in (
            "instruments",
            "instrument_catalog",
            "candles",
            "market_snapshots",
            "decision_snapshots",
            "orderbook_snapshots",
            "trade_plans",
            "trades",
        ):
            rows = await self._arun(f"SELECT count(*) FROM {table}")
            sizes[table] = int(rows[0][0]) if rows else 0
        return sizes
