"""Embedders: deterministic fake for tests, Ollama adapter against a mocked HTTP API."""

import json
import math

import httpx
import pytest

from app.retrieval.embeddings import (
    DOC_PREFIX,
    QUERY_PREFIX,
    EmbeddingUnavailable,
    FakeEmbedder,
    OllamaEmbedder,
    get_embedder,
)

pytestmark = pytest.mark.unit


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_fake_is_deterministic_unit_length_and_768():
    e = FakeEmbedder(768)
    a, b = e.embed_documents(["Bob Smith absent 09/09", "Bob Smith absent 09/09"])
    assert a == b and len(a) == 768
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, rel_tol=1e-9)
    assert e.embed_query("") and len(e.embed_query("")) == 768


def test_fake_similar_texts_are_closer():
    e = FakeEmbedder(768)
    q = e.embed_query("Bob Smith late arrivals")
    near = e.embed_query("Bob Smith was late on three days")
    far = e.embed_query("quarterly revenue forecast")
    assert _cos(q, near) > _cos(q, far)


def test_default_embedder_in_tests_is_fake():
    assert get_embedder().name == "fake" and get_embedder().dim == 768


def _ollama(handler, dim=4, batch=2):
    return OllamaEmbedder(
        "http://ollama/v1",
        "nomic-embed-text",
        dim,
        batch=batch,
        transport=httpx.MockTransport(handler),
    )


def test_ollama_batches_prefixes_and_normalizes():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        data = [{"index": i, "embedding": [3.0, 4.0, 0.0, 0.0]} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": list(reversed(data))})

    e = _ollama(handler)
    vectors = e.embed_documents(["a", "b", "c"])
    assert [len(b["input"]) for b in seen] == [2, 1]
    assert all(t.startswith(DOC_PREFIX) for b in seen for t in b["input"])
    assert seen[0]["model"] == "nomic-embed-text"
    assert vectors[0] == pytest.approx([0.6, 0.8, 0.0, 0.0])
    e.embed_query("who was late")
    assert seen[-1]["input"] == [QUERY_PREFIX + "who was late"]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(404, json={}), "not found"),
        (httpx.Response(500, json={}), "failed with 500"),
        (httpx.Response(200, text="not json"), "malformed"),
        (httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]}), "4 dims"),
        (httpx.Response(200, json={"data": []}), "expected 1 vectors"),
    ],
)
def test_ollama_errors_are_embedding_unavailable(response, message):
    e = _ollama(lambda request: response)
    with pytest.raises(EmbeddingUnavailable, match=message):
        e.embed_documents(["x"])


def test_ollama_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(EmbeddingUnavailable, match="unreachable"):
        _ollama(handler).embed_query("x")
    assert _ollama(handler).ping() == {"reachable": False, "model_available": False}


def test_ollama_ping_accepts_tagged_model_names():
    e = _ollama(lambda r: httpx.Response(200, json={"data": [{"id": "nomic-embed-text:latest"}]}))
    assert e.ping() == {"reachable": True, "model_available": True}
