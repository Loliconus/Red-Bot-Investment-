"""Content-addressed Parquet snapshots, SQLite trial ledger, одноразовый final gate.

Замороженные данные физически отделены. Development runner их не читает и не
размечает. O_EXCL/SQLite transactions не позволяют двум workers раскрыть final
одновременно; ошибка после раскрытия не возвращает тесту невинность.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import numpy as np
import pandas as pd

from core.domain.probability import ResearchInstrument
from synthetic_trader.config import ExperimentConfig
from synthetic_trader.data import MarketDataset


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is pd.NaT:
        return None
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(json_safe(payload), ensure_ascii=False, allow_nan=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(json_safe(payload), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def study_key(config: ExperimentConfig) -> str:
    fields = {
        key: config.model_dump(mode="json")[key]
        for key in ("source", "start", "end", "interval", "freeze_months")
    }
    fields["symbols"] = sorted(config.symbols)
    return digest(fields)


def write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(":memory:") as conn:
        conn.from_df(frame).write_parquet(str(path), compression="zstd")


def read_parquet(path: Path) -> pd.DataFrame:
    with duckdb.connect(":memory:") as conn:
        return conn.execute("SELECT * FROM read_parquet(?)", [str(path)]).fetchdf()


@contextmanager
def research_job(root: Path, run_id: str) -> Iterator[None]:
    """Общий mutex CLI / GUI / Prefect; GUI child наследует только свою запись."""
    root.mkdir(parents=True, exist_ok=True)
    lock = root / "worker.lock"
    owned = False
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        current = json.loads(lock.read_text(encoding="utf-8"))
        if current.get("run_id") != run_id or current.get("pid") != os.getpid():
            raise ValueError(
                "Research worker уже занят. CLI / GUI / Prefect используют общий mutex."
            ) from None
    else:
        with os.fdopen(descriptor, "w") as file:
            json.dump({"run_id": run_id, "pid": os.getpid(), "owner_pid": os.getpid()}, file)
        owned = True
    try:
        yield
    finally:
        if owned and lock.is_file():
            current = json.loads(lock.read_text(encoding="utf-8"))
            if current.get("run_id") == run_id and current.get("pid") == os.getpid():
                lock.unlink(missing_ok=True)


class FinalAlreadyConsumedError(ValueError):
    """Один и тот же frozen study уже раскрыт и не является final OOS снова."""


class ExperimentLedger:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "experiments.sqlite3"
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS trials (
                    id INTEGER PRIMARY KEY, study TEXT NOT NULL, run TEXT NOT NULL,
                    configuration TEXT NOT NULL, candidate TEXT NOT NULL, started_at TEXT NOT NULL,
                    daily_sharpe REAL
                );
                CREATE TABLE IF NOT EXISTS studies (
                    study TEXT PRIMARY KEY, source TEXT NOT NULL, cutoff TEXT NOT NULL, end_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_trials (
                    source TEXT PRIMARY KEY, count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    run TEXT PRIMARY KEY, study TEXT NOT NULL, report_hash TEXT NOT NULL,
                    model_hash TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS frozen (
                    study TEXT PRIMARY KEY, run TEXT NOT NULL, revealed_at TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(trials)")}
            if "daily_sharpe" not in columns:
                connection.execute("ALTER TABLE trials ADD COLUMN daily_sharpe REAL")
        # Backfill historical snapshot scopes; no prices / frozen Parquet read.
        for file in (root / "datasets").glob("*/manifest.json"):
            metadata = json.loads(file.read_text(encoding="utf-8"))
            with self.connect() as conn:
                end_at = (
                    (pd.Timestamp(metadata["end"], tz="Europe/Moscow") + pd.Timedelta(days=1))
                    .tz_convert("UTC")
                    .isoformat()
                )
                conn.execute(
                    "INSERT OR IGNORE INTO studies VALUES (?,?,?,?)",
                    (metadata["study_id"], metadata["source"], metadata["cutoff"], end_at),
                )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=15)

    def _assert_open(self, conn: sqlite3.Connection, study: str) -> None:
        # Scope guard survives changes in start/universe/timeframe: a different
        # study ID cannot turn the same revealed calendar into untouched OOS.
        seen = conn.execute(
            """
            SELECT 1 FROM frozen f LEFT JOIN studies previous ON previous.study=f.study
            LEFT JOIN studies current ON current.study=?
            WHERE f.study=? OR (previous.source=current.source
              AND previous.cutoff<current.end_at AND current.cutoff<previous.end_at)
            LIMIT 1
        """,
            (study, study),
        ).fetchone()
        if seen:
            raise FinalAlreadyConsumedError(
                "Final OOS этого или перекрывающегося study уже раскрыт. Смена universe/start/interval не восстанавливает независимость."
            )

    def register_study(self, config: ExperimentConfig) -> None:
        end_at = pd.Timestamp(config.end, tz="Europe/Moscow") + pd.Timedelta(days=1)
        cutoff = (end_at - pd.DateOffset(months=config.freeze_months)).tz_convert("UTC")
        study = study_key(config)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO studies VALUES (?,?,?,?)",
                (study, config.source, cutoff.isoformat(), end_at.tz_convert("UTC").isoformat()),
            )
            self._assert_open(conn, study)

    def assert_development_open(self, study: str) -> None:
        with self.connect() as conn:
            self._assert_open(conn, study)

    def trial_population(self, study: str) -> dict[str, Any]:
        with self.connect() as conn:
            scope = conn.execute("SELECT source FROM studies WHERE study=?", (study,)).fetchone()
            if scope is None:
                raise ValueError("Study scope не зарегистрирован")
            source = scope[0]
            rows = conn.execute(
                "SELECT t.daily_sharpe FROM trials t JOIN studies s ON s.study=t.study WHERE s.source=?",
                (source,),
            ).fetchall()
            declared = conn.execute(
                "SELECT count FROM external_trials WHERE source=?", (source,)
            ).fetchone()
        sharpes = np.asarray([row[0] for row in rows if row[0] is not None], dtype=float)
        return {
            "trials": len(rows) + (declared[0] if declared else 0),
            "observed_sharpes": len(sharpes),
            "variance": float(np.var(sharpes, ddof=1)) if len(sharpes) > 1 else None,
            "scope": "all ledger configurations of this data source, including other universes/timeframes",
            "external_trials": declared[0] if declared else 0,
        }

    def record_sharpes(self, run: str, sharpes: dict[str, float | None]) -> None:
        with self.connect() as conn:
            conn.executemany(
                "UPDATE trials SET daily_sharpe=? WHERE run=? AND candidate=?",
                [(value, run, candidate) for candidate, value in sharpes.items()],
            )

    def register_trials(
        self, study: str, run: str, config: ExperimentConfig, candidates: list[str]
    ) -> int:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_open(conn, study)
            stamp = datetime.now(UTC).isoformat()
            conn.executemany(
                "INSERT INTO trials(study,run,configuration,candidate,started_at) VALUES (?,?,?,?,?)",
                [
                    (study, run, digest(config.model_dump(mode="json")), candidate, stamp)
                    for candidate in candidates
                ],
            )
            conn.execute(
                "INSERT INTO external_trials(source,count) VALUES (?,?) ON CONFLICT(source) DO UPDATE SET count=max(external_trials.count,excluded.count)",
                (config.source, config.additional_trials),
            )
        return int(self.trial_population(study)["trials"])

    def register_candidate(self, run: str, study: str, report_hash: str, model_hash: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO candidates VALUES (?,?,?,?,?)",
                (run, study, report_hash, model_hash, datetime.now(UTC).isoformat()),
            )

    def assert_candidate(self, run: str, report_hash: str, model_hash: str) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT report_hash,model_hash FROM candidates WHERE run=?", (run,)
            ).fetchone()
        if row is None or row != (report_hash, model_hash):
            raise ValueError(
                "Модель / конфигурация / отчёт изменены или не зафиксированы в ledger. Final остаётся закрыт."
            )

    def reveal_once(self, study: str, run: str) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_open(conn, study)
            try:
                conn.execute(
                    "INSERT INTO frozen(study,run,revealed_at) VALUES (?,?,?)",
                    (study, run, datetime.now(UTC).isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise FinalAlreadyConsumedError(
                    "Final OOS уже был раскрыт; повтор запрещён даже после ошибки"
                ) from exc

    def frozen_state(self, study: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT run,revealed_at FROM frozen WHERE study=?", (study,)
            ).fetchone()
        return {
            "consumed": row is not None,
            "run_id": row[0] if row else None,
            "revealed_at": row[1] if row else None,
        }


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root / "datasets"
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger = ExperimentLedger(root)

    def path(self, dataset_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", dataset_id):
            raise ValueError("Некорректный dataset_id")
        return self.root / dataset_id

    def manifest(self, dataset_id: str) -> dict[str, Any]:
        path = self.path(dataset_id) / "manifest.json"
        if not path.is_file():
            raise ValueError("Снэпшот данных не найден")
        result: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return result

    def save(self, dataset: MarketDataset, config: ExperimentConfig) -> dict[str, Any]:
        study = study_key(config)
        self.ledger.register_study(config)
        self.ledger.assert_development_open(study)
        cutoff = (
            pd.Timestamp(config.end, tz="Europe/Moscow")
            + pd.Timedelta(days=1)
            - pd.DateOffset(months=config.freeze_months)
        ).tz_convert("UTC")
        metadata = {
            "study_id": study,
            "source": dataset.source,
            "interval": config.interval,
            "symbols": list(config.symbols),
            "start": config.start.isoformat(),
            "end": config.end.isoformat(),
            "freeze_months": config.freeze_months,
            "cutoff": cutoff.isoformat(),
            "instruments": [asdict(i) for i in dataset.instruments],
            "schema": "decimal-text-ohlcv-exclusive-end-v1",
        }
        checksum = hashlib.sha256(json.dumps(json_safe(metadata), sort_keys=True).encode())
        for key, frame in sorted(dataset.frames.items()):
            checksum.update(str(key).encode())
            checksum.update(
                frame[["begin", "end", "open", "high", "low", "close", "volume"]]
                .to_csv(index=False)
                .encode()
            )
        dataset_id = checksum.hexdigest()
        destination = self.path(dataset_id)
        if destination.is_dir():
            return self.manifest(dataset_id)
        staging = self.root / f".{uuid4().hex}.tmp"
        staging.mkdir()
        try:
            files: list[dict[str, Any]] = []
            for (symbol, tf), frame in sorted(dataset.frames.items()):
                for part, subset in (
                    ("development", frame.loc[frame["end"] <= cutoff]),
                    ("frozen", frame.loc[frame["end"] > cutoff]),
                ):
                    if subset.empty:
                        raise ValueError(
                            f"{symbol}/{tf}: нет {part} истории; диапазон ISS недостаточен"
                        )
                    relative = f"{part}/{symbol}_{tf}.parquet"
                    file = staging / relative
                    write_parquet(subset.reset_index(drop=True), file)
                    files.append(
                        {
                            "path": relative,
                            "part": part,
                            "symbol": symbol,
                            "timeframe": tf,
                            "rows": len(subset),
                            "first": subset["begin"].min(),
                            "last": subset["end"].max(),
                            "sha256": file_digest(file),
                        }
                    )
            manifest = {
                **metadata,
                "dataset_id": dataset_id,
                "files": files,
                "warnings": dataset.warnings,
                "created_at": datetime.now(UTC),
            }
            write_json(staging / "manifest.json", manifest)
            try:
                os.replace(staging, destination)
            except OSError as exc:
                if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not destination.is_dir():
                    raise
                # Другой ingestion завершил тот же content-addressed snapshot.
                # НЕ перезаписываем ни данные, ни исходный manifest.
            return self.manifest(dataset_id)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _read_part(self, dataset_id: str, part: str) -> MarketDataset:
        manifest = self.manifest(dataset_id)
        frames: dict[tuple[str, str], pd.DataFrame] = {}
        for item in manifest["files"]:
            if item["part"] != part:
                continue
            path = self.path(dataset_id) / item["path"]
            if file_digest(path) != item["sha256"]:
                raise ValueError("Снэпшот повреждён: checksum не совпадает")
            frames[(item["symbol"], item["timeframe"])] = read_parquet(path)
        instruments = [
            ResearchInstrument(**{**i, "tick_size": Decimal(i["tick_size"])})
            for i in manifest["instruments"]
        ]
        return MarketDataset(
            frames=frames,
            instruments=instruments,
            source=manifest["source"],
            warnings=manifest["warnings"],
        )

    def development(self, dataset_id: str) -> MarketDataset:
        self.ledger.assert_development_open(self.manifest(dataset_id)["study_id"])
        return self._read_part(dataset_id, "development")

    def frozen(self, dataset_id: str, *, run_id: str) -> MarketDataset:
        manifest = self.manifest(dataset_id)
        state = self.ledger.frozen_state(manifest["study_id"])
        if not state["consumed"] or state["run_id"] != run_id:
            raise ValueError("Frozen данные закрыты: сначала зафиксируйте модель и reveal_once")
        return self._read_part(dataset_id, "frozen")

    def revealed_development(self, dataset_id: str, *, run_id: str) -> MarketDataset:
        # Финальный worker нуждается в прошлом для rolling context, не для fit.
        manifest = self.manifest(dataset_id)
        if self.ledger.frozen_state(manifest["study_id"])["run_id"] != run_id:
            raise ValueError("Этот запуск не владеет одноразовым final gate")
        return self._read_part(dataset_id, "development")
