"""CLI account prompt accepts both its displayed index and full account ID."""

from __future__ import annotations

import pytest

from adapters.driving.cli.main import _parse_account_choice


@pytest.fixture
def account_choices() -> list[dict[str, object]]:
    return [
        {"id": "account-uuid-1", "name": "Песочница 1"},
        {"id": "account-uuid-2", "name": "Песочница 2"},
    ]


def test_account_prompt_accepts_numbered_choice(account_choices: list[dict[str, object]]) -> None:
    assert _parse_account_choice("1", account_choices) == "account-uuid-1"
    assert _parse_account_choice(" 2 ", account_choices) == "account-uuid-2"


def test_account_prompt_accepts_full_id(account_choices: list[dict[str, object]]) -> None:
    assert _parse_account_choice("account-uuid-2", account_choices) == "account-uuid-2"


def test_account_prompt_rejects_unknown_choice(account_choices: list[dict[str, object]]) -> None:
    with pytest.raises(ValueError, match="номер счёта"):
        _parse_account_choice("3", account_choices)
