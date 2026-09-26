"""Offline checks that used SDK request/response models still match protobuf contracts."""

from __future__ import annotations

from scripts.check_tinvest_sandbox import verify_sdk_schema


def test_sdk_request_fields_enums_and_mapper_response_fields_match_contract() -> None:
    checked = verify_sdk_schema()

    assert "GetCandlesRequest" in checked
    assert "PostOrderRequest" in checked
    assert "GetTechAnalysisResponse" in checked
