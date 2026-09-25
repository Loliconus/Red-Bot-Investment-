"""Юнит-тесты журнала самоанализа: MFE/MAE, гипотезы, советы."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from core.domain.enums import HypothesisStatus, Timeframe, TradeVerdict
from core.domain.value_objects import OHLCV
from core.journal.advisory import Advice, build_advice, has_actionable
from core.journal.hypothesis_engine import (
    OVERFIT_THRESHOLD,
    Hypothesis,
    mean,
    propose_efficiency_hypotheses,
    split_by_condition,
    walk_forward_efficiency,
    walk_forward_split,
)
from core.journal.trade_review import (
    TradeExcursion,
    TradeReview,
    classify_verdict,
    compute_excursion,
    compute_exit_efficiency,
    compute_post_exit_drift,
)

NOW = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
ZERO = Decimal("0")


def _candle(high: Decimal, low: Decimal, *, offset_hours: int = 0) -> OHLCV:
    return OHLCV(
        open=low,
        high=high,
        low=low,
        close=(high + low) / 2,
        volume=100,
        timestamp=NOW + timedelta(hours=offset_hours),
        timeframe=Timeframe.H1,
    )


# ------------------------------------------------------------------ MFE / MAE
def test_compute_excursion_finds_extremes() -> None:
    candles = [
        _candle(Decimal("105"), Decimal("98")),
        _candle(Decimal("115"), Decimal("100")),
        _candle(Decimal("110"), Decimal("94")),
    ]
    excursion = compute_excursion(candles)
    assert excursion.mfe_price == Decimal("115")
    assert excursion.mae_price == Decimal("94")


def test_compute_excursion_rejects_empty() -> None:
    with pytest.raises(ValueError, match="без свечей"):
        compute_excursion([])


def test_excursion_pct_from_entry() -> None:
    excursion = TradeExcursion(mfe_price=Decimal("110"), mae_price=Decimal("95"))
    assert excursion.mfe_pct(Decimal("100")) == Decimal("0.1")
    assert excursion.mae_pct(Decimal("100")) == Decimal("-0.05")


def test_exit_efficiency_captured_share() -> None:
    # MFE 120, выход 110, вход 100 → поймали 50% движения
    assert compute_exit_efficiency(Decimal("100"), Decimal("110"), Decimal("120")) == Decimal("0.5")


def test_exit_efficiency_zero_when_no_favorable_move() -> None:
    assert compute_exit_efficiency(Decimal("100"), Decimal("105"), Decimal("100")) == Decimal("0")


def test_post_exit_drift_sign() -> None:
    assert compute_post_exit_drift(Decimal("100"), Decimal("110")) == Decimal("0.1")
    assert compute_post_exit_drift(Decimal("100"), Decimal("90")) == Decimal("-0.1")


# ------------------------------------------------------------------ вердикты
def test_verdict_premature_exit() -> None:
    """Кейс из ТЗ: закрыл в 10, а день закрылся в 25."""
    verdict = classify_verdict(
        entry=Decimal("100"),
        exit_price=Decimal("110"),
        mfe_price=Decimal("112"),
        post_exit_drift_pct=Decimal("0.13"),
    )
    assert verdict is TradeVerdict.PREMATURE_EXIT


def test_verdict_overstayed() -> None:
    verdict = classify_verdict(
        entry=Decimal("100"),
        exit_price=Decimal("104"),
        mfe_price=Decimal("120"),
        post_exit_drift_pct=Decimal("0.0"),
    )
    assert verdict is TradeVerdict.OVERSTAYED


def test_verdict_good_exit() -> None:
    verdict = classify_verdict(
        entry=Decimal("100"),
        exit_price=Decimal("118"),
        mfe_price=Decimal("120"),
        post_exit_drift_pct=Decimal("0.0"),
    )
    assert verdict is TradeVerdict.GOOD_EXIT


def test_verdict_loss() -> None:
    verdict = classify_verdict(
        entry=Decimal("100"),
        exit_price=Decimal("95"),
        mfe_price=Decimal("105"),
        post_exit_drift_pct=Decimal("0.0"),
    )
    assert verdict is TradeVerdict.LOSS


def test_verdict_correct_caution() -> None:
    verdict = classify_verdict(
        entry=Decimal("100"),
        exit_price=Decimal("100.1"),
        mfe_price=Decimal("100.2"),
        post_exit_drift_pct=Decimal("0.0"),
    )
    assert verdict is TradeVerdict.CORRECT_CAUTION


def test_trade_review_from_prices_computes_all() -> None:
    review = TradeReview.from_prices(
        trade_plan_id=uuid4(),
        entry_price=Decimal("100"),
        exit_price=Decimal("110"),
        mfe_price=Decimal("120"),
        mae_price=Decimal("95"),
        price_at_session_close=Decimal("115"),
        closed_at=NOW,
        quantity=10,
    )
    assert review.mfe == Decimal("0.2")
    assert review.mae == Decimal("-0.05")
    assert review.exit_efficiency == Decimal("0.5")
    # 115/110 - 1 = 0.04545...
    assert abs(review.post_exit_drift_pct - Decimal("0.04545")) < Decimal("0.0001")
    # Реализовано ровно 50% движения, а цена ушла ещё на 4.5% → преждевременный выход
    assert review.verdict is TradeVerdict.PREMATURE_EXIT
    assert review.realized_pnl == Decimal("100")


# ------------------------------------------------------------------ гипотезы
def _review(pnl: Decimal, efficiency: Decimal, *, day: int = 1) -> TradeReview:
    return TradeReview(
        trade_plan_id=uuid4(),
        entry_price=Decimal("100"),
        exit_price=Decimal("100") + pnl,
        mfe=Decimal("0.05"),
        mae=Decimal("-0.02"),
        exit_efficiency=efficiency,
        price_at_session_close=Decimal("101"),
        price_at_t_plus_1d=None,
        price_at_t_plus_3d=None,
        post_exit_drift_pct=Decimal("0.0"),
        verdict=TradeVerdict.GOOD_EXIT if pnl > ZERO else TradeVerdict.LOSS,
        closed_at=NOW + timedelta(days=day),
        holding_seconds=3600,
        realized_pnl=pnl,
    )


def _history(size: int = 60) -> list[TradeReview]:
    """История, где сделки с низкой эффективностью стабильно хуже."""
    reviews: list[TradeReview] = []
    for i in range(size):
        if i % 2 == 0:
            reviews.append(_review(Decimal("-100"), Decimal("0.2"), day=i))
        else:
            reviews.append(_review(Decimal("300"), Decimal("0.8"), day=i))
    return reviews


def test_mean_of_values() -> None:
    assert mean([Decimal("1"), Decimal("3")]) == Decimal("2")
    assert mean([]) == ZERO


def test_split_by_condition() -> None:
    history = _history(20)
    matched, rest = split_by_condition(history, lambda r: r.exit_efficiency < Decimal("0.5"))
    assert len(matched) == 10
    assert len(rest) == 10


def test_hypothesis_lifecycle() -> None:
    hypothesis = Hypothesis.create(
        text="тест",
        condition_description="x < 1",
        sample_size=40,
        confidence=Decimal("0.8"),
    )
    assert hypothesis.status is HypothesisStatus.PROPOSED
    assert hypothesis.can_enter_testing(30)

    status = hypothesis.record_walk_forward(Decimal("0.7"))
    assert status is HypothesisStatus.CONFIRMED

    hypothesis.mark_applied()
    assert hypothesis.status is HypothesisStatus.APPLIED


def test_hypothesis_rejects_low_walk_forward() -> None:
    hypothesis = Hypothesis.create(
        text="тест", condition_description="x", sample_size=40, confidence=Decimal("0.8")
    )
    status = hypothesis.record_walk_forward(Decimal("0.1"))
    assert status is HypothesisStatus.REJECTED
    assert hypothesis.is_overfitted


def test_hypothesis_cannot_apply_before_confirmation() -> None:
    hypothesis = Hypothesis.create(
        text="тест", condition_description="x", sample_size=40, confidence=Decimal("0.8")
    )
    with pytest.raises(ValueError, match="CONFIRMED"):
        hypothesis.mark_applied()


def test_overfit_threshold_matches_spec() -> None:
    """Порог переобучения зафиксирован ТЗ: ниже 0.3 — переобучение."""
    assert Decimal("0.3") == OVERFIT_THRESHOLD


def test_walk_forward_split_keeps_time_order() -> None:
    train, test = walk_forward_split(_history(20), train_ratio=0.6)
    assert len(train) == 12
    assert len(test) == 8
    assert train[-1].closed_at <= test[0].closed_at


def test_walk_forward_efficiency_ratio() -> None:
    history = _history(60)
    efficiency = walk_forward_efficiency(history, lambda r: r.exit_efficiency < Decimal("0.5"))
    assert efficiency is not None
    assert efficiency != ZERO


def test_propose_hypotheses_requires_min_sample() -> None:
    assert propose_efficiency_hypotheses(_history(10), min_sample_size=30) == []


def test_propose_hypotheses_returns_proposals_on_rich_history() -> None:
    proposals = propose_efficiency_hypotheses(_history(80), min_sample_size=30)
    assert proposals, "на 80 сделках гипотезы обязаны находиться"
    texts = " ".join(p.text for p in proposals)
    assert "эффективность" in texts or "преждевремен" in texts


def test_proposed_hypotheses_have_evidence_and_action() -> None:
    proposals = propose_efficiency_hypotheses(_history(80), min_sample_size=30)
    for hypothesis in proposals:
        assert hypothesis.suggested_action
        assert hypothesis.evidence
        assert hypothesis.sample_size >= 30


# ------------------------------------------------------------------ советы
def test_build_advice_sorts_confirmed_first() -> None:
    confirmed = Hypothesis.create(
        text="подтверждено",
        condition_description="x",
        sample_size=50,
        confidence=Decimal("0.5"),
    )
    confirmed.status = HypothesisStatus.CONFIRMED
    proposed = Hypothesis.create(
        text="предложение",
        condition_description="y",
        sample_size=50,
        confidence=Decimal("0.9"),
    )
    advice = build_advice([proposed, confirmed])
    assert advice[0].text == "подтверждено"
    assert advice[0].render().startswith("[CONFIRMED]")
    assert all(isinstance(item, Advice) for item in advice)


def test_has_actionable_detects_confirmed() -> None:
    confirmed = Hypothesis.create(
        text="x", condition_description="x", sample_size=30, confidence=Decimal("0.9")
    )
    confirmed.status = HypothesisStatus.CONFIRMED
    assert has_actionable([confirmed])
    assert not has_actionable([])
