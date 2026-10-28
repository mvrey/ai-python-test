"""Runtime settings, read from environment variables with sensible defaults.

Every field can be overridden with an env var of the same name in upper case,
e.g. ``NOTIFY_RATE_PER_SEC=4``.
"""

import os
from dataclasses import dataclass, fields
from functools import lru_cache

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _parse(raw: str, kind: type):
    """Convert an env var string to ``kind``. ``bool`` is explicit because ``bool("false")`` is True."""
    if kind is bool:
        value = raw.strip().lower()
        if value in _TRUE:
            return True
        if value in _FALSE:
            return False
        raise ValueError(f"expected one of {sorted(_TRUE | _FALSE)}")
    return kind(raw)


@dataclass(frozen=True)
class Settings:
    # Provider (AI engine + notification service)
    provider_base_url: str = "http://localhost:3001"
    provider_api_key: str = "test-dev-2026"

    # AI extraction stage.
    # Timeouts are sized for a *degraded* provider, not its nominal latency (1.5-3 s). Under load
    # the mock's event loop stalls on synchronous InfluxDB writes and answers take 10 s+. A call
    # that times out still counts as an AI hit and is still processed by the provider, so giving
    # up early only adds load and burns the retry budget.
    ai_timeout_sec: float = 30.0
    max_ai_attempts: int = 2  # total AI calls per request, retries included
    ai_retry_backoff_sec: float = 0.5  # base delay before retrying after a transport/HTTP error
    extraction_workers: int = 100

    # Notification stage. The provider accepts 50 req / 10 s, so we pace below 5 req/s.
    # Nominal latency is 0.1-0.5 s; a read timeout on a slow-but-alive provider usually means the
    # notification *was* delivered, and retrying it sends a duplicate. Hence a generous timeout.
    notify_timeout_sec: float = 15.0
    notify_workers: int = 5
    notify_rate_per_sec: float = 4.5
    notify_burst: int = 1
    notify_cooldown_sec: float = 2.0
    max_notify_attempts: int = 6
    notify_retry_backoff_sec: float = 1.0
    max_backoff_sec: float = 30.0

    queue_maxsize: int = 10_000  # extraction intake; beyond this, /process marks requests failed
    log_level: str = "INFO"
    access_log: bool = False  # uvicorn per-request access log; noisy and costly under load

    def public_dict(self) -> dict:
        """Settings safe to log (secrets masked)."""
        return {f.name: "***" if f.name == "provider_api_key" else getattr(self, f.name) for f in fields(self)}

    def __post_init__(self) -> None:
        # Every numeric setting is a timeout, rate, count or size: zero or negative is always a mistake.
        for f in fields(self):
            value = getattr(self, f.name)
            if f.type in (int, float) and value <= 0:
                raise ValueError(f"{f.name.upper()} must be positive, got {value!r}")

    @classmethod
    def from_env(cls) -> "Settings":
        overrides = {}
        for f in fields(cls):
            raw = os.getenv(f.name.upper())
            if raw is None:
                continue
            try:
                overrides[f.name] = _parse(raw, f.type)
            except ValueError as exc:
                raise ValueError(f"Invalid value for {f.name.upper()}: {raw!r} ({exc})") from exc
        return cls(**overrides)


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
