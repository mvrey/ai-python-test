"""Client for the AI extraction endpoint (OpenAI-style chat completion).

Makes exactly **one** HTTP call per ``extract``; it never retries on its own. The provider
counts every call (failed ones included) toward the AI-calls-per-request ratio, so the retry
budget lives in a single place: the pipeline. Errors say whether retrying makes sense.
"""

import logging

import httpx

from service.prompts import ChatMessage
from service.provider_http import RETRYABLE_STATUS

logger = logging.getLogger(__name__)

EXTRACT_PATH = "/v1/ai/extract"


class AIRequestError(Exception):
    def __init__(self, reason: str, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class AIClient:
    def __init__(self, http: httpx.AsyncClient, timeout_sec: float) -> None:
        self._http = http
        self._timeout = timeout_sec

    async def extract(self, messages: list[ChatMessage]) -> str:
        """Return the assistant's raw text answer. Raises ``AIRequestError``."""
        try:
            response = await self._http.post(EXTRACT_PATH, json={"messages": messages}, timeout=self._timeout)
        except httpx.TimeoutException:
            raise AIRequestError("timeout", retryable=True) from None
        except httpx.TransportError as exc:
            raise AIRequestError(f"transport_error:{type(exc).__name__}", retryable=True) from None

        if response.status_code != 200:
            status = response.status_code
            raise AIRequestError(f"http_{status}", retryable=status in RETRYABLE_STATUS)

        try:
            content = _content_of(response.json())
        except (ValueError, LookupError, TypeError, AttributeError):
            raise AIRequestError("invalid_envelope", retryable=True) from None
        logger.debug("ai answer: %.300r", content)
        return content


def _content_of(body: dict) -> str:
    """``choices[0].message.content``; a null content is an empty answer."""
    content = body["choices"][0]["message"].get("content")
    if content is None:
        return ""
    if not isinstance(content, str):
        raise TypeError("content is not text")
    return content
