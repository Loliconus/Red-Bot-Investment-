"""Отдельный ML worker. Не composition торгового приложения, не API/SDK клиент."""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from synthetic_trader.config import ExperimentConfig
from synthetic_trader.pipeline import run_experiment, run_final
from synthetic_trader.storage import write_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--run", required=True)
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--managed", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{32}", args.run):
        parser.error("run must be a 32-character hexadecimal ID")
    root, run_id = args.root, args.run
    run_dir = root / "runs" / run_id
    status_path = run_dir / ("final_status.json" if args.final else "status.json")
    started = datetime.now(UTC).isoformat()

    def progress(stage: str, value: int, detail: str) -> None:
        write_json(
            status_path,
            {
                "id": run_id,
                "status": "completed" if stage == "completed" else "running",
                "stage": stage,
                "progress": value,
                "detail": detail,
                "started_at": started,
                "updated_at": datetime.now(UTC),
                "pid": os.getpid(),
                "final": args.final,
            },
        )

    try:
        if args.managed and sys.stdin.buffer.read(1) != b"G":
            raise ValueError("Research manager не подтвердил запуск worker")
        if args.final:
            run_final(root, run_id, progress)
        else:
            config = ExperimentConfig.model_validate_json((run_dir / "config.json").read_text())
            run_experiment(root, run_id, config, progress)
    except Exception as exc:  # noqa: BLE001 — граница отдельного worker, GUI должен получить failed
        # Worker получает whitelist environment БЕЗ токенов / счёта / GUI secrets.
        detail = (
            str(exc)
            if isinstance(exc, ValueError)
            else f"{type(exc).__name__}: этап не выполнен; проверьте источник/зависимости"
        )
        write_json(
            status_path,
            {
                "id": run_id,
                "status": "failed",
                "stage": "failed",
                "progress": 0,
                "detail": detail[:1200],
                "started_at": started,
                "updated_at": datetime.now(UTC),
                "final": args.final,
            },
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
