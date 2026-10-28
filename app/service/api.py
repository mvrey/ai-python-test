"""HTTP endpoints.

Handlers are ``async def`` on purpose: they run on the event loop, the same thread as the
pipeline workers, which is what makes the store's check-then-act sequences atomic (see
``service.store``). A plain ``def`` handler would run in a thread pool and break that.
"""

import logging
from collections import Counter
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from service.models import CreateRequest, CreateResponse, RequestRecord, Stage, StatusResponse
from service.store import RequestStore

logger = logging.getLogger(__name__)

router = APIRouter()


class JobSubmitter(Protocol):
    def submit(self, request_id: str) -> bool: ...


class PipelineMonitor(Protocol):
    @property
    def healthy(self) -> bool: ...

    def snapshot(self) -> dict: ...


def get_store(request: Request) -> RequestStore:
    return request.app.state.store


def get_submitter(request: Request) -> JobSubmitter:
    return request.app.state.pipeline


def get_monitor(request: Request) -> PipelineMonitor:
    return request.app.state.pipeline


StoreDep = Annotated[RequestStore, Depends(get_store)]
SubmitterDep = Annotated[JobSubmitter, Depends(get_submitter)]
MonitorDep = Annotated[PipelineMonitor, Depends(get_monitor)]


def _require(store: RequestStore, request_id: str) -> RequestRecord:
    record = store.get(request_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return record


def _status_response(record: RequestRecord) -> StatusResponse:
    return StatusResponse(id=record.id, status=record.status)


@router.post("/v1/requests", status_code=status.HTTP_201_CREATED, response_model=CreateResponse)
async def create_request(body: CreateRequest, store: StoreDep) -> CreateResponse:
    record = store.create(body.user_input)
    logger.debug("request created id=%s", record.id)
    return CreateResponse(id=record.id)


@router.post(
    "/v1/requests/{request_id}/process",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StatusResponse,
)
async def process_request(request_id: str, store: StoreDep, submitter: SubmitterDep) -> StatusResponse:
    """Accept the request for asynchronous processing and return immediately.

    Idempotent: only a freshly created request is enqueued; any later call reports the
    current status without triggering a second AI extraction.
    """
    record = _require(store, request_id)
    if record.stage is not Stage.CREATED:
        return _status_response(record)

    record = store.transition(request_id, Stage.EXTRACTION_PENDING)
    if not submitter.submit(request_id):
        # Still 202: the request was accepted and its outcome is visible through the status endpoint.
        logger.warning("extraction queue full, rejecting id=%s", request_id)
        record = store.transition(request_id, Stage.FAILED, error="overloaded")
    return _status_response(record)


@router.get("/v1/requests/{request_id}", response_model=StatusResponse)
async def get_request_status(request_id: str, store: StoreDep) -> StatusResponse:
    return _status_response(_require(store, request_id))


@router.get("/health")
async def health(response: Response, store: StoreDep, monitor: MonitorDep) -> dict:
    """Liveness plus operational state. 503 if any pipeline worker has died."""
    healthy = monitor.healthy
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    by_stage = store.count_by_stage()
    by_status: Counter[str] = Counter()
    for stage, count in by_stage.items():
        by_status[stage.status.value] += count
    return {
        "status": "ok" if healthy else "degraded",
        "requests": {
            "total": sum(by_stage.values()),
            "by_status": dict(by_status),
            "by_stage": {stage.value: count for stage, count in by_stage.items()},
        },
        **monitor.snapshot(),
    }
