"""Read-only smoke probe for the real T-Invest sandbox SDK/API.

Usage:
    uv run python scripts/check_tinvest_sandbox.py
    uv run python scripts/check_tinvest_sandbox.py --schema-only

The network probe only performs reads. It never creates an account, tops up a
balance, submits/cancels an order, or contacts the production endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from typing import Any

EXPECTED_REQUEST_FIELDS: dict[str, dict[str, int]] = {
    "GetCandlesRequest": {
        "from_": 2,
        "to": 3,
        "interval": 4,
        "instrument_id": 5,
        "limit": 10,
    },
    "GetOrderBookRequest": {"depth": 2, "instrument_id": 3},
    "GetAccountsRequest": {"status": 1},
    "CloseSandboxAccountRequest": {"account_id": 1},
    "PortfolioRequest": {"account_id": 1},
    "InstrumentsRequest": {"instrument_status": 1},
    "InstrumentRequest": {"id_type": 1, "class_code": 2, "id": 3},
    "MarketDataServerSideStreamRequest": {"subscribe_candles_request": 1},
    "SubscribeCandlesRequest": {"subscription_action": 1, "instruments": 2},
    "CandleInstrument": {"interval": 2, "instrument_id": 3},
    "PostOrderRequest": {
        "quantity": 2,
        "price": 3,
        "direction": 4,
        "account_id": 5,
        "order_type": 6,
        "order_id": 7,
        "instrument_id": 8,
    },
    "CancelOrderRequest": {"account_id": 1, "order_id": 2, "order_id_type": 3},
    "GetOrderStateRequest": {
        "account_id": 1,
        "order_id": 2,
        "order_id_type": 4,
    },
    "GetTechAnalysisRequest": {
        "indicator_type": 1,
        "from_": 3,
        "to": 4,
        "interval": 5,
        "type_of_price": 6,
        "length": 7,
        "deviation": 8,
        "smoothing": 9,
        "instrument_id": 10,
    },
    "OpenSandboxAccountRequest": {"name": 1},
    "SandboxPayInRequest": {"account_id": 1, "amount": 2},
    # Каталог инструментов: методы списков и FindInstrument.
    "FindInstrumentRequest": {
        "query": 1,
        "instrument_kind": 2,
        "api_trade_available_flag": 3,
    },
}

#: Поля ответов, которые потребляют адаптер каталога. Номера — часть контракта:
#: по ним мапперы находят данные, поэтому смена proto ломает молча.
EXPECTED_RESPONSE_FIELDS: dict[str, dict[str, int]] = {
    "FindInstrumentResponse": {"instruments": 1},
    "InstrumentShort": {
        "isin": 1,
        "figi": 2,
        "ticker": 3,
        "class_code": 4,
        "instrument_type": 5,
        "name": 6,
        "uid": 7,
        "position_uid": 8,
        "api_trade_available_flag": 10,
    },
}


def _field_numbers(message_type: type[Any]) -> dict[str, int]:
    if not is_dataclass(message_type):
        raise TypeError(f"{message_type.__name__} is not an SDK dataclass")
    result: dict[str, int] = {}
    for field in fields(message_type):
        metadata = field.metadata.get("proto")
        if metadata is not None:
            result[field.name] = int(metadata.number)
    return result


def verify_sdk_schema() -> list[str]:
    """Fail if SDK request fields/numbers no longer match the API proto."""
    from t_tech.invest.grpc.sandbox import (
        CloseSandboxAccountRequest,
        OpenSandboxAccountRequest,
        SandboxPayInRequest,
    )
    from t_tech.invest.grpc.schemas import (
        CancelOrderRequest,
        CandleInstrument,
        CandleInterval,
        FindInstrumentRequest,
        GetAccountsRequest,
        GetCandlesRequest,
        GetOrderBookRequest,
        GetOrderStateRequest,
        GetTechAnalysisRequest,
        InstrumentRequest,
        InstrumentsRequest,
        InstrumentStatus,
        InstrumentType,
        MarketDataServerSideStreamRequest,
        OrderDirection,
        OrderExecutionReportStatus,
        OrderIdType,
        OrderType,
        PortfolioRequest,
        PostOrderRequest,
        SubscribeCandlesRequest,
        SubscriptionAction,
        SubscriptionInterval,
    )

    request_types = {
        cls.__name__: cls
        for cls in (
            GetCandlesRequest,
            GetOrderBookRequest,
            GetAccountsRequest,
            CloseSandboxAccountRequest,
            PortfolioRequest,
            InstrumentsRequest,
            InstrumentRequest,
            MarketDataServerSideStreamRequest,
            SubscribeCandlesRequest,
            CandleInstrument,
            PostOrderRequest,
            CancelOrderRequest,
            GetOrderStateRequest,
            GetTechAnalysisRequest,
            OpenSandboxAccountRequest,
            SandboxPayInRequest,
            FindInstrumentRequest,
        )
    }
    checked: list[str] = []
    for message_name, expected in EXPECTED_REQUEST_FIELDS.items():
        actual = _field_numbers(request_types[message_name])
        mismatches = {
            field_name: {"expected": number, "actual": actual.get(field_name)}
            for field_name, number in expected.items()
            if actual.get(field_name) != number
        }
        if mismatches:
            raise AssertionError(f"{message_name} SDK/proto mismatch: {mismatches}")
        checked.append(message_name)

    enum_values = {
        "CANDLE_INTERVAL_1_MIN": int(CandleInterval.CANDLE_INTERVAL_1_MIN),
        "CANDLE_INTERVAL_HOUR": int(CandleInterval.CANDLE_INTERVAL_HOUR),
        "CANDLE_INTERVAL_DAY": int(CandleInterval.CANDLE_INTERVAL_DAY),
        "ORDER_DIRECTION_BUY": int(OrderDirection.ORDER_DIRECTION_BUY),
        "ORDER_DIRECTION_SELL": int(OrderDirection.ORDER_DIRECTION_SELL),
        "ORDER_TYPE_MARKET": int(OrderType.ORDER_TYPE_MARKET),
        "ORDER_ID_TYPE_EXCHANGE": int(OrderIdType.ORDER_ID_TYPE_EXCHANGE),
        "ORDER_ID_TYPE_REQUEST": int(OrderIdType.ORDER_ID_TYPE_REQUEST),
        "ORDER_STATUS_FILL": int(OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL),
        "ORDER_STATUS_REJECTED": int(OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED),
        "ORDER_STATUS_CANCELLED": int(OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED),
        "ORDER_STATUS_NEW": int(OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW),
        "ORDER_STATUS_PARTIALLY_FILLED": int(
            OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL
        ),
        "INSTRUMENT_STATUS_BASE": int(InstrumentStatus.INSTRUMENT_STATUS_BASE),
        "SUBSCRIPTION_ACTION_SUBSCRIBE": int(SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE),
        "SUBSCRIPTION_INTERVAL_ONE_MINUTE": int(
            SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE
        ),
        "SUBSCRIPTION_INTERVAL_ONE_HOUR": int(SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_HOUR),
        "SUBSCRIPTION_INTERVAL_ONE_DAY": int(SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_DAY),
        "INDICATOR_TYPE_BB": int(GetTechAnalysisRequest.IndicatorType.INDICATOR_TYPE_BB),
        "INDICATOR_TYPE_EMA": int(GetTechAnalysisRequest.IndicatorType.INDICATOR_TYPE_EMA),
        "INDICATOR_TYPE_RSI": int(GetTechAnalysisRequest.IndicatorType.INDICATOR_TYPE_RSI),
        "INDICATOR_TYPE_MACD": int(GetTechAnalysisRequest.IndicatorType.INDICATOR_TYPE_MACD),
        "INDICATOR_TYPE_SMA": int(GetTechAnalysisRequest.IndicatorType.INDICATOR_TYPE_SMA),
        "INDICATOR_INTERVAL_ONE_MINUTE": int(
            GetTechAnalysisRequest.IndicatorInterval.INDICATOR_INTERVAL_ONE_MINUTE
        ),
        "INDICATOR_INTERVAL_ONE_HOUR": int(
            GetTechAnalysisRequest.IndicatorInterval.INDICATOR_INTERVAL_ONE_HOUR
        ),
        "INDICATOR_INTERVAL_ONE_DAY": int(
            GetTechAnalysisRequest.IndicatorInterval.INDICATOR_INTERVAL_ONE_DAY
        ),
        "TYPE_OF_PRICE_CLOSE": int(GetTechAnalysisRequest.TypeOfPrice.TYPE_OF_PRICE_CLOSE),
        "INSTRUMENT_TYPE_SHARE": int(InstrumentType.INSTRUMENT_TYPE_SHARE),
        "INSTRUMENT_TYPE_UNSPECIFIED": int(InstrumentType.INSTRUMENT_TYPE_UNSPECIFIED),
    }
    expected_enum_values = {
        "CANDLE_INTERVAL_1_MIN": 1,
        "CANDLE_INTERVAL_HOUR": 4,
        "CANDLE_INTERVAL_DAY": 5,
        "ORDER_DIRECTION_BUY": 1,
        "ORDER_DIRECTION_SELL": 2,
        "ORDER_TYPE_MARKET": 2,
        "ORDER_ID_TYPE_EXCHANGE": 1,
        "ORDER_ID_TYPE_REQUEST": 2,
        "ORDER_STATUS_FILL": 1,
        "ORDER_STATUS_REJECTED": 2,
        "ORDER_STATUS_CANCELLED": 3,
        "ORDER_STATUS_NEW": 4,
        "ORDER_STATUS_PARTIALLY_FILLED": 5,
        "INSTRUMENT_STATUS_BASE": 1,
        "SUBSCRIPTION_ACTION_SUBSCRIBE": 1,
        "SUBSCRIPTION_INTERVAL_ONE_MINUTE": 1,
        "SUBSCRIPTION_INTERVAL_ONE_HOUR": 4,
        "SUBSCRIPTION_INTERVAL_ONE_DAY": 5,
        "INDICATOR_TYPE_BB": 1,
        "INDICATOR_TYPE_EMA": 2,
        "INDICATOR_TYPE_RSI": 3,
        "INDICATOR_TYPE_MACD": 4,
        "INDICATOR_TYPE_SMA": 5,
        "INDICATOR_INTERVAL_ONE_MINUTE": 1,
        "INDICATOR_INTERVAL_ONE_HOUR": 4,
        "INDICATOR_INTERVAL_ONE_DAY": 5,
        "TYPE_OF_PRICE_CLOSE": 1,
        "INSTRUMENT_TYPE_SHARE": 2,
        "INSTRUMENT_TYPE_UNSPECIFIED": 0,
    }
    if enum_values != expected_enum_values:
        raise AssertionError(
            f"SDK enum mismatch: expected={expected_enum_values}, actual={enum_values}"
        )
    checked.append("CandleInterval/Orders/InstrumentStatus/Subscription/TechnicalAnalysis enums")

    # Response fields consumed by adapters/mappers are part of the contract too.
    from t_tech.invest.grpc import sandbox as sandbox_schemas
    from t_tech.invest.grpc import schemas

    response_fields = {
        "GetAccountsResponse": {"accounts"},
        "Account": {"id", "name", "status", "type"},
        "OpenSandboxAccountResponse": {"account_id"},
        "SandboxPayInResponse": {"balance"},
        "GetCandlesResponse": {"candles"},
        "HistoricCandle": {"open", "high", "low", "close", "volume", "time"},
        "GetOrderBookResponse": {"orderbook_ts", "bids", "asks"},
        "Order": {"price", "quantity"},
        "GetTechAnalysisResponse": {"technical_indicators"},
        "PostOrderResponse": {
            "order_id",
            "execution_report_status",
            "lots_executed",
            "executed_order_price",
            "order_request_id",
        },
        "OrderState": {"order_id", "execution_report_status", "lots_executed"},
        "PortfolioResponse": {
            "total_amount_portfolio",
            "total_amount_currencies",
            "total_amount_shares",
            "positions",
        },
        "PortfolioPosition": {"instrument_uid", "quantity", "average_position_price"},
        "Instrument": {"uid", "ticker", "class_code", "lot", "currency"},
        "InstrumentResponse": {"instrument"},
        "SharesResponse": {"instruments"},
        "EtfsResponse": {"instruments"},
        "CurrenciesResponse": {"instruments"},
        "FuturesResponse": {"instruments"},
        "BondsResponse": {"instruments"},
        "FindInstrumentResponse": {"instruments"},
        "Share": {
            "uid",
            "ticker",
            "class_code",
            "name",
            "lot",
            "currency",
            "isin",
            "figi",
            "api_trade_available_flag",
            "buy_available_flag",
            "sell_available_flag",
            "min_price_increment",
            "liquidity_flag",
        },
    }
    for type_name, expected_numbers in EXPECTED_RESPONSE_FIELDS.items():
        actual = _field_numbers(getattr(schemas, type_name))
        mismatches = {
            field_name: {"expected": number, "actual": actual.get(field_name)}
            for field_name, number in expected_numbers.items()
            if actual.get(field_name) != number
        }
        if mismatches:
            raise AssertionError(f"{type_name} SDK/proto mismatch: {mismatches}")
        checked.append(type_name)

    sandbox_response_types = {"OpenSandboxAccountResponse", "SandboxPayInResponse"}
    for type_name, expected_fields in response_fields.items():
        schema_module = sandbox_schemas if type_name in sandbox_response_types else schemas
        response_type = getattr(schema_module, type_name)
        actual_fields = set(_field_numbers(response_type))
        missing = expected_fields - actual_fields
        if missing:
            raise AssertionError(f"{type_name} SDK response fields missing: {sorted(missing)}")
        checked.append(type_name)
    return checked


async def _run_read_only_probe() -> None:
    from adapters.driven.sandbox.sandbox_adapter import create_sandbox_adapters
    from config.enums import ExecutionMode
    from config.settings import load_settings
    from core.domain.enums import Timeframe

    settings = load_settings()
    if settings.execution_mode is not ExecutionMode.SANDBOX:
        raise RuntimeError("SDK probe разрешен только при REDBOT_EXECUTION_MODE=sandbox")

    market_data, broker = await create_sandbox_adapters(settings)
    try:
        accounts = await broker.get_sandbox_accounts()
        print(f"SandboxService/GetSandboxAccounts: OK ({len(accounts)} account(s))")

        instruments = await broker.list_instruments()
        print(f"InstrumentsService/Shares: OK ({len(instruments)} instrument(s))")

        instrument = await market_data.resolve_instrument("SBER", "TQBR")
        print("InstrumentsService/GetInstrumentBy: OK (SBER.TQBR resolved)")

        now = datetime.now(tz=UTC)
        candles = await market_data.get_candles(
            instrument,
            Timeframe.D1,
            from_=now - timedelta(days=30),
            to=now,
        )
        if any(candle.timestamp.tzinfo is None for candle in candles):
            raise AssertionError("GetCandles returned a naive timestamp")
        print(f"MarketDataService/GetCandles: OK ({len(candles)} candle(s), interval=DAY)")

        book = await market_data.get_orderbook(instrument, depth=10)
        if book.captured_at.tzinfo is None:
            raise AssertionError("GetOrderBook returned a naive timestamp")
        print(
            "MarketDataService/GetOrderBook: OK "
            f"(bids={len(book.bids)}, asks={len(book.asks)}, depth=10)"
        )

        if settings.tbank.account_id:
            portfolio = await broker.get_portfolio()
            if portfolio is None:
                raise AssertionError("OperationsService/GetPortfolio returned no portfolio")
            print("OperationsService/GetPortfolio: OK")
        else:
            print("OperationsService/GetPortfolio: skipped (account_id is not configured)")
    finally:
        await market_data.aclose()
        await broker.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Проверить request/response поля и enum SDK без сети и токена",
    )
    args = parser.parse_args()

    try:
        checks = verify_sdk_schema()
        try:
            sdk_version = version("t-tech-investments")
        except PackageNotFoundError:
            sdk_version = "unknown"
        print(f"SDK schema: OK (t-tech-investments {sdk_version}; {', '.join(checks)})")
        if not args.schema_only:
            asyncio.run(_run_read_only_probe())
    except Exception as exc:  # noqa: BLE001 - CLI prints sanitized one-line diagnostics
        from config.settings import Settings

        try:
            token = Settings().tbank.api_token.get_secret_value()
        except Exception:  # noqa: BLE001 - settings may be unavailable before connection
            token = ""
        diagnostic = str(exc).replace(token, "[REDACTED]") if token else str(exc)
        print(f"SDK/API probe FAILED: {type(exc).__name__}: {diagnostic}")
        return 1
    print("SDK/API probe: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
