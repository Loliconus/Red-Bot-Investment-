"""Внутрипроцессные фоновые операции хранения; не блокируют другие экраны."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from adapters.driving.web.ws.hub import BroadcastHub


@dataclass(slots=True)
class Job:
    id: str
    kind: str
    status: str
    started_at: datetime
    detail: str = ""


class JobManager:
    def __init__(self, hub: BroadcastHub) -> None:
        self._hub = hub
        self._jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def jobs(self) -> list[Job]:
        return list(reversed(list(self._jobs.values())))[:20]

    def start(self, kind: str, work: Callable[[], Awaitable[str]]) -> Job:
        if any(j.kind == kind and j.status == "running" for j in self._jobs.values()):
            raise ValueError("Операция такого типа уже выполняется")
        job = Job(id=uuid4().hex, kind=kind, status="running", started_at=datetime.now(tz=UTC))
        self._jobs[job.id] = job
        task = asyncio.create_task(self._run(job, work), name=f"storage:{kind}:{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job, work: Callable[[], Awaitable[str]]) -> None:
        await self._announce(job)
        try:
            job.detail = await work()
            job.status = "done"
        except Exception as exc:  # noqa: BLE001 — фоновые операции не должны прерывать GUI
            job.status = "failed"
            job.detail = f"Ошибка: {type(exc).__name__}"  # traceback может содержать секреты
        await self._announce(job)

    async def _announce(self, job: Job) -> None:
        await self._hub.publish(
            "db_admin.jobs",
            "job.updated",
            {
                "id": job.id,
                "kind": job.kind,
                "status": job.status,
                "detail": job.detail,
                "started_at": job.started_at.isoformat(),
            },
        )

    async def stop(self) -> None:
        # При остановке GUI ждём завершения копирования, не прерываем середину транзакции.
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
