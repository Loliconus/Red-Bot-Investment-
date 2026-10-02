"""Исследовательские use cases. Инъекция сервиса, никаких broker mutations."""

from __future__ import annotations

from typing import Any, Protocol


class ResearchService(Protocol):
    """Application service boundary (не седьмой core broker/storage порт)."""

    async def launch(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def finalize(self, run_id: str) -> dict[str, Any]: ...
    async def cancel(self, run_id: str) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...
    def runs(self) -> list[dict[str, Any]]: ...
    def get(self, run_id: str, *, report: bool = True) -> dict[str, Any]: ...
    def artifact(self, run_id: str, name: str) -> str: ...


async def launch_research(service: ResearchService, payload: dict[str, Any]) -> dict[str, Any]:
    return await service.launch(payload)


async def evaluate_final(
    service: ResearchService, run_id: str, confirmation: str
) -> dict[str, Any]:
    if confirmation != "ОТКРЫТЬ FINAL OOS":
        raise ValueError(
            "Нужно точное подтверждение ОТКРЫТЬ FINAL OOS. Период раскрывается один раз."
        )
    return await service.finalize(run_id)
