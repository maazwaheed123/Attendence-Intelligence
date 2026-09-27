"""The HTTP adapter against a fake Ollama (httpx.MockTransport)."""

import base64
import json

import httpx
import pytest

from app.generation.providers.base import (
    ProviderBadResponse,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderServerError,
    ProviderTimeout,
)
from app.generation.providers.openai_compat import OpenAICompatibleProvider, image_message

pytestmark = pytest.mark.unit
MSG = [{"role": "user", "content": "hi"}]


def provider(handler):
    return OpenAICompatibleProvider(
        "ollama-primary",
        "http://ollama:11434/v1",
        "qwen2.5:7b-instruct",
        transport=httpx.MockTransport(handler),
    )


def ok_handler(seen):
    def h(request: httpx.Request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"total_tokens": 7},
            },
        )

    return h


def test_request_payload_and_response():
    seen = []
    r = provider(ok_handler(seen)).chat(MSG, json_mode=True, timeout=5, max_tokens=50)
    assert (
        r.text == '{"ok": true}'
        and r.provider == "ollama-primary"
        and r.usage == {"total_tokens": 7}
    )
    body = seen[0]
    assert body["model"] == "qwen2.5:7b-instruct" and body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"} and body["stream"] is False


def test_no_json_mode_omits_response_format():
    seen = []
    provider(ok_handler(seen)).chat(MSG, json_mode=False, timeout=5, max_tokens=50)
    assert "response_format" not in seen[0]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, ProviderRateLimited),
        (404, ProviderNotConfigured),
        (500, ProviderServerError),
        (503, ProviderServerError),
        (400, ProviderBadResponse),
    ],
)
def test_http_errors_mapped(status, error):
    with pytest.raises(error):
        provider(lambda r: httpx.Response(status, json={})).chat(
            MSG, json_mode=False, timeout=5, max_tokens=5
        )


def test_timeout_mapped():
    def h(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderTimeout):
        provider(h).chat(MSG, json_mode=False, timeout=1, max_tokens=5)


def test_connection_refused_mapped():
    def h(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderNotConfigured):
        provider(h).chat(MSG, json_mode=False, timeout=1, max_tokens=5)


def test_malformed_payload():
    with pytest.raises(ProviderBadResponse):
        provider(lambda r: httpx.Response(200, json={"unexpected": 1})).chat(
            MSG, json_mode=False, timeout=1, max_tokens=5
        )


def test_ping_reports_model_availability():
    def h(request):
        return httpx.Response(
            200, json={"data": [{"id": "qwen2.5:7b-instruct"}, {"id": "gemma3:1b"}]}
        )

    assert provider(h).ping() == {"reachable": True, "model_available": True}
    missing = OpenAICompatibleProvider(
        "v", "http://x/v1", "qwen2.5vl:3b", transport=httpx.MockTransport(h)
    )
    assert missing.ping() == {"reachable": True, "model_available": False}


def test_image_message_format():
    msg = image_message("read this", b"\x89PNGdata")
    parts = msg[0]["content"]
    assert parts[0] == {"type": "text", "text": "read this"}
    url = parts[1]["image_url"]["url"]
    assert (
        url.startswith("data:image/png;base64,")
        and base64.b64decode(url.split(",")[1]) == b"\x89PNGdata"
    )
