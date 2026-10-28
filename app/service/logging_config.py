"""Logging setup: one plain-text format for the app and uvicorn, level from settings.

Pipeline log lines carry ``id=<request id>``, so one request can be followed end to end with
``grep id=<request id>``.
"""

import logging
import logging.config

from service.config import Settings

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(settings: Settings) -> None:
    level = settings.log_level.upper()
    if level not in logging.getLevelNamesMapping():
        raise ValueError(f"Invalid LOG_LEVEL: {settings.log_level!r}")

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": {"format": FORMAT}},
        "handlers": {"stdout": {"class": "logging.StreamHandler", "formatter": "default", "stream": "ext://sys.stdout"}},
        "root": {"level": level, "handlers": ["stdout"]},
        "loggers": {
            # uvicorn installs its own handlers; route everything through ours instead.
            "uvicorn": {"level": level, "handlers": [], "propagate": True},
            "uvicorn.error": {"level": level, "handlers": [], "propagate": True},
            # One line per HTTP request is too much under load and is written synchronously on
            # the event loop; the pipeline already logs every state change.
            "uvicorn.access": {
                "level": "INFO" if settings.access_log else "WARNING",
                "handlers": [],
                "propagate": True,
            },
            "httpx": {"level": "WARNING"},
        },
    })
