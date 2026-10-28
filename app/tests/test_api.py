import pytest
from fastapi.testclient import TestClient

from main import app
from service.api import get_monitor, get_store, get_submitter
from service.models import Stage
from service.store import InMemoryRequestStore

INPUT = "Manda un mail a feda@test.com diciendo hola"


class FakeSubmitter:
    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.submitted: list[str] = []

    def submit(self, request_id: str) -> bool:
        self.submitted.append(request_id)
        return self.accept


@pytest.fixture
def store():
    return InMemoryRequestStore()


@pytest.fixture
def submitter():
    return FakeSubmitter()


@pytest.fixture
def client(store, submitter):
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_submitter] = lambda: submitter
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def create(client) -> str:
    response = client.post("/v1/requests", json={"user_input": INPUT})
    assert response.status_code == 201
    return response.json()["id"]


# --- POST /v1/requests ---------------------------------------------------------


def test_create_returns_201_with_id(client, store):
    response = client.post("/v1/requests", json={"user_input": INPUT})

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id"}
    assert store.get(body["id"]).user_input == INPUT


@pytest.mark.parametrize(
    "payload",
    [{}, {"user_input": ""}, {"user_input": "   "}, {"user_input": 42}, {"other": INPUT}],
)
def test_create_rejects_invalid_body(client, payload):
    assert client.post("/v1/requests", json=payload).status_code == 422


def test_create_rejects_non_json_body(client):
    response = client.post("/v1/requests", content="not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 422


# --- POST /v1/requests/{id}/process ---------------------------------------------


def test_process_accepts_and_enqueues(client, store, submitter):
    request_id = create(client)

    response = client.post(f"/v1/requests/{request_id}/process")

    assert response.status_code == 202
    assert response.json() == {"id": request_id, "status": "queued"}
    assert submitter.submitted == [request_id]
    assert store.get(request_id).stage is Stage.EXTRACTION_PENDING


def test_process_is_idempotent(client, submitter):
    request_id = create(client)

    responses = [client.post(f"/v1/requests/{request_id}/process") for _ in range(3)]

    assert [r.status_code for r in responses] == [202, 202, 202]
    assert submitter.submitted == [request_id]


def test_process_reports_current_status_of_an_already_processed_request(client, store):
    request_id = create(client)
    store.transition(request_id, Stage.FAILED, error="no_recipient")

    response = client.post(f"/v1/requests/{request_id}/process")

    assert response.status_code == 202
    assert response.json()["status"] == "failed"


def test_process_when_queue_is_full_marks_failed_but_still_accepts(client, store, submitter):
    submitter.accept = False
    request_id = create(client)

    response = client.post(f"/v1/requests/{request_id}/process")

    assert response.status_code == 202
    assert response.json()["status"] == "failed"
    assert store.get(request_id).error == "overloaded"


def test_process_unknown_id_returns_404(client, submitter):
    assert client.post("/v1/requests/missing/process").status_code == 404
    assert submitter.submitted == []


# --- GET /v1/requests/{id} ------------------------------------------------------


def test_status_returns_id_and_public_status(client, store):
    request_id = create(client)
    store.transition(request_id, Stage.EXTRACTION_PENDING)
    store.transition(request_id, Stage.EXTRACTING)

    response = client.get(f"/v1/requests/{request_id}")

    assert response.status_code == 200
    assert response.json() == {"id": request_id, "status": "processing"}


def test_status_unknown_id_returns_404(client):
    response = client.get("/v1/requests/missing")
    assert response.status_code == 404
    assert response.json() == {"detail": "Request not found"}


# --- Cross-cutting ----------------------------------------------------------------


class FakeMonitor:
    def __init__(self, healthy=True):
        self.healthy = healthy

    def snapshot(self):
        return {"queues": {"extraction": 0, "notify": 3}, "ai_calls_per_request": 1.1}


def health(client, monitor):
    app.dependency_overrides[get_monitor] = lambda: monitor
    return client.get("/health")


def test_health_reports_request_counts_and_pipeline_state(client, store):
    create(client)
    second = create(client)
    store.transition(second, Stage.EXTRACTION_PENDING)
    store.transition(second, Stage.EXTRACTING)

    response = health(client, FakeMonitor())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["requests"]["total"] == 2
    assert body["requests"]["by_status"] == {"queued": 1, "processing": 1, "sent": 0, "failed": 0}
    assert body["requests"]["by_stage"]["extracting"] == 1
    assert body["queues"] == {"extraction": 0, "notify": 3}
    assert body["ai_calls_per_request"] == 1.1
    assert "healthy" not in body


def test_health_is_503_when_degraded(client):
    response = health(client, FakeMonitor(healthy=False))
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_health_with_the_real_pipeline():
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["workers"]["extraction"]["alive"] == body["workers"]["extraction"]["expected"]


def test_unexpected_errors_return_json_500():
    class BrokenStore(InMemoryRequestStore):
        def get(self, _request_id):
            raise RuntimeError("boom")

    app.dependency_overrides[get_store] = lambda: BrokenStore()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/v1/requests/anything")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}


def test_real_wiring_without_overrides():
    """Through the lifespan-built store and pipeline. Workers start, but no provider is reachable."""
    with TestClient(app) as client:
        request_id = create(client)
        assert client.post(f"/v1/requests/{request_id}/process").status_code == 202
        assert client.get(f"/v1/requests/{request_id}").json()["status"] in ("queued", "processing", "failed")
