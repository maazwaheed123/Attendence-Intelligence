"""Answer confidence v1: a weighted score over verifiable signals, plus a band.

Signals (weights):
  sql_valid        0.20  the SQL that produced the answer passed validation
  dual_path        0.25  LLM SQL and template SQL agree (template-only counts as agreement:
                         it is the deterministic reference)
  grounding        0.25  the answer's numbers/dates/names are supported by the rows
  citations        0.15  source records are cited for the answer
  extraction       0.15  mean extraction confidence of the cited records (OCR < CSV)
Answers that need review are capped below the medium band.
"""

from dataclasses import dataclass

WEIGHTS = {
    "sql_valid": 0.20,
    "dual_path": 0.25,
    "grounding": 0.25,
    "citations": 0.15,
    "extraction": 0.15,
}
DUAL_PATH = {"agree": 1.0, "template_only": 1.0, "llm_only": 0.5, "disagree": 0.3}
REVIEW_CAP = 0.5


@dataclass
class Signals:
    sql_valid: bool
    dual_path: str  # agree | template_only | llm_only | disagree
    grounded: bool
    citation_coverage: float  # 0..1
    mean_extraction_confidence: float | None
    needs_review: bool = False


@dataclass
class Confidence:
    value: float
    band: str
    explanation: str


def band(value: float, high: float, low: float) -> str:
    return "high" if value >= high else "medium" if value >= low else "low"


def score(sig: Signals, *, high: float = 0.85, low: float = 0.60) -> Confidence:
    parts = {
        "sql_valid": 1.0 if sig.sql_valid else 0.0,
        "dual_path": DUAL_PATH.get(sig.dual_path, 0.0),
        "grounding": 1.0 if sig.grounded else 0.0,
        "citations": max(0.0, min(1.0, sig.citation_coverage)),
        "extraction": max(0.0, min(1.0, sig.mean_extraction_confidence or 0.0)),
    }
    value = sum(WEIGHTS[k] * v for k, v in parts.items())
    if sig.needs_review:
        value = min(value, REVIEW_CAP)
    value = round(value, 3)

    reasons = [
        "SQL validated" if sig.sql_valid else "SQL not validated",
        {
            "agree": "model SQL and template SQL agree",
            "template_only": "computed by the deterministic template",
            "llm_only": "model SQL without a deterministic cross-check",
            "disagree": "model SQL disagreed with the template (template result used)",
        }.get(sig.dual_path, sig.dual_path),
        "answer grounded in the results" if sig.grounded else "answer not fully grounded",
        "source records cited" if parts["citations"] >= 1 else "limited citations",
    ]
    if sig.mean_extraction_confidence is not None:
        reasons.append(f"mean extraction confidence {sig.mean_extraction_confidence:.2f}")
    if sig.needs_review:
        reasons.append("evidence needs human review")
    return Confidence(value, band(value, high, low), "; ".join(reasons) + ".")


def unavailable(reason: str) -> Confidence:
    return Confidence(0.0, "low", f"No answer was produced ({reason}).")
