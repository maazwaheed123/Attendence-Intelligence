"""Confidence v1: weights, bands, review cap, explanations."""

import pytest

from app.governance.confidence import WEIGHTS, Signals, band, score, unavailable

pytestmark = pytest.mark.unit


def _s(**kw):
    base = dict(
        sql_valid=True,
        dual_path="agree",
        grounded=True,
        citation_coverage=1.0,
        mean_extraction_confidence=1.0,
    )
    return Signals(**{**base, **kw})


def test_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_all_signals_perfect_is_one_and_high():
    c = score(_s())
    assert (c.value, c.band) == (1.0, "high")


def test_template_only_counts_as_agreement():
    assert score(_s(dual_path="template_only")).value == score(_s()).value


def test_each_signal_moves_the_score():
    full = score(_s()).value
    assert score(_s(sql_valid=False)).value == pytest.approx(full - 0.20)
    assert score(_s(grounded=False)).value == pytest.approx(full - 0.25)
    assert score(_s(citation_coverage=0.0)).value == pytest.approx(full - 0.15)
    assert score(_s(mean_extraction_confidence=0.4)).value == pytest.approx(full - 0.09)
    assert score(_s(dual_path="disagree")).value == pytest.approx(full - 0.175)
    assert score(_s(dual_path="llm_only")).value == pytest.approx(full - 0.125)


def test_bands():
    assert band(0.85, 0.85, 0.60) == "high"
    assert band(0.849, 0.85, 0.60) == "medium"
    assert band(0.60, 0.85, 0.60) == "medium"
    assert band(0.599, 0.85, 0.60) == "low"
    assert score(_s(dual_path="disagree")).band == "medium"
    assert score(_s(grounded=False, citation_coverage=0.0)).band == "medium"
    assert (
        score(_s(grounded=False, citation_coverage=0.0, mean_extraction_confidence=0)).band == "low"
    )


def test_needs_review_is_capped_low():
    c = score(_s(needs_review=True))
    assert c.value == 0.5 and c.band == "low"
    assert "needs human review" in c.explanation


def test_explanations_name_the_reasons():
    c = score(_s(dual_path="disagree", grounded=False, mean_extraction_confidence=0.72))
    assert "disagreed" in c.explanation
    assert "not fully grounded" in c.explanation
    assert "0.72" in c.explanation


def test_unavailable_confidence():
    c = unavailable("no_data_in_scope")
    assert (c.value, c.band) == (0.0, "low") and "no_data_in_scope" in c.explanation
