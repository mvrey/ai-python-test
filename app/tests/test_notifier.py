import email.utils
import json
import time

import httpx
import pytest
from conftest import API_KEY

from service.models import NotificationPayload
from service.notifier import NOTIFY_PATH, Notifier, NotifyError

PAYLOAD = NotificationPayload(to="699888777", message="cita", type="sms")


@pytest.fixture
def notifier(provider_http):
    return Notifier(provider_http, timeout_sec=1.0)


async def send_expecting_error(notifier) -> NotifyError:
    with pytest.raises(NotifyError) as exc_info:
        await notifier.send(PAYLOAD, trace_id="req-1")
    return exc_info.value


async def test_success_returns_provider_id_and_sends_contract(notifier, provider):
    route = provider.post(NOTIFY_PATH).respond(200, json={"status": "delivered", "provider_id": "p-1234"})

    assert await notifier.send(PAYLOAD, trace_id="req-1") == "p-1234"

    assert route.call_count == 1
    request = route.calls.last.request
    assert request.headers["X-API-Key"] == API_KEY
    assert json.loads(request.content) == {"to": "699888777", "message": "cita", "type": "sms"}
    assert dict(request.url.params) == {"priority": "normal", "trace_id": "req-1"}


@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, json={"status": "delivered"}), httpx.Response(200, text="ok"), httpx.Response(200, json=[1])],
    ids=["no-provider-id", "not-json", "not-object"],
)
async def test_a_200_is_final_even_with_a_malformed_body(notifier, provider, response):
    provider.post(NOTIFY_PATH).mock(return_value=response)
    assert await notifier.send(PAYLOAD, trace_id="req-1") is None


async def test_429_is_retryable_and_reports_retry_after(notifier, provider):
    provider.post(NOTIFY_PATH).respond(429, json={"detail": "Rate limit exceeded"}, headers={"Retry-After": "3"})

    error = await send_expecting_error(notifier)

    assert (error.status_code, error.rate_limited, error.retryable, error.retry_after, error.maybe_delivered) == (
        429, True, True, 3.0, False,
    )


@pytest.mark.parametrize("status", [500, 401])
async def test_only_429_is_rate_limited(notifier, provider, status):
    provider.post(NOTIFY_PATH).respond(status)
    error = await send_expecting_error(notifier)
    assert error.status_code == status and not error.rate_limited


async def test_errors_without_a_response_have_no_status(notifier, provider):
    provider.post(NOTIFY_PATH).mock(side_effect=httpx.ConnectError("refused"))
    error = await send_expecting_error(notifier)
    assert error.status_code is None and not error.rate_limited


async def test_429_without_retry_after(notifier, provider):
    provider.post(NOTIFY_PATH).respond(429)
    assert (await send_expecting_error(notifier)).retry_after is None


async def test_retry_after_as_http_date(notifier, provider):
    when = email.utils.formatdate(time.time() + 30, usegmt=True)
    provider.post(NOTIFY_PATH).respond(429, headers={"Retry-After": when})
    assert (await send_expecting_error(notifier)).retry_after == pytest.approx(30, abs=2)


async def test_garbage_retry_after_is_ignored(notifier, provider):
    provider.post(NOTIFY_PATH).respond(429, headers={"Retry-After": "soon"})
    assert (await send_expecting_error(notifier)).retry_after is None


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
async def test_server_errors_are_retryable(notifier, provider, status):
    provider.post(NOTIFY_PATH).respond(status)
    error = await send_expecting_error(notifier)
    assert error.retryable and error.reason == f"http_{status}"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_client_errors_are_permanent(notifier, provider, status):
    provider.post(NOTIFY_PATH).respond(status)
    assert not (await send_expecting_error(notifier)).retryable


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.PoolTimeout("busy")])
async def test_not_sent_errors_are_safe_to_retry(notifier, provider, error):
    provider.post(NOTIFY_PATH).mock(side_effect=error)
    result = await send_expecting_error(notifier)
    assert result.retryable and not result.maybe_delivered


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("dropped")])
async def test_errors_after_sending_may_have_delivered(notifier, provider, error):
    provider.post(NOTIFY_PATH).mock(side_effect=error)
    result = await send_expecting_error(notifier)
    assert result.retryable and result.maybe_delivered


async def test_makes_exactly_one_call_even_on_failure(notifier, provider):
    route = provider.post(NOTIFY_PATH).respond(503)
    await send_expecting_error(notifier)
    assert route.call_count == 1
