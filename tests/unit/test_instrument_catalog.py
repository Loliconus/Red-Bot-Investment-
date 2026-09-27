"""Каталог инструментов: данные из API, сохранённые в БД.

Проверяем главное свойство нового подхода: хардкода каталога нет, данные
приходят из ``InstrumentsService``, сохраняются в хранилище и читаются оттуда
GUI, бэктест и поиском по названию.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from adapters.driven.backtest.replay_adapter import BacktestReplayAdapter
from application.composition import AppContext
from application.use_cases.manage_instrument_catalog import (
    CATALOG_UPDATED_AT_KEY,
    catalog_status,
    catalog_view,
    ensure_catalog_fresh,
    get_catalog_name,
    is_catalog_stale,
    list_catalog_entries,
    list_catalog_views,
    refresh_instrument_catalog,
    search_instruments,
)
from application.use_cases.manage_instruments import extract_ticker
from core.domain.catalog import CatalogUnavailableError, InstrumentCatalogEntry
from tests.fakes import make_catalog_entry


def _catalog() -> list[InstrumentCatalogEntry]:
    return [
        make_catalog_entry(uid="uid-sber", ticker="SBER", name="Сбербанк", lot_size=10),
        make_catalog_entry(uid="uid-vtbr", ticker="VTBR", name="Банк ВТБ", lot_size=10000),
        make_catalog_entry(
            uid="uid-usd",
            ticker="USD000UTSTOM",
            name="Доллар США",
            class_code="CETS",
            lot_size=1,
            instrument_type="currency",
            currency="USD",
            liquidity=False,
        ),
    ]


async def test_catalog_entry_validates_trade_critical_fields() -> None:
    with pytest.raises(ValueError, match="lot_size"):
        InstrumentCatalogEntry(uid="u", ticker="SBER", class_code="TQBR", name="", lot_size=0)
    with pytest.raises(ValueError, match="uid"):
        InstrumentCatalogEntry(uid="", ticker="SBER", class_code="TQBR", name="", lot_size=1)


def test_catalog_entry_to_instrument_keeps_lot_and_uid() -> None:
    entry = make_catalog_entry(uid="uid-sber", ticker="SBER", lot_size=10)
    instrument = entry.to_instrument()
    assert instrument.uid == "uid-sber"
    assert instrument.lot_size == 10
    assert instrument.currency == "RUB"


def test_catalog_entry_matches_ticker_name_and_isin() -> None:
    entry = make_catalog_entry(uid="u", ticker="SBER", name="Сбербанк", isin="RU0009029540")
    assert entry.matches("sber")
    assert entry.matches("Сбербанк")
    assert entry.matches("RU0009029540")
    assert not entry.matches("GAZP")


def test_catalog_view_shape_for_gui() -> None:
    view = catalog_view(make_catalog_entry(uid="u", ticker="SBER", name="Сбербанк"))
    assert view["ticker"] == "SBER"
    assert view["label"] == "Сбербанк (SBER)"
    assert view["tradable"] is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("VTBR", "VTBR"),
        ("vtbr", "VTBR"),
        ("VTBR (Банк ВТБ)", "VTBR"),
        ("Газпром (GAZP)", "GAZP"),
        ("Сбербанк", "СБЕРБАНК"),
        ("  SBER  ", "SBER"),
    ],
)
def test_extract_ticker_parses_user_input(raw: str, expected: str) -> None:
    assert extract_ticker(raw) == expected


async def test_refresh_persists_api_catalog_and_replaces_stale_types(
    context: AppContext,
) -> None:
    context.market_data.set_catalog(_catalog())

    result = await refresh_instrument_catalog(context, instrument_types=["share"])

    assert result.fetched == 2
    assert result.types == ("share",)
    saved = await list_catalog_entries(context, limit=10)
    assert {entry.ticker for entry in saved} == {"SBER", "VTBR"}
    assert await context.repository.get_operational_value(CATALOG_UPDATED_AT_KEY)
    assert await context.repository.count_catalog_entries() == 2


async def test_refresh_replaces_previous_entries_of_same_type(context: AppContext) -> None:
    context.market_data.set_catalog(
        [make_catalog_entry(uid="uid-old", ticker="OLD", name="Старая")]
    )
    await refresh_instrument_catalog(context, instrument_types=["share"])

    context.market_data.set_catalog([make_catalog_entry(uid="uid-new", ticker="NEW", name="Новая")])
    await refresh_instrument_catalog(context, instrument_types=["share"])

    tickers = {entry.ticker for entry in await list_catalog_entries(context, limit=10)}
    assert tickers == {"NEW"}


async def test_refresh_keeps_other_instrument_types(context: AppContext) -> None:
    context.market_data.set_catalog(_catalog())
    await refresh_instrument_catalog(context, instrument_types=["share", "currency"])
    assert await context.repository.count_catalog_entries(["share"]) == 2
    assert await context.repository.count_catalog_entries(["currency"]) == 1

    await refresh_instrument_catalog(context, instrument_types=["share"])
    assert await context.repository.count_catalog_entries(["share"]) == 2
    assert await context.repository.count_catalog_entries(["currency"]) == 1


async def test_empty_catalog_is_stale_and_gets_refreshed(context: AppContext) -> None:
    context.market_data.set_catalog(_catalog())
    assert await is_catalog_stale(context)

    refreshed = await ensure_catalog_fresh(context)
    assert refreshed is True
    assert await is_catalog_stale(context) is False
    assert context.market_data.calls.count("fetch_catalog:share,etf,currency,futures") == 1


async def test_fresh_catalog_is_not_refetched(context: AppContext) -> None:
    context.market_data.set_catalog(_catalog())
    await refresh_instrument_catalog(context)
    context.market_data.calls.clear()

    assert await ensure_catalog_fresh(context) is False
    assert "fetch_catalog:share,etf,currency,futures" not in context.market_data.calls


async def test_stale_catalog_is_refreshed_on_demand(context: AppContext) -> None:
    context.market_data.set_catalog(_catalog())
    await refresh_instrument_catalog(context)
    await context.repository.set_operational_value(
        CATALOG_UPDATED_AT_KEY,
        (datetime.now(tz=UTC) - timedelta(hours=25)).isoformat(),
    )

    assert await is_catalog_stale(context)
    assert await ensure_catalog_fresh(context) is True


async def test_search_prefers_saved_catalog(context: AppContext) -> None:
    context.market_data.set_catalog(_catalog())
    await refresh_instrument_catalog(context)

    found = await search_instruments(context, "втб")
    assert [entry.ticker for entry in found] == ["VTBR"]
    assert not any(call.startswith("search_instruments") for call in context.market_data.calls)


async def test_search_falls_back_to_api_and_persists_result(context: AppContext) -> None:
    context.market_data.set_catalog(_catalog())

    found = await search_instruments(context, "Сбербанк")
    assert [entry.ticker for entry in found] == ["SBER"]
    assert "search_instruments:Сбербанк" in context.market_data.calls

    saved = await context.repository.find_catalog_entry("SBER", "TQBR")
    assert saved is not None
    assert saved.lot_size == 10

    context.market_data.calls.clear()
    again = await search_instruments(context, "Сбербанк")
    assert [entry.ticker for entry in again] == ["SBER"]
    assert not any(call.startswith("search_instruments") for call in context.market_data.calls)


async def test_search_ignores_too_short_query(context: AppContext) -> None:
    context.market_data.set_catalog(_catalog())
    assert await search_instruments(context, "s") == []
    assert context.market_data.calls == []


async def test_list_catalog_views_filters_and_formats(context: AppContext) -> None:
    context.market_data.set_catalog(_catalog())
    await refresh_instrument_catalog(context)

    views = await list_catalog_views(context, query="втб", limit=10)
    assert [view["ticker"] for view in views] == ["VTBR"]

    by_type = await list_catalog_views(context, instrument_types=["currency"], limit=10)
    assert [view["ticker"] for view in by_type] == ["USD000UTSTOM"]

    tradable = await list_catalog_views(context, tradable_only=True, limit=10)
    assert {view["ticker"] for view in tradable} == {"SBER", "VTBR", "USD000UTSTOM"}


async def test_get_catalog_name_uses_saved_data(context: AppContext) -> None:
    context.market_data.set_catalog(_catalog())
    await refresh_instrument_catalog(context)

    assert await get_catalog_name(context, "SBER") == "Сбербанк"
    assert await get_catalog_name(context, "UNKNOWN") == "UNKNOWN"


async def test_catalog_status_reports_count_and_age(context: AppContext) -> None:
    context.market_data.set_catalog(_catalog())
    assert await catalog_status(context) == {"count": 0, "updated_at": None, "stale": True}

    await refresh_instrument_catalog(context)
    status = await catalog_status(context)
    assert status["count"] == 3
    assert status["stale"] is False
    assert isinstance(status["updated_at"], str)


# ------------------------------------------------------------------ мапперы
class _StubShare:
    """Минимальная копия ответа InstrumentsService/Shares."""

    uid = "uid-sber"
    ticker = "SBER"
    class_code = "TQBR"
    name = "Сбербанк"
    lot = 10
    currency = "rub"
    instrument_type = "share"
    isin = "RU0009029540"
    figi = "BBG004730N88"
    api_trade_available_flag = True
    buy_available_flag = True
    sell_available_flag = True
    for_iis_flag = True
    for_qual_investor_flag = False
    exchange = "MOEX"
    sector = "financial"
    country_of_risk_name = "Россия"
    liquidity_flag = True
    min_price_increment = None


class _StubCurrency(_StubShare):
    uid = "uid-usd"
    ticker = "USD000UTSTOM"
    class_code = "CETS"
    name = "Доллар США"
    lot = 1
    currency = "usd"
    instrument_type = "currency"
    liquidity_flag = False


class _StubShort:
    """Минимальная копия InstrumentShort из FindInstrument."""

    uid = "uid-gazp"
    ticker = "GAZP"
    class_code = "TQBR"
    name = "Газпром"
    lot = 10
    isin = "RU0007661625"
    figi = "BBG004730RP0"
    api_trade_available_flag = True
    for_iis_flag = False
    for_qual_investor_flag = False
    instrument_type = ""
    instrument_kind = type("Kind", (), {"name": "INSTRUMENT_TYPE_SHARE"})()


def test_instrument_list_to_catalog_maps_api_fields() -> None:
    from adapters.driven.tbank.mappers import instrument_list_to_catalog

    entries = instrument_list_to_catalog([_StubShare(), _StubCurrency()], "share")
    assert len(entries) == 2

    sber = entries[0]
    assert (sber.uid, sber.ticker, sber.class_code) == ("uid-sber", "SBER", "TQBR")
    assert (sber.name, sber.lot_size, sber.currency) == ("Сбербанк", 10, "RUB")
    assert sber.instrument_type == "share"
    assert sber.isin == "RU0009029540"
    assert sber.api_trade_available is True
    assert sber.for_iis is True
    assert sber.sector == "financial"
    assert sber.liquidity is True

    usd = entries[1]
    assert usd.instrument_type == "currency"
    assert usd.currency == "USD"
    assert usd.liquidity is False


def test_instrument_list_to_catalog_skips_incomplete_rows() -> None:
    from adapters.driven.tbank.mappers import instrument_list_to_catalog

    class _NoTicker:
        uid = "uid-x"
        ticker = ""
        lot = 1

    assert instrument_list_to_catalog([_NoTicker()], "share") == []


def test_instrument_list_to_catalog_converts_price_increment(monkeypatch: Any) -> None:
    from adapters.driven.tbank import mappers

    class _WithIncrement(_StubShare):
        min_price_increment = object()

    monkeypatch.setattr(mappers, "quotation_to_decimal", lambda value: Decimal("0.02"))
    entries = mappers.instrument_list_to_catalog([_WithIncrement()], "share")
    assert entries[0].min_price_increment == Decimal("0.02")


def test_instrument_list_to_catalog_without_increment_keeps_none() -> None:
    from adapters.driven.tbank.mappers import instrument_list_to_catalog

    entries = instrument_list_to_catalog([_StubShare()], "share")
    assert entries[0].min_price_increment is None


def test_instrument_short_to_catalog_entry_uses_kind_enum() -> None:
    from adapters.driven.tbank.mappers import instrument_short_to_catalog_entry

    entry = instrument_short_to_catalog_entry(_StubShort())
    assert entry.ticker == "GAZP"
    assert entry.instrument_type == "share"
    assert entry.isin == "RU0007661625"
    assert entry.api_trade_available is True
    # InstrumentShort не содержит валюту котировки — домен подставит RUB.
    assert entry.currency == ""
    assert entry.to_instrument().currency == "RUB"


# ------------------------------------------------------------------ бэктест
async def test_replay_resolves_instrument_from_saved_catalog(tmp_path: Any) -> None:
    from tests.fakes import InMemoryRepository

    repository = InMemoryRepository()
    await repository.save_catalog_entries(_catalog())
    replay = BacktestReplayAdapter(data_dir=tmp_path / "history", repository=repository)

    instrument = await replay.resolve_instrument("SBER", "TQBR")
    assert instrument.uid == "uid-sber"
    assert instrument.lot_size == 10

    vtbr = await replay.resolve_instrument("VTBR", "TQBR")
    assert vtbr.lot_size == 10000


async def test_replay_search_reads_saved_catalog(tmp_path: Any) -> None:
    from tests.fakes import InMemoryRepository

    repository = InMemoryRepository()
    await repository.save_catalog_entries(_catalog())
    replay = BacktestReplayAdapter(data_dir=tmp_path / "history", repository=repository)

    found = await replay.search_instruments("доллар")
    assert [entry.ticker for entry in found] == ["USD000UTSTOM"]


async def test_replay_without_repository_falls_back_to_synthetic_instrument(
    tmp_path: Any,
) -> None:
    replay = BacktestReplayAdapter(data_dir=tmp_path / "history")

    instrument = await replay.resolve_instrument("SBER", "TQBR")
    assert instrument.uid == "backtest-sber"
    assert instrument.ticker == "SBER"

    benchmark = await replay.resolve_instrument("IMOEX", "SPBFUT")
    assert benchmark.is_benchmark is True


async def test_replay_has_no_network_catalog(tmp_path: Any) -> None:
    replay = BacktestReplayAdapter(data_dir=tmp_path / "history")
    with pytest.raises(CatalogUnavailableError):
        await replay.fetch_catalog(["share"])
