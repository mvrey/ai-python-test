"""Client for the provider's notification endpoint.

Like ``AIClient``, it makes exactly one HTTP call per ``send`` and leaves retry decisions to the
pipeline. Sending is **not idempotent**, so errors also say whether the notification may have
been delivered anyway:

- the request never reached the provider (connect error/timeout, pool timeout): safe to retry;
- the request was sent but no answer came back (read timeout, dropped connection): the
  provider may have delivered it. Retrying gives at-least-once delivery; ``trace_id`` (the
  request id, identical on every attempt) is what a real provider would deduplicate on.
- a 200 is final: a malformed body never triggers a resend.
"""

import email.utils
import logging
import time

import httpx

from service.models import NotificationPayload
from service.provider_http import RETRYABLE_STATUS

logger = logging.getLogger(__name__)

NOTIFY_PATH = "/v1/notify"
PRIORITY = "normal"
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class NotifyError(Exception):
    def __init__(
        self,
        reason: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        maybe_delivered: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason  # diagnostic label for logs and metrics; don't branch on it
        self.retryable = retryable
        self.status_code = status_code  # None when no HTTP response was received
        self.maybe_delivered = maybe_delivered
        self.retry_after = retry_after

    @property
    def rate_limited(self) -> bool:
        return self.status_code == 429


class Notifier:
    def __init__(self, http: httpx.AsyncClient, timeout_sec: float) -> None:
        self._http = http
        self._timeout = timeout_sec

    async def send(self, payload: NotificationPayload, trace_id: str) -> str | None:
        """Send one notification. Returns the provider id (None if the 200 body lacked one)."""
        try:
            response = await self._http.post(
                NOTIFY_PATH,
                json=payload.model_dump(),
                params={"priority": PRIORITY, "trace_id": trace_id},
                timeout=self._timeout,
            )
        except _NOT_SENT_ERRORS as exc:
            raise NotifyError(f"not_sent:{type(exc).__name__}", retryable=True) from None
        except httpx.TimeoutException as exc:
            raise NotifyError(f"timeout:{type(exc).__name__}", retryable=True, maybe_delivered=True) from None
        except httpx.TransportError as exc:
            raise NotifyError(f"transport_error:{type(exc).__name__}", retryable=True, maybe_delivered=True) from None

        status = response.status_code
        if status == 200:
            return _provider_id(response)
        raise NotifyError(
            f"http_{status}",
            retryable=status in RETRYABLE_STATUS,
            status_code=status,
            retry_after=_retry_after(response) if status == 429 else None,
        )


def _provider_id(response: httpx.Response) -> str | None:
    try:
        provider_id = response.json().get("provider_id")
    except (ValueError, AttributeError):
        provider_id = None
    if not isinstance(provider_id, str):
        logger.warning("notification accepted but the response had no provider_id: %.200r", response.text)
        return None
    return provider_id


def _retry_after(response: httpx.Response) -> float | None:
    """Parse ``Retry-After`` (seconds or HTTP date). None if absent or unparseable."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())
