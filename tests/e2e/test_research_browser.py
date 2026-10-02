"""Browser fixtures are illustrative reports, never claimed as model performance."""

from uuid import uuid4

import pytest

from synthetic_trader.config import ExperimentConfig
from synthetic_trader.manager import ResearchManager
from synthetic_trader.storage import write_json
from tests.e2e import test_gui_browser as gui_fixtures

browser = gui_fixtures.browser
live_gui = gui_fixtures.live_gui

pytestmark = pytest.mark.e2e


@pytest.fixture
def browser_research(context, tmp_path):
    manager = ResearchManager(tmp_path / "research")
    context.research = manager
    run_id = uuid4().hex
    config = ExperimentConfig().model_dump(mode="json")
    point = {
        "symbol": "SBER",
        "asof": "2025-01-10T10:00:00Z",
        "p_trend": 0.8,
        "p_up": 0.7,
        "p_break": 0.2,
        "regime": "trend",
        "regime_panic": 0,
        "rv_20": 0.003,
    }
    curve = [
        {
            "date": "2025-01-10T00:00:00Z",
            "strategy": 1000000,
            "imoex": 1000000,
            "ma": 1000000,
            "rsi": 1000000,
        },
        {
            "date": "2025-01-11T00:00:00Z",
            "strategy": 1000100,
            "imoex": 1000200,
            "ma": 1000100,
            "rsi": 1000000,
        },
    ]
    report = {
        "run_id": run_id,
        "source": "demo",
        "config": config,
        "environment": {"python": "3.14.8"},
        "registry_stage": "research",
        "live_enabled": False,
        "source_hash": "f" * 64,
        "dataset": {"dataset_id": "b" * 64, "study_id": "c" * 64, "cutoff": "2026-03-31T21:00:00Z"},
        "candidate_model_hash": "d" * 64,
        "feature_version": "v1",
        "metrics": {
            "net_return": 0.0001,
            "sharpe": 0.1,
            "max_drawdown": -0.001,
            "trades": 2,
            "fees": "50",
        },
        "latest": [point],
        "probability_series": {
            "SBER": [point, {**point, "asof": "2025-01-10T11:00:00Z", "p_trend": 0.9}]
        },
        "equity_curve": curve,
        "selected_features": ["return_1"],
        "feature_importance": {"return_1": 0.2},
        "probabilities": {
            head: {
                "brier": 0.2,
                "raw_brier": 0.22,
                "samples": 100,
                "reliability": [{"predicted": 0.5, "observed": 0.6, "count": 100}],
            }
            for head in ["trend", "up", "break"]
        },
        "statistics": {"trials": 5, "dsr": None, "pbo": None, "spa": None, "reality_check": None},
        "checks": [{"id": "fixture", "label": "Fixture is not alpha", "passed": False}],
        "final_oos": {"consumed": False},
        "monitoring": {"drift_alert": False},
        "cpcv": {"computed": False, "reason": "Fixture, not computed"},
        "folds": [],
        "warnings": ["Browser fixture only, not a strategy result"],
    }
    directory = manager.root / "runs" / run_id
    write_json(directory / "config.json", config)
    write_json(
        directory / "status.json",
        {
            "id": run_id,
            "status": "completed",
            "progress": 100,
            "stage": "completed",
            "started_at": "2026-01-10T10:00:00Z",
        },
    )
    write_json(directory / "report.json", report)
    return manager, run_id


def login(page, url):
    page.goto(url + "/login")
    page.get_by_label("Пароль сессии").fill("dev-only-insecure-secret")
    page.get_by_role("button", name="Открыть панель").click()
    page.wait_for_url("**/control")


def test_research_charts_tabs_mobile_and_exact_final_dialog(browser, browser_research, live_gui):
    _, run_id = browser_research
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    login(page, live_gui)
    page.goto(live_gui + "/backtest?run=" + run_id)
    page.wait_for_function('document.querySelector("#metric-return").textContent === "0.01%"')
    assert "80.0" in page.locator("#p-trend").inner_text()
    assert page.locator("#equity-chart").evaluate("(c)=>c.width>0 && c.height>0")
    assert "ИСКУССТВЕННЫЕ" in page.locator("#source-chip").inner_text()
    page.locator('[data-tab="validation"]').click()
    assert page.locator("#calibration-chart").is_visible()
    page.locator("#final-open").click()
    assert page.locator("#final-confirm").is_disabled()
    page.locator("#final-confirmation").fill("yes")
    assert page.locator("#final-confirm").is_disabled()
    page.locator("#final-confirmation").fill("ОТКРЫТЬ FINAL OOS")
    assert page.locator("#final-confirm").is_enabled()
    page.locator("#final-cancel").click()  # Does not consume any final, even fixture.
    page.locator('[data-tab="overview"]').click()
    for width in [1440, 390]:
        page.set_viewport_size({"width": width, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
    assert not errors, errors
    page.close()


def test_new_unfinished_run_clears_old_metrics_and_sends_decimal_percent(
    browser, browser_research, live_gui
):
    manager, previous = browser_research
    run_id = uuid4().hex
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    payload = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def launch(route):
        if route.request.method == "POST":
            body = route.request.post_data_json
            payload.append(body)
            directory = manager.root / "runs" / run_id
            write_json(directory / "config.json", body)
            write_json(
                directory / "status.json",
                {
                    "id": run_id,
                    "status": "running",
                    "progress": 5,
                    "stage": "data",
                    "detail": "Fixture queued",
                    "started_at": "2026-01-10T11:00:00Z",
                },
            )
            route.fulfill(status=202, json={"id": run_id, "status": "running"})
        else:
            route.continue_()

    page.route("**/api/backtest/runs", launch)
    login(page, live_gui)
    page.goto(live_gui + "/backtest?run=" + previous)
    page.wait_for_function('document.querySelector("#metric-return").textContent === "0.01%"')
    page.locator('[data-tab="experiments"]').click()
    page.locator("#launch-experiment").click()
    page.wait_for_function('document.querySelector("#metric-return").textContent === "—"')
    assert page.locator("#p-trend").inner_text() == "—"
    assert page.locator("#final-open").is_disabled()
    assert page.locator("#download-json").get_attribute("href") is None
    assert payload[0]["risk"]["risk_per_trade"] == "0.01"
    assert payload[0]["risk"]["max_daily_drawdown"] == "0.03"
    assert page.locator("#cancel-run").is_visible()
    assert not errors, errors
    page.close()
