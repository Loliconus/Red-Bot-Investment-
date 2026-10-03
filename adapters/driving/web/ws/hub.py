"""BroadcastHub: независимые seq, snapshot при подписке и HTTP replay.

Решения хранятся в RepositoryPort без coalescing; при переполнении очереди
медленный клиент отключается, затем скачивает ВСЕ решения по seq. Для графика
при перегрузке сохраняется лишь последний бар, защищая event loop и FPS.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from adapters.driving.web.ws.channels import Channel, valid_channel
from adapters.driving.web.ws.envelope import Envelope
from core.ports.persistence import RepositoryPort, WsReplayEvent

SnapshotProvider = Callable[[str], Awaitable[dict[str, Any] | list[Any]]]
REPLAY_WINDOW = 512
CLIENT_QUEUE = 128


@dataclass(eq=False, slots=True)
class Client:
    socket: WebSocket
    queue: asyncio.Queue[Envelope] = field(
        default_factory=lambda: asyncio.Queue(maxsize=CLIENT_QUEUE)
    )
    channels: set[str] = field(default_factory=set)
    ready: set[str] = field(default_factory=set)
    pending: dict[str, list[Envelope]] = field(default_factory=lambda: defaultdict(list))
    coalesced: dict[str, Envelope] = field(default_factory=dict)
    sender: asyncio.Task[None] | None = None
    closing: bool = False


class ReplayGapError(ValueError):
    def __init__(self, latest_seq: int) -> None:
        self.latest_seq = latest_seq
        super().__init__("История канала недоступна; запросите полный снимок")


class BroadcastHub:
    def __init__(
        self,
        repository: RepositoryPort,
        snapshot_provider: SnapshotProvider,
    ) -> None:
        self._repository = repository
        self._snapshot_provider = snapshot_provider
        self._lock = asyncio.Lock()
        self._seq: dict[str, int] = defaultdict(int)
        self._history: dict[str, deque[Envelope]] = defaultdict(lambda: deque(maxlen=REPLAY_WINDOW))
        self._clients: set[Client] = set()
        self._subscribers: dict[str, set[Client]] = defaultdict(set)
        self._closing_tasks: set[asyncio.Task[None]] = set()

    @property
    def connection_count(self) -> int:
        return len(self._clients)

    async def start(self) -> None:
        self._seq[Channel.DASHBOARD_DECISIONS] = await self._repository.last_ws_seq(
            Channel.DASHBOARD_DECISIONS
        )

    async def stop(self) -> None:
        for client in list(self._clients):
            with contextlib.suppress(Exception):
                await client.socket.close()
            await self.disconnect(client)
        if self._closing_tasks:
            await asyncio.gather(*self._closing_tasks, return_exceptions=True)

    async def connect(self, socket: WebSocket) -> Client:
        await socket.accept()
        client = Client(socket=socket)
        self._clients.add(client)
        client.sender = asyncio.create_task(self._sender(client), name="ws-sender")
        return client

    async def disconnect(self, client: Client) -> None:
        self._clients.discard(client)
        for channel in client.channels:
            self._subscribers[channel].discard(client)
        if client.sender is not None and client.sender is not asyncio.current_task():
            client.sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await client.sender
        client.closing = True

    async def subscribe(self, client: Client, channel: str) -> None:
        if not valid_channel(channel):
            raise ValueError("Неизвестный WS-канал")
        async with self._lock:
            if channel in client.channels:
                return
            client.channels.add(channel)
            self._subscribers[channel].add(client)
            seq = self._seq[channel]
        try:
            snapshot = await self._snapshot_provider(channel)
        except Exception:
            # Не выдаём пустой фиктивный снимок: ошибка канала, другие работают.
            async with self._lock:
                client.channels.discard(channel)
                self._subscribers[channel].discard(client)
                client.pending.pop(channel, None)
            raise
        async with self._lock:
            self._enqueue(client, Envelope.snapshot(channel, seq, datetime.now(tz=UTC), snapshot))
            for pending in client.pending.pop(channel, []):
                self._enqueue(client, pending)
            client.ready.add(channel)

    async def unsubscribe(self, client: Client, channel: str) -> None:
        async with self._lock:
            client.channels.discard(channel)
            client.ready.discard(channel)
            self._subscribers[channel].discard(client)
            client.pending.pop(channel, None)

    async def publish(
        self, channel: str, event_type: str, payload: dict[str, Any] | list[Any]
    ) -> Envelope:
        if not valid_channel(channel):
            raise ValueError("Неизвестный WS-канал")
        async with self._lock:
            seq = self._seq[channel] + 1
            envelope = Envelope(
                channel=channel,
                seq=seq,
                ts=datetime.now(tz=UTC),
                type=event_type,
                payload=payload,
            )
            if channel == Channel.DASHBOARD_DECISIONS:
                if not isinstance(payload, dict):
                    raise ValueError("Решение должно иметь объектный payload")
                await self._repository.append_ws_event(
                    WsReplayEvent(
                        channel=channel,
                        seq=seq,
                        ts=envelope.ts,
                        event_type=event_type,
                        payload=payload,
                    )
                )
            self._seq[channel] = seq
            self._history[channel].append(envelope)
            for client in list(self._subscribers[channel]):
                if channel in client.ready:
                    self._enqueue(client, envelope)
                else:
                    client.pending[channel].append(envelope)
        return envelope

    def _enqueue(self, client: Client, envelope: Envelope) -> None:
        if client.closing:
            return
        channel = envelope.channel
        if channel.startswith("chart.") and (client.queue.full() or channel in client.coalesced):
            client.coalesced[channel] = envelope
            return
        try:
            client.queue.put_nowait(envelope)
        except asyncio.QueueFull:
            # Никогда не выбрасываем dashboard.decisions: WS переподключится
            # и дочитает журнал через /api/ws/replay.
            client.closing = True
            task = asyncio.create_task(client.socket.close(code=1013))
            self._closing_tasks.add(task)
            task.add_done_callback(self._closing_tasks.discard)

    async def _sender(self, client: Client) -> None:
        try:
            while not client.closing:
                try:
                    item = await asyncio.wait_for(client.queue.get(), timeout=0.5)
                    await client.socket.send_json(item.model_dump(mode="json"))
                except TimeoutError:
                    pass
                if client.coalesced:
                    items = list(client.coalesced.values())
                    client.coalesced.clear()
                    for latest in items:
                        await client.socket.send_json(latest.model_dump(mode="json"))
        except (WebSocketDisconnect, RuntimeError, OSError):
            client.closing = True

    async def replay(
        self, channel: str, since_seq: int, *, limit: int = 1000
    ) -> tuple[int, list[Envelope]]:
        if not valid_channel(channel) or since_seq < 0:
            raise ValueError("Неизвестный канал или отрицательный seq")
        limit = min(max(limit, 1), 1000)
        async with self._lock:
            latest = self._seq[channel]
            history = list(self._history[channel])
        if since_seq > latest:
            raise ReplayGapError(latest)
        if channel == Channel.DASHBOARD_DECISIONS:
            events = await self._repository.list_ws_events(channel, since_seq, limit=limit)
            return latest, [
                Envelope(
                    channel=e.channel, seq=e.seq, ts=e.ts, type=e.event_type, payload=e.payload
                )
                for e in events
            ]
        if history and since_seq < history[0].seq - 1:
            raise ReplayGapError(latest)
        return latest, [item for item in history if item.seq > since_seq][:limit]


async def websocket_endpoint(hub: BroadcastHub, websocket: WebSocket) -> None:
    client = await hub.connect(websocket)
    try:
        while True:
            command = await websocket.receive_json()
            if not isinstance(command, dict):
                continue
            action = command.get("action")
            if action == "subscribe":
                for channel in command.get("channels", [])[:20]:
                    try:
                        await hub.subscribe(client, str(channel))
                    except (ValueError, RuntimeError):
                        continue
            elif action == "unsubscribe":
                for channel in command.get("channels", []):
                    await hub.unsubscribe(client, str(channel))
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(client)
