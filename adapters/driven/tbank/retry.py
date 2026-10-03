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


@dataclass(frozen=True, slots=True, kw_only=True)
class RateLimitHeaders:
    """Снимок заголовков квотирования T-Invest API (раздел 1.2 ТЗ).

    Поддерживает ``x-ratelimit-limit``, ``x-ratelimit-remaining``, ``x-ratelimit-reset``.
    """

    limit: int
    remaining: int
    reset_seconds: float


def parse_ratelimit_metadata(metadata: Any) -> RateLimitHeaders | None:
    """Извлекает заголовки ``x-ratelimit-*`` из gRPC metadata или HTTP-словаря."""
    if metadata is None:
        return None

    kv: dict[str, str] = {}
    if isinstance(metadata, dict):
        for k, v in metadata.items():
            kv[str(k).lower()] = str(v)
    else:
        try:
            for item in metadata:
                if hasattr(item, "key") and hasattr(item, "value"):
                    kv[str(item.key).lower()] = str(item.value)
                elif isinstance(item, tuple) and len(item) == 2:
                    kv[str(item[0]).lower()] = str(item[1])
        except TypeError:
            return None

    if not any(k.startswith("x-ratelimit-") for k in kv):
        return None

    try:
        raw_lim = kv.get("x-ratelimit-limit", "100").split(",")[0].strip()
        raw_rem = kv.get("x-ratelimit-remaining", "100").split(",")[0].strip()
        raw_rst = kv.get("x-ratelimit-reset", "1").split(",")[0].strip()
        return RateLimitHeaders(
            limit=max(1, int(float(raw_lim))),
            remaining=max(0, int(float(raw_rem))),
            reset_seconds=max(0.0, float(raw_rst)),
        )
    except (ValueError, TypeError):
        return None


class AdaptiveRateLimitGovernor:
    """Адаптивный регулятор частоты запросов по заголовкам ``x-ratelimit-*`` (раздел 1.2 ТЗ)."""

    def __init__(self, *, low_watermark: int = 3) -> None:
        self.low_watermark = max(1, low_watermark)
        self.last_headers: RateLimitHeaders | None = None

    def observe(self, metadata: Any) -> RateLimitHeaders | None:
        parsed = parse_ratelimit_metadata(metadata)
        if parsed is not None:
            self.last_headers = parsed
        return parsed

    async def throttle_if_needed(self) -> float:
        """Если остаток квоты ``x-ratelimit-remaining <= low_watermark``, ждёт ``reset_seconds``."""
        snap = self.last_headers
        if snap is None or snap.remaining > self.low_watermark:
            return 0.0
        pause = min(max(snap.reset_seconds, 0.05), 60.0)
        await asyncio.sleep(pause)
        return pause


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
                meta = getattr(exc, "trailing_metadata", None)
                meta_obj = meta() if callable(meta) else meta
                rl = parse_ratelimit_metadata(meta_obj)
                if rl is not None and rl.reset_seconds > 0:
                    delay = max(delay, min(rl.reset_seconds, 60.0))
                else:
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
