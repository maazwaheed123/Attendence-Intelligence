"""LIVE: the demo questions against the running dev server (real Ollama + embeddings).

Excluded from the gate. Needs the dev stack up with the corpus ingested:
    docker compose run --rm -T api pytest -m live -s -q tests/live/test_eval_questions.py
All cases run once (module fixture, ~20-40 min on CPU); results are written to
docs/eval_results.json for scripts/eval_report.py, then each case is asserted.
"""

import httpx
import pytest

from scripts import eval_live

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def results():
    try:
        httpx.get(f"{eval_live.API_URL}/v1/health", timeout=5).raise_for_status()
    except httpx.HTTPError:
        pytest.skip(f"dev server not reachable at {eval_live.API_URL}")
    out = eval_live.run()
    eval_live.save(out)
    return out


@pytest.mark.parametrize("case_id", [c["id"] for c in eval_live.CASES])
def test_case(results, case_id):
    r = results[case_id]
    failed = [f"{c['check']} ({c['detail']})" for c in r["checks"] if not c["ok"]]
    assert r["passed"], f"{case_id}: {failed} | answer: {r['answer']!r} | note: {r['note']}"
