"""Answer phrasing: the model turns result rows into one business sentence.

The rows are passed as <evidence> data. The deterministic reference sentence is
included when a template exists, so the model only restyles verified facts; the
caller still grounds the output and falls back to the reference on any mismatch.
"""

import json

from pydantic import BaseModel

from app.generation.prompts import UNTRUSTED_DATA_RULES
from app.generation.providers.base import LLMResult, ProviderUnavailable

MAX_EVIDENCE_ROWS = 50

PHRASE_SYSTEM = f"""{UNTRUSTED_DATA_RULES}
You write the final answer of an attendance analytics service.
Write ONE or TWO short, plain business sentences answering the question using ONLY the
result rows in <evidence>. Copy numbers exactly as they appear. Write dates as DD/MM/YYYY.
Refer to employees as "Name (ID)". Do not add facts, advice or caveats that are not in the rows.
Reply with JSON only: {{"answer": "..."}}
"""


class Phrased(BaseModel):
    answer: str


def phrase(
    question: str,
    rows: list[dict],
    router,
    *,
    reference: str | None = None,
    audit: dict | None = None,
) -> tuple[LLMResult | None, list[dict]]:
    """(result, failed_attempts). result is None when no provider could answer."""
    evidence = json.dumps(rows[:MAX_EVIDENCE_ROWS], default=str)
    user = f"TASK: phrase\n<question>{question}</question>\n<evidence>{evidence}</evidence>"
    if reference:
        user += f"\nReference answer with verified facts: {reference}"
    messages = [{"role": "system", "content": PHRASE_SYSTEM}, {"role": "user", "content": user}]
    try:
        return (
            router.complete(
                messages,
                response_model=Phrased,
                purpose="phrase_answer",
                audit=audit,
                max_tokens=250,
            ),
            [],
        )
    except ProviderUnavailable as exc:
        return None, exc.attempts
