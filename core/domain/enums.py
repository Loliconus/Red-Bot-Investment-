"""Единая точка правды по всем доменным статусам и перечислениям.

Все перечисления — ``enum.StrEnum``: значения остаются строками, что критично
для сериализации в БД/JSON и для SQL-запросов при дата-майнинге (статус пишется
как обычная строка, без дополнительного маппинга).

Имена членов — ``UPPER_CASE``, значения — ``snake_case``.
"""

from enum import StrEnum


class Timeframe(StrEnum):
    """Рабочие таймфреймы мультитаймфреймового анализа."""

    D1 = "1d"  # контекст и bias
    H1 = "1h"  # торговый сетап
    M1 = "1m"  # тайминг входа + стакан


class Trend(StrEnum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"


class MarketRegime(StrEnum):
    TRENDING = "trending"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"


class VolatilityRegime(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class TradePlanStatus(StrEnum):
    """Жизненный цикл торгового плана.

    ``CLOSED_*`` разделены намеренно: это позволяет фильтровать сделки по
    причине закрытия прямо в SQL, без JOIN на дополнительную таблицу.
    """

    PROPOSED = "proposed"  # сформирован, ещё не подан на исполнение
    PENDING = "pending"  # ордер выставлен, ждём заполнения
    ACTIVE = "active"  # позиция открыта
    CLOSED_TARGET = "closed_target"
    CLOSED_HARD_STOP = "closed_hard_stop"
    CLOSED_INVALIDATION = "closed_invalidation"
    CLOSED_TIME_EXIT = "closed_time_exit"
    CLOSED_MANUAL = "closed_manual"
    REJECTED = "rejected"  # риск-модуль отклонил до исполнения
    CANCELLED = "cancelled"


class DecisionType(StrEnum):
    ENTER = "enter"
    EXIT = "exit"
    HOLD = "hold"
    REJECT = "reject"


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    TESTING = "testing"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    APPLIED = "applied"


class TradeVerdict(StrEnum):
    """Оценка качества выхода из сделки."""

    GOOD_EXIT = "good_exit"
    PREMATURE_EXIT = "premature_exit"  # закрыл в 10, а разлетелось до 25
    OVERSTAYED = "overstayed"  # держал слишком долго, отдал прибыль
    CORRECT_CAUTION = "correct_caution"  # низкий MFE, выход всё равно верным
    LOSS = "loss"


class OrderSide(StrEnum):
    """Только LONG: шорты исключены из домена полностью."""

    BUY = "buy"


class OrderStatus(StrEnum):
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ExitReason(StrEnum):
    TARGET = "target"
    HARD_STOP = "hard_stop"
    INVALIDATION = "invalidation"
    TIME_EXIT = "time_exit"
    MANUAL = "manual"


TERMINAL_STATUSES = frozenset(
    {
        TradePlanStatus.CLOSED_TARGET,
        TradePlanStatus.CLOSED_HARD_STOP,
        TradePlanStatus.CLOSED_INVALIDATION,
        TradePlanStatus.CLOSED_TIME_EXIT,
        TradePlanStatus.CLOSED_MANUAL,
        TradePlanStatus.REJECTED,
        TradePlanStatus.CANCELLED,
    }
)
