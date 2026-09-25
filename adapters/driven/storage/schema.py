"""Схема DuckDB: горячий / тёплый слой.

Слои хранения:

* **hot** — последние сутки, full-detail: все свечи 1m/1h/1d, снапшоты стакана
  и все market/decision снапшоты;
* **warm** — до ~180 дней в той же БД, но «прореженный» (свечи 1m агрегируются
  до 5m/15m, снапшоты стакана удаляются);
* **cold** — всё старше: Parquet + ZSTD, доступно через ``read_parquet``.

Снапшоты хранятся как JSON-поля: структура у них глубоко вложенная и
изменяемая, а нормализация в десяток таблиц резко усложнила бы запись без
выигрыша для дата-майнинга (DuckDB умеет работать с JSON как с данными).
"""

from __future__ import annotations

SCHEMA_VERSION = 1

DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key VARCHAR PRIMARY KEY,
        value VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS instruments (
        uid VARCHAR PRIMARY KEY,
        ticker VARCHAR NOT NULL,
        class_code VARCHAR NOT NULL DEFAULT 'TQBR',
        lot_size INTEGER NOT NULL,
        is_benchmark BOOLEAN NOT NULL DEFAULT FALSE,
        currency VARCHAR NOT NULL DEFAULT 'RUB'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candles (
        instrument_uid VARCHAR NOT NULL,
        timeframe VARCHAR NOT NULL,
        ts TIMESTAMPTZ NOT NULL,
        open DECIMAL(18, 6) NOT NULL,
        high DECIMAL(18, 6) NOT NULL,
        low DECIMAL(18, 6) NOT NULL,
        close DECIMAL(18, 6) NOT NULL,
        volume BIGINT NOT NULL,
        PRIMARY KEY (instrument_uid, timeframe, ts)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orderbook_snapshots (
        id VARCHAR PRIMARY KEY,
        instrument_uid VARCHAR NOT NULL,
        captured_at TIMESTAMPTZ NOT NULL,
        bids VARCHAR NOT NULL,
        asks VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_snapshots (
        id VARCHAR PRIMARY KEY,
        instrument_uid VARCHAR NOT NULL,
        captured_at TIMESTAMPTZ NOT NULL,
        payload VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_snapshots (
        id VARCHAR PRIMARY KEY,
        market_snapshot_id VARCHAR,
        trade_plan_id VARCHAR,
        decision VARCHAR NOT NULL,
        confluence_score DECIMAL(9, 6) NOT NULL,
        reasoning VARCHAR NOT NULL,
        risk_check_passed BOOLEAN NOT NULL,
        risk_check_reason VARCHAR,
        thought_text VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_plans (
        id VARCHAR PRIMARY KEY,
        instrument_uid VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        entry_price DECIMAL(18, 6) NOT NULL,
        hard_stop_price DECIMAL(18, 6) NOT NULL,
        target_price DECIMAL(18, 6) NOT NULL,
        thesis VARCHAR NOT NULL,
        invalidation VARCHAR NOT NULL,
        max_holding_seconds BIGINT NOT NULL,
        quantity_lots INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL,
        closed_at TIMESTAMPTZ,
        rejection_reason VARCHAR,
        config_version INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
        trade_plan_id VARCHAR PRIMARY KEY,
        entry_price DECIMAL(18, 6) NOT NULL,
        exit_price DECIMAL(18, 6) NOT NULL,
        mfe DECIMAL(12, 8) NOT NULL,
        mae DECIMAL(12, 8) NOT NULL,
        exit_efficiency DECIMAL(12, 8) NOT NULL,
        price_at_session_close DECIMAL(18, 6) NOT NULL,
        price_at_t_plus_1d DECIMAL(18, 6),
        price_at_t_plus_3d DECIMAL(18, 6),
        post_exit_drift_pct DECIMAL(12, 8) NOT NULL,
        verdict VARCHAR NOT NULL,
        holding_seconds BIGINT NOT NULL DEFAULT 0,
        realized_pnl DECIMAL(18, 6) NOT NULL DEFAULT 0,
        closed_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hypotheses (
        id VARCHAR PRIMARY KEY,
        text VARCHAR NOT NULL,
        condition_description VARCHAR NOT NULL,
        sample_size INTEGER NOT NULL,
        confidence DECIMAL(9, 6) NOT NULL,
        status VARCHAR NOT NULL,
        suggested_action VARCHAR NOT NULL DEFAULT '',
        walk_forward_efficiency DECIMAL(12, 8),
        evidence VARCHAR NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_configs (
        version INTEGER PRIMARY KEY,
        payload VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_states (
        id VARCHAR PRIMARY KEY,
        payload VARCHAR NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
)

INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles (instrument_uid, timeframe, ts)",
    "CREATE INDEX IF NOT EXISTS idx_market_snapshots_time ON market_snapshots (captured_at)",
    "CREATE INDEX IF NOT EXISTS idx_decision_created ON decision_snapshots (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_plans_status ON trade_plans (status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades (closed_at)",
)


def ddl_script() -> str:
    """Полный DDL-скрипт для инициализации БД."""
    return ";\n".join(DDL_STATEMENTS) + ";\n" + ";\n".join(INDEX_STATEMENTS) + ";"


def version_statement() -> str:
    return f"INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', '{SCHEMA_VERSION}')"


TABLES: tuple[str, ...] = (
    "schema_meta",
    "instruments",
    "candles",
    "orderbook_snapshots",
    "market_snapshots",
    "decision_snapshots",
    "trade_plans",
    "trades",
    "hypotheses",
    "strategy_configs",
    "portfolio_states",
)
