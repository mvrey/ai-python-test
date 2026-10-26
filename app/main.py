import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from service.api import router
from service.config import get_settings
from service.pipeline import Pipeline
from service.store import InMemoryRequestStore

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.store = InMemoryRequestStore()
    app.state.pipeline = Pipeline(settings)
    await app.state.pipeline.start()
    try:
        yield
    finally:
        await app.state.pipeline.stop()


app = FastAPI(title="Notification Service (Technical Test)", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last line of defence: log the traceback and keep the response JSON."""
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )
