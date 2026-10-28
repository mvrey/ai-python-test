"""Shared HTTP client and conventions for the provider (AI engine and notification service)."""

import httpx

from service.config import Settings

# Statuses worth retrying: the request may succeed later without any change on our side.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def create_provider_client(settings: Settings) -> httpx.AsyncClient:
    """One pooled client for the app's lifetime. Per-call timeouts are set by each caller."""
    pool_size = settings.extraction_workers + settings.notify_workers
    return httpx.AsyncClient(
        base_url=settings.provider_base_url,
        headers={"X-API-Key": settings.provider_api_key},
        limits=httpx.Limits(max_connections=pool_size, max_keepalive_connections=pool_size),
    )
