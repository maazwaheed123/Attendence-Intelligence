"""LIVE: real Ollama nomic-embed-text (excluded from the gate). Run: pytest -m live"""

import time

import pytest

from app.config import get_settings
from app.retrieval.embeddings import OllamaEmbedder

pytestmark = pytest.mark.live


@pytest.fixture
def embedder():
    s = get_settings()
    e = OllamaEmbedder(s.ollama_base_url, s.ollama_embed_model, s.embed_dim, batch=s.embed_batch)
    if not e.ping().get("model_available"):
        pytest.skip(f"{e.model} not available in Ollama")
    return e


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_real_embeddings_are_768_and_semantic(embedder):
    start = time.perf_counter()
    docs = embedder.embed_documents(
        [
            "2026-09-07 | E002 Bob Smith | Engineering | Present | 09:48-18:10",
            "Manager remarks: Bob arrived late on 07/09, 08/09 and 10/09.",
            "Quarterly revenue grew by 12 percent.",
        ]
    )
    ms = int((time.perf_counter() - start) * 1000)
    assert all(len(v) == 768 for v in docs)
    q = embedder.embed_query("What did the manager note about Bob's late arrivals?")
    assert _cos(q, docs[1]) > _cos(q, docs[2])
    print(f"\nnomic-embed-text: 3 docs in {ms} ms")
