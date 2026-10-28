import json

import httpx
import pytest
import respx

from service.ai_client import EXTRACT_PATH, AIClient, AIRequestError
from service.config import Settings
from service.provider_http import create_provider_client

BASE_URL = "http://provider.test"
MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def completion(content) -> dict:
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}


@pytest.fixture
async def http():
    client = create_provider_client(Settings(provider_base_url=BASE_URL, provider_api_key="secret"))
    yield client
    await client.aclose()


@pytest.fixture
def ai(http):
    return AIClient(http, timeout_sec=1.0)


@pytest.fixture
def provider():
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


async def test_returns_assistant_content_and_sends_contract(ai, provider):
    route = provider.post(EXTRACT_PATH).respond(200, json=completion('{"to": "a@b.com"}'))

    assert await ai.extract(MESSAGES) == '{"to": "a@b.com"}'

    assert route.call_count == 1
    request = route.calls.last.request
    assert request.headers["X-API-Key"] == "secret"
    assert json.loads(request.content) == {"messages": MESSAGES}


@pytest.mark.parametrize("content, expected", [(None, ""), ([{"type": "text", "text": "a"}, {"text": "b"}], "ab")])
async def test_tolerates_null_and_multipart_content(ai, provider, content, expected):
    provider.post(EXTRACT_PATH).respond(200, json=completion(content))
    assert await ai.extract(MESSAGES) == expected


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_transient_http_errors_are_retryable(ai, provider, status):
    provider.post(EXTRACT_PATH).respond(status, json={"error": "x"})
    with pytest.raises(AIRequestError) as exc_info:
        await ai.extract(MESSAGES)
    assert exc_info.value.retryable
    assert exc_info.value.reason == f"http_{status}"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_client_errors_are_not_retryable(ai, provider, status):
    provider.post(EXTRACT_PATH).respond(status, json={"detail": "x"})
    with pytest.raises(AIRequestError) as exc_info:
        await ai.extract(MESSAGES)
    assert not exc_info.value.retryable


@pytest.mark.parametrize(
    "error, reason",
    [(httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("down"), "transport_error:ConnectError")],
)
async def test_network_errors_are_retryable(ai, provider, error, reason):
    provider.post(EXTRACT_PATH).mock(side_effect=error)
    with pytest.raises(AIRequestError) as exc_info:
        await ai.extract(MESSAGES)
    assert exc_info.value.retryable
    assert exc_info.value.reason == reason


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": "text"}]}),
        httpx.Response(200, json=completion(42)),
        httpx.Response(200, json=[1, 2]),
    ],
    ids=["not-json", "no-choices", "empty-choices", "message-not-object", "content-not-text", "body-not-object"],
)
async def test_malformed_envelopes_are_retryable(ai, provider, response):
    provider.post(EXTRACT_PATH).mock(return_value=response)
    with pytest.raises(AIRequestError) as exc_info:
        await ai.extract(MESSAGES)
    assert exc_info.value.reason == "invalid_envelope"
    assert exc_info.value.retryable


async def test_makes_exactly_one_call_even_on_failure(ai, provider):
    route = provider.post(EXTRACT_PATH).respond(500)
    with pytest.raises(AIRequestError):
        await ai.extract(MESSAGES)
    assert route.call_count == 1
