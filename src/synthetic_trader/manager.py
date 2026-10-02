"""GUI orchestration: один отдельный cancellable worker, persisted run artifacts."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil

from synthetic_trader.config import ExperimentConfig
from synthetic_trader.storage import ExperimentLedger, study_key, write_json


class ResearchBusyError(ValueError):
    """Один worker уже работает; параллельные прогоны пилота запрещены."""


class ResearchManager:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)
        self.ledger = ExperimentLedger(self.root)
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._watchers: set[asyncio.Task[None]] = set()
        self._final_jobs: set[str] = set()
        self._recover()

    def _recover(self) -> None:
        for path in sorted((self.root / "runs").glob("*/*status.json")):
            data = json.loads(path.read_text())
            if data.get("status") == "running" and not self._alive(data.get("pid")):
                write_json(
                    path,
                    {
                        **data,
                        "status": "interrupted",
                        "stage": "interrupted",
                        "detail": "Worker не работает; прогон прерван, не результат бэктеста",
                    },
                )
        lock = self.root / "worker.lock"
        if lock.exists():
            data = json.loads(lock.read_text())
            if not self._alive(data.get("owner_pid")) and not self._alive(data.get("pid")):
                lock.unlink()

    @staticmethod
    def _alive(pid: Any) -> bool:
        if not isinstance(pid, int) or pid < 1:
            return False
        return bool(psutil.pid_exists(pid))

    def _directory(self, run_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Некорректный run_id")
        path = self.root / "runs" / run_id
        if not path.is_dir():
            raise ValueError("Прогон не найден")
        return path

    def runs(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted((self.root / "runs").glob("*/*status.json")):
            status = json.loads(path.read_text())
            config_path = path.parent / "config.json"
            if config_path.exists():
                config = json.loads(config_path.read_text())
                status["source"], status["symbols"], status["interval"] = (
                    config["source"],
                    config["symbols"],
                    config["interval"],
                )
            result.append(status)
        return sorted(result, key=lambda r: r.get("started_at", ""), reverse=True)[:50]

    def get(self, run_id: str, *, report: bool = True) -> dict[str, Any]:
        directory = self._directory(run_id)
        status: dict[str, Any] = json.loads((directory / "status.json").read_text())
        status["config"] = json.loads((directory / "config.json").read_text())
        if report and (directory / "report.json").exists():
            status["report"] = json.loads((directory / "report.json").read_text())
            study = status["report"]["dataset"]["study_id"]
            status["report"]["final_oos"] = self.ledger.frozen_state(study)
        if (directory / "final_status.json").exists():
            status["final_status"] = json.loads((directory / "final_status.json").read_text())
        if (directory / "final.json").exists():
            status["final_report"] = json.loads((directory / "final.json").read_text())
        return status

    def artifact(self, run_id: str, name: str) -> str:
        allowed = {
            "report.json",
            "report.md",
            "predictions.parquet",
            "final.json",
            "final_predictions.parquet",
        }
        if name not in allowed:
            raise ValueError("Артефакт недоступен")
        path = self._directory(run_id) / name
        if not path.is_file():
            raise ValueError("Артефакт ещё не создан")
        return str(path)

    async def launch(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = ExperimentConfig.model_validate(payload)
        self.ledger.register_study(config)
        self.ledger.assert_development_open(study_key(config))
        run_id = uuid4().hex
        directory = self.root / "runs" / run_id
        directory.mkdir()
        write_json(directory / "config.json", config.model_dump(mode="json"))
        write_json(
            directory / "status.json",
            {
                "id": run_id,
                "status": "running",
                "stage": "queued",
                "progress": 0,
                "source": config.source,
                "detail": "Изолированный worker запускается",
                "started_at": datetime.now(UTC),
            },
        )
        try:
            await self._spawn(run_id, final=False)
        except (ResearchBusyError, OSError):
            write_json(
                directory / "status.json",
                {
                    "id": run_id,
                    "status": "failed",
                    "stage": "queued",
                    "progress": 0,
                    "detail": "Worker занят или не запустился",
                    "started_at": datetime.now(UTC),
                },
            )
            raise
        return self.get(run_id, report=False)

    async def finalize(self, run_id: str) -> dict[str, Any]:
        status = self.get(run_id)
        if status["status"] != "completed" or "report" not in status:
            raise ValueError("Сначала завершите и зафиксируйте исследовательский прогон")
        manifest = status["report"]["dataset"]
        self.ledger.assert_development_open(manifest["study_id"])
        # Bundle должен существовать ДО открытия final.
        if not (self._directory(run_id) / "model" / "bundle.json").is_file():
            raise ValueError("Нет фиксированной модели")
        await self._spawn(run_id, final=True)
        return self.get(run_id, report=False)

    async def _spawn(self, run_id: str, *, final: bool) -> None:
        lock = self.root / "worker.lock"
        try:
            handle = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ResearchBusyError(
                "Уже выполняется ML worker; дождитесь окончания или отмените прогон"
            ) from exc
        with os.fdopen(handle, "w") as file:
            json.dump({"run_id": run_id, "owner_pid": os.getpid()}, file)
        package_root = Path(__file__).resolve().parents[1]
        repo_root = package_root.parent
        # Не передаём INVEST_TOKEN, REDBOT_*, account IDs, GUI password, MLflow credentials.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "SYSTEMROOT",
                "WINDIR",
                "TEMP",
                "TMP",
                "HOME",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
            }
        }
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join((str(package_root), str(repo_root))),
                "OMP_NUM_THREADS": "2",
                "OPENBLAS_NUM_THREADS": "2",
                "MKL_NUM_THREADS": "2",
                "PYTHONHASHSEED": "0",
            }
        )
        command = [
            sys.executable,
            "-m",
            "synthetic_trader.worker",
            "--root",
            str(self.root),
            "--run",
            run_id,
            "--managed",
        ]
        if final:
            command.append("--final")
            self._final_jobs.add(run_id)
            write_json(
                self._directory(run_id) / "final_status.json",
                {
                    "id": run_id,
                    "status": "running",
                    "stage": "queued",
                    "progress": 0,
                    "detail": "Ожидание одноразового final gate",
                    "started_at": datetime.now(UTC),
                },
            )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            lock.unlink(missing_ok=True)
            self._final_jobs.discard(run_id)
            raise
        self._processes[run_id] = process
        write_json(lock, {"run_id": run_id, "owner_pid": os.getpid(), "pid": process.pid})
        path = self._directory(run_id) / ("final_status.json" if final else "status.json")
        status = json.loads(path.read_text())
        write_json(path, {**status, "pid": process.pid})
        # Child waits for this byte before writing progress; queued/pid cannot
        # overwrite an already completed/failed worker status.
        if process.stdin is not None:
            process.stdin.write(b"G")
            await process.stdin.drain()
            process.stdin.close()
        watcher = asyncio.create_task(self._watch(run_id, process, path))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)

    async def _watch(self, run_id: str, process: asyncio.subprocess.Process, path: Path) -> None:
        code = await process.wait()
        status = json.loads(path.read_text())
        if status["status"] == "running":
            write_json(
                path,
                {
                    **status,
                    "status": "failed",
                    "detail": f"Worker прерван (exit {code}); результата нет",
                },
            )
        self._processes.pop(run_id, None)
        self._final_jobs.discard(run_id)
        lock = self.root / "worker.lock"
        if lock.exists() and json.loads(lock.read_text()).get("run_id") == run_id:
            lock.unlink(missing_ok=True)

    async def cancel(self, run_id: str) -> dict[str, Any]:
        directory = self._directory(run_id)
        process = self._processes.get(run_id)
        if process is None:
            raise ValueError("Этот worker не работает / принадлежит другой GUI-сессии")
        path = directory / ("final_status.json" if run_id in self._final_jobs else "status.json")
        current = json.loads(path.read_text())
        write_json(
            path,
            {
                **current,
                "status": "cancelled",
                "stage": "cancelled",
                "detail": "Остановлен оператором. Раскрытый final gate не восстанавливается.",
            },
        )
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        return self.get(run_id, report=False)

    async def aclose(self) -> None:
        for run_id in list(self._processes):
            await self.cancel(run_id)
        if self._watchers:
            await asyncio.gather(*self._watchers)
