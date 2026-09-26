"""Retry-политика: осторожная и раздельная для чтения и мутаций.

Главное правило: **слепой retry мутации недопустим**. Повтор ``post_order``
без ключа идемпотентности при таймауте соединения — это вторая сделка, которую
никто не заказывал. Поэтому:

* read-only вызовы ретраим с ограниченным backoff;
* мутации не повторяем вслепую даже при известном idempotency key — при неопределенном
  результате сначала запрашиваем исходную заявку по клиентскому ключу;
* ``RESOURCE_EXHAUSTED`` повторяется только у явно безопасного read-only вызова.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: gRPC-коды, при которых повтор считается безопасным.
RETRIABLE_GRPC_CODES = frozenset({"UNAVAILABLE", "DEADLINE_EXCEEDED", "ABORTED"})
#: Код превышения квот API — повторяем, но с уважительной паузой.
RATE_LIMIT_CODES = frozenset({"RESOURCE_EXHAUSTED"})


class RetryExhaustedError(RuntimeError):
    """Все попытки исчерпаны."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.2
    max_delay: float = 5.0
    jitter: bool = True


DEFAULT_POLICY = RetryPolicy()


def classify_error(exc: BaseException) -> str:
    """Определяет класс ошибки по имени gRPC-кода."""
    code = getattr(exc, "code", None)
    if code is not None:
        name = getattr(code, "name", str(code))
        if name in RETRIABLE_GRPC_CODES:
            return "transient"
        if name in RATE_LIMIT_CODES:
            return "rate_limited"
        return "permanent"
    return "permanent"


def _backoff(attempt: int, policy: RetryPolicy) -> float:
    delay: float = min(policy.base_delay * (2 ** (attempt - 1)), policy.max_delay)
    if policy.jitter:
        delay *= 0.5 + random.random() / 2  # noqa: S311 - jitter, не криптография
    return delay


async def retry_async(
    operation: Callable[[], Awaitable[Any]],
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    idempotency_key: str | None = None,
    operation_name: str = "grpc_call",
    safe_to_retry: bool = False,
) -> Any:
    """Выполняет операцию с повторами.

    Повтор разрешён только при ``safe_to_retry=True`` для операций, чья
    повторяемость доказана (обычно read-only). Наличие idempotency key не
    делает слепой повтор финансовой мутации безопасным.
    """
    last_error: BaseException | None = None

    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            kind = classify_error(exc)

            if kind == "permanent":
                logger.warning(
                    "grpc_permanent_error", operation=operation_name, error_type=type(exc).__name__
                )
                raise

            if not safe_to_retry:
                logger.error(
                    "retry_skipped_unsafe_mutation",
                    operation=operation_name,
                    error_type=type(exc).__name__,
                    has_idempotency_key=idempotency_key is not None,
                    message="после неопределённой мутации требуется сверка, не повторная отправка",
                )
                raise

            if attempt == policy.max_attempts:
                break

            delay = _backoff(attempt, policy)
            if kind == "rate_limited":
                delay = max(delay, policy.max_delay)
            logger.warning(
                "grpc_retry",
                operation=operation_name,
                attempt=attempt,
                delay=round(delay, 3),
                kind=kind,
                error_type=type(exc).__name__,
            )
            await asyncio.sleep(delay)

    msg = f"{operation_name}: все {policy.max_attempts} попыток неудачны"
    raise RetryExhaustedError(msg) from last_error


async def retry_read(
    operation: Callable[[], Awaitable[Any]],
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    operation_name: str = "read",
) -> Any:
    """Повтор read-only вызова — всегда безопасен."""
    return await retry_async(
        operation,
        policy=policy,
        idempotency_key="read-only",
        operation_name=operation_name,
        safe_to_retry=True,
    )
