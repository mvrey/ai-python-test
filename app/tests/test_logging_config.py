import logging

import pytest

from service.config import Settings
from service.logging_config import configure_logging


@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    saved = root.level, list(root.handlers)
    yield
    root.setLevel(saved[0])
    root.handlers[:] = saved[1]


def test_level_and_single_handler():
    configure_logging(Settings(log_level="debug"))

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 1
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate


@pytest.mark.parametrize("access_log, enabled", [(False, False), (True, True)])
def test_access_log_is_opt_in(access_log, enabled):
    configure_logging(Settings(access_log=access_log))
    assert logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO) is enabled


def test_existing_loggers_keep_working():
    logger = logging.getLogger("service.pipeline")
    configure_logging(Settings())
    assert not logger.disabled


def test_invalid_level_is_rejected():
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        configure_logging(Settings(log_level="loud"))
