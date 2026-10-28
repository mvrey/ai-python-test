import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from service.ai_client import AIClient
from service.api import router
from service.config import get_settings
from service.logging_config import configure_logging
from service.notifier import Notifier
from service.pipeline import Pipeline
from service.provider_http import create_provider_client
from service.rate_limiter import TokenBucket
from service.store import InMemoryRequestStore

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    logger.info("starting with settings %s", settings.public_dict())
    http = create_provider_client(settings)
    store = InMemoryRequestStore()
    pipeline = Pipeline(
        settings,
        store,
        extractor=AIClient(http, settings.ai_timeout_sec),
        sender=Notifier(http, settings.notify_timeout_sec),
        limiter=TokenBucket(settings.notify_rate_per_sec, settings.notify_burst),
    )
    app.state.store = store
    app.state.pipeline = pipeline
    await pipeline.start()
    try:
        yield
    finally:
        await pipeline.stop()
        await http.aclose()


app = FastAPI(title="Notification Service (Technical Test)", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, _exc: Exception) -> JSONResponse:
    """Last line of defence: log the traceback and keep the response JSON."""
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )
