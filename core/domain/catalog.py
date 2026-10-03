"""Справочник инструментов: структура записи, приходящей из API.

Каталог — **не** константа в коде. Тикеры, названия, размеры лотов, UID и
торговые флаги живут в T-Invest API (``InstrumentsService``), поэтому любой
хардкод расходится с реальностью: делистинги, сплиты и смена лотности делают
зашитые значения неверными молча.

Здесь описана только форма записи. Данные приходят из API через адаптер
(``MarketDataPort.fetch_catalog`` / ``search_instruments``) и сохраняются в БД
через ``RepositoryPort`` — см. ``application/use_cases/manage_instrument_catalog.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.domain.entities import Instrument

#: Типы инструментов, для которых API отдаёт списки (InstrumentsService).
CATALOG_INSTRUMENT_TYPES: frozenset[str] = frozenset(
    {"share", "etf", "bond", "currency", "futures"}
)

#: Типы, которые составляют рабочий каталог бота по умолчанию.
#: Облигации намеренно не входят: для них нужен учёт НКД, которого в домене нет.
DEFAULT_CATALOG_TYPES: tuple[str, ...] = ("share", "etf", "currency", "futures")


class CatalogUnavailableError(RuntimeError):
    """Контур не может отдать справочник инструментов (например, бэктест без сети)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class InstrumentCatalogEntry:
    """Запись справочника: то, что API знает об инструменте.

    ``lot_size`` и ``uid`` — торгово-критичные поля: размер лота участвует в
    расчёте количества, поэтому значение берётся строго из API, а не из
    предположений. ``min_price_increment`` нужен для нормализации цены.
    """

    uid: str
    ticker: str
    class_code: str
    name: str
    lot_size: int
    currency: str = "RUB"
    instrument_type: str = "share"
    isin: str = ""
    figi: str = ""
    api_trade_available: bool = True
    buy_available: bool = True
    sell_available: bool = True
    for_iis: bool = False
    for_qual_investor: bool = False
    exchange: str = ""
    sector: str = ""
    country_of_risk: str = ""
    liquidity: bool = False
    min_price_increment: Decimal | None = None
    #: Момент сохранения записи в хранилище (проставляет репозиторий).
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.uid:
            msg = "instrument_uid записи каталога не может быть пустым"
            raise ValueError(msg)
        if not self.ticker:
            msg = "тикер записи каталога не может быть пустым"
            raise ValueError(msg)
        if self.lot_size <= 0:
            msg = f"lot_size записи каталога должен быть положительным, получен {self.lot_size}"
            raise ValueError(msg)

    @property
    def tradable(self) -> bool:
        """Доступен ли инструмент для торговли через API."""
        return self.api_trade_available and self.buy_available

    @property
    def label(self) -> str:
        """Человекочитаемая подпись для GUI: название и тикер."""
        return f"{self.name} ({self.ticker})" if self.name else self.ticker

    def to_instrument(self, *, is_benchmark: bool = False) -> Instrument:
        """Доменный инструмент корзины из записи справочника."""
        return Instrument(
            uid=self.uid,
            ticker=self.ticker,
            lot_size=self.lot_size,
            class_code=self.class_code,
            is_benchmark=is_benchmark,
            currency=self.currency or "RUB",
        )

    def matches(self, query: str) -> bool:
        """Нестрогий поиск по тикеру, названию, ISIN, FIGI и UID."""
        needle = query.strip().lower()
        if not needle:
            return False
        candidates = (self.ticker, self.name, self.isin, self.figi, self.uid)
        return any(needle in value.lower() for value in candidates if value)
