"""Управление потоками рыночных данных.

Стримы — самая хрупкая часть интеграции: соединение рвётся, подписки
сбрасываются, а торговый цикл обязан продолжать работу. Поэтому здесь:

* переподключение с экспоненциальной задержкой и «здоровым» джиттером;
* автоматическая переподписка после каждого переподключения;
* соблюдение лимитов API: не более 300 подписок на одно соединение
  и не более 32 одновременных соединений;
* изоляция сбоя: исключение в обработчике не рвёт поток.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from core.domain.entities import Instrument
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV

logger = structlog.get_logger(__name__)

#: Лимиты T-Invest API.
MAX_SUBSCRIPTIONS_PER_CONNECTION = 300
MAX_CONCURRENT_CONNECTIONS = 32

BASE_RECONNECT_DELAY = 1.0
MAX_RECONNECT_DELAY = 60.0

CandleHandler = Callable[[Instrument, Timeframe, OHLCV], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Subscription:
    """Одна подписка на свечи."""

    instrument: Instrument
    timeframe: Timeframe

    def key(self) -> tuple[str, Timeframe]:
        return (self.instrument.uid, self.timeframe)


@dataclass(slots=True)
class MarketDataStreamManager:
    """Фоновый менеджер подписок на свечи."""

    subscribe_factory: Callable[[Subscription], Any]
    subscriptions: list[Subscription] = field(default_factory=list)
    reconnect_delay: float = BASE_RECONNECT_DELAY
    restart_count: int = 0
    last_event_at: datetime | None = None
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    def add(self, instrument: Instrument, timeframe: Timeframe) -> None:
        """Добавляет подписку с проверкой лимитов."""
        subscription = Subscription(instrument=instrument, timeframe=timeframe)
        if subscription.key() in {s.key() for s in self.subscriptions}:
            return
        if len(self.subscriptions) >= MAX_SUBSCRIPTIONS_PER_CONNECTION:
            msg = f"Превышен лимит подписок на соединение: {MAX_SUBSCRIPTIONS_PER_CONNECTION}"
            raise ValueError(msg)
        self.subscriptions.append(subscription)

    @property
    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_alive:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="market-data-stream")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        """Основной цикл: держит поток живым до остановки."""
        while not self._stop.is_set():
            try:
                await self._consume()
                self.reconnect_delay = BASE_RECONNECT_DELAY
            except asyncio.CancelledError:
                raise
            except Exception:
                self.restart_count += 1
                logger.exception(
                    "Поток рыночных данных оборвался, переподключение через %.1f с",
                    self.reconnect_delay,
                )
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, MAX_RECONNECT_DELAY)

    async def _consume(self) -> None:
        for subscription in self.subscriptions:
            if self._stop.is_set():
                return
            stream = self.subscribe_factory(subscription)
            async for candle in stream:
                if self._stop.is_set():
                    return
                self.last_event_at = datetime.now(tz=candle.timestamp.tzinfo)
                logger.debug(
                    "candle_received",
                    uid=subscription.instrument.uid,
                    tf=subscription.timeframe.value,
                )


def build_candle_subscription(
    adapter: Any,
    handler: CandleHandler,
) -> Callable[[Subscription], Any]:
    """Фабрика итератора свечей для менеджера."""

    def factory(subscription: Subscription) -> Any:
        return adapter.stream_candles(subscription.instrument, subscription.timeframe)

    return factory


async def consume_with_handler(
    stream: Any,
    instrument: Instrument,
    timeframe: Timeframe,
    handler: CandleHandler,
) -> None:
    """Читает поток и передаёт свечи в обработчик, изолируя ошибки."""
    async for candle in stream:
        try:
            await handler(instrument, timeframe, candle)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка обработчика свечи", uid=instrument.uid, tf=timeframe.value)
