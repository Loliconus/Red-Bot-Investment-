"""WebSocket-хаб: живая лента мыслей бота и торговых событий.

GUI не должен опрашивать сервер каждые полсекунды — события толкаются сами.
Хаб хранит активные соединения, рассылает JSON и молча отбрасывает мёртвые
сокеты: падение одного клиента не влияет на остальных.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect

logger = structlog.get_logger(__name__)


class WebSocketHub:
    """Простой broadcast-хаб."""

    __slots__ = ("_connections", "_pump_task", "_queue")

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._pump_task: asyncio.Task[None] | None = None

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)
        logger.info("ws_connected", total=self.connection_count)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)
        logger.info("ws_disconnected", total=self.connection_count)

    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        """Ставит событие в очередь на рассылку (не блокирует отправителя)."""
        message = json.dumps({"event": event, "payload": payload}, default=str)
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:  # pragma: no cover - защита от переполнения
            logger.warning("ws_queue_full", message_dropped=True)

    async def _pump(self) -> None:
        while True:
            message = await self._queue.get()
            await self._send_to_all(message)

    async def _send_to_all(self, message: str) -> None:
        broken: list[WebSocket] = []
        for connection in list(self._connections):
            try:
                await connection.send_text(message)
            except Exception:  # noqa: BLE001 - мёртвый сокет не должен ломать рассылку
                broken.append(connection)
        for connection in broken:
            self.disconnect(connection)

    async def start(self) -> None:
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump(), name="ws-pump")

    async def stop(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
            self._pump_task = None
        for connection in list(self._connections):
            with contextlib.suppress(Exception):
                await connection.close()
        self._connections.clear()


async def websocket_endpoint(hub: WebSocketHub, websocket: WebSocket) -> None:
    """Обработчик WS-соединения: держит его открытым, пока жив клиент."""
    await hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
            await websocket.send_text(json.dumps({"event": "ack"}))
    except WebSocketDisconnect:
        hub.disconnect(websocket)
    except Exception:  # noqa: BLE001
        hub.disconnect(websocket)


def render_thought(text: str, *, module: str, score: Any = None) -> dict[str, Any]:
    """Формирует payload события «мысль бота»."""
    return {"text": text, "module": module, "confluence_score": score}
