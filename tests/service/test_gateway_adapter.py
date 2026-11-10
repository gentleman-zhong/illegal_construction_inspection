import asyncio
import httpx
import pytest
from fastapi.testclient import TestClient

from scripts.gateway_adapter.app import create_app, FAILURE


class FakeSDK:
    def __init__(self):
        self.active = 0
        self.loaded = False
        self.stopped = False

    def track_request(self, delta):
        self.active += delta

    def mark_model_loaded(self, loaded):
        self.loaded = loaded

    async def bootstrap(self):
        await asyncio.Event().wait()

    async def shutdown(self):
        self.stopped = True

    def health_payload(self):
        return self.loaded, {"status": "healthy" if self.loaded else "unavailable"}

    def info_payload(self):
        return {}


@pytest.mark.parametrize("state,code", [("RUNNING", 0), ("SUCCESS", 0), ("FAILED", 500)])
def test_query_preserves_business_result(state, code):
    sdk = FakeSDK()
    def upstream(request):
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        assert request.method == "GET"
        assert request.url.path == "/two-violation/tasks/T1"
        return httpx.Response(200, json={"code": code, "data": {
            "taskId": "T1", "status": state, "progress": "100",
            "3dtilesUrl": None, "instanceJsonUrl": "https://oss/result.json",
            "errorMessage": "private traceback"}})
    with TestClient(create_app(sdk=sdk, transport=httpx.MockTransport(upstream))) as client:
        response = client.post("/gateway/tasks/query", json={"taskId": "T1"})
        assert response.status_code == 200
        assert response.json()["status"] == state
        assert response.json()["3dtilesUrl"] is None
        assert response.json()["errorMessage"] == (FAILURE if state == "FAILED" else None)
        assert sdk.active == 0
    assert sdk.stopped


@pytest.mark.parametrize("mode,expected", [("ok", 200), ("duplicate", 409), ("timeout", 502), ("malformed", 502)])
def test_submit_forward_once(mode, expected):
    import json
    sdk = FakeSDK()
    calls = []
    body = {"taskId": "T1", "baseModelPath": "a", "compareModelPath": "b",
            "areaCoordinates": [], "radius": 50, "detectionType": "twoIllegal"}
    def upstream(request):
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        calls.append(request)
        assert json.loads(request.content) == body
        assert request.url.path == "/two-violation/compare"
        if mode == "timeout":
            raise httpx.ReadTimeout("private URL", request=request)
        if mode == "malformed":
            return httpx.Response(200, text="private traceback")
        return httpx.Response(200, json={"code": 500 if mode == "duplicate" else 0,
            "data": {"taskId": "T1", "status": "FAILED" if mode == "duplicate" else "PENDING",
                     "errorMessage": "private details"}})
    with TestClient(create_app(sdk=sdk, transport=httpx.MockTransport(upstream))) as client:
        response = client.post("/gateway/tasks/submit", json=body)
        assert response.status_code == expected
        assert "private" not in response.text
        assert len(calls) == 1
        assert sdk.active == 0


def test_invalid_task_id_never_forwarded():
    def upstream(request):
        assert request.url.path == "/healthz"
        return httpx.Response(503)
    with TestClient(create_app(sdk=FakeSDK(), transport=httpx.MockTransport(upstream))) as client:
        assert client.post("/gateway/tasks/query", json={"taskId": "../secret"}).status_code == 422
        assert client.get("/healthz").status_code == 200


def test_real_sdk_registers_after_health_and_deregisters():
    from algo_sdk import AlgoServiceSDK, EndpointSpec, SDKConfig
    calls = []
    def controller(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/register"):
            return httpx.Response(200, json={"code": 0, "data": {
                "instance_id": "adapter-test", "heartbeat_interval_sec": 10}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    async def scenario():
        config = SDKConfig(algo_name="adapter_test", instance_token="test-token",
                           callback_host="adapter", callback_port=8000,
                           endpoints=[EndpointSpec(name="query_task", path="/gateway/tasks/query")])
        sdk = AlgoServiceSDK(config, client=httpx.AsyncClient(
            transport=httpx.MockTransport(controller)))
        app = create_app(sdk=sdk, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok"})))
        assert not sdk.model_loaded
        async with app.router.lifespan_context(app):
            for _ in range(100):
                if sdk.instance_id:
                    break
                await asyncio.sleep(0.01)
            assert sdk.model_loaded
            assert sdk.instance_id == "adapter-test"
        assert any("/deregister/adapter-test" in path for path in calls)
    asyncio.run(scenario())


def test_algorithm_unavailable_does_not_block_startup():
    sdk = FakeSDK()
    app = create_app(sdk=sdk, transport=httpx.MockTransport(
        lambda request: httpx.Response(503)))
    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.01)
            assert not sdk.loaded
        assert sdk.stopped
    asyncio.run(scenario())


@pytest.mark.parametrize("method,path,status,payload", [
    ("POST", "/two-violation/compare", 200,
     {"code": 0, "message": "success", "data": {"taskId": "T1", "status": "PENDING"}}),
    ("POST", "/two-violation/compare", 200,
     {"code": 500, "message": "failed", "data": {"taskId": "T1", "status": "FAILED"}}),
    ("GET", "/two-violation/tasks/T1", 200,
     {"code": 500, "message": "failed", "data": {"taskId": "T1", "status": "FAILED"}}),
    ("GET", "/two-violation/tasks/T1", 200,
     {"code": 0, "message": "success", "data": {"taskId": "T1", "status": "SUCCESS",
      "3dtilesUrl": None, "instanceJsonUrl": "https://oss/result.json"}}),
    ("POST", "/two-violation/compare", 422, {"detail": [{"msg": "Field required"}]}),
])
def test_original_api_compatibility(method, path, status, payload):
    sdk = FakeSDK()
    calls = []
    body = b'{"taskId":"T1","detectionType":"twoIllegal","unknown":1}' if method == "POST" else b""
    def upstream(request):
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        calls.append(request)
        assert request.method == method
        assert request.url.path == path
        assert request.content == body
        assert request.url.query == b"test=1"
        return httpx.Response(status, json=payload)
    with TestClient(create_app(sdk=sdk, transport=httpx.MockTransport(upstream))) as client:
        response = client.request(method, path + "?test=1", content=body,
                                  headers={"Content-Type": "application/json"})
        assert response.status_code == status
        assert response.json() == payload
        assert len(calls) == 1
        assert sdk.active == 0


def test_compatible_submit_timeout_is_not_retried():
    calls = []
    sdk = FakeSDK()
    def upstream(request):
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        calls.append(request)
        raise httpx.ReadTimeout("private URL", request=request)
    with TestClient(create_app(sdk=sdk, transport=httpx.MockTransport(upstream))) as client:
        response = client.post("/two-violation/compare", json={"taskId": "T1"})
        assert response.status_code == 502
        assert response.json()["code"] == 500
        assert response.json()["data"]["errorMessage"] == FAILURE
        assert "private" not in response.text
        assert len(calls) == 1
        assert sdk.active == 0


def test_registered_native_get_contract(monkeypatch):
    monkeypatch.setenv("ALGO_NAME", "adapter_test")
    monkeypatch.setenv("INSTANCE_TOKEN", "test-token")
    monkeypatch.setenv("CALLBACK_HOST", "127.0.0.1")
    monkeypatch.setenv("CALLBACK_PORT", "6602")
    monkeypatch.setenv("ALGO_VERSION", "v1.1.0")
    app = create_app()
    import asyncio
    info_route = next(route for route in app.routes if route.path == "/info")
    info = asyncio.run(info_route.endpoint())
    endpoints = {item["name"]: item for item in info["endpoints"]}
    query = endpoints["query_task"]
    assert query["method"] == "get"
    assert query["path"] == "/two-violation/tasks/{taskId}"
    assert query["input_schema"]["required"] == ["taskId"]
    assert query["input_schema"]["properties"]["taskId"]["type"] == "string"
    assert endpoints["submit_task"]["path"] == "/two-violation/compare"
    assert endpoints["submit_task"].get("method", "post") == "post"
    assert query["output_schema"]["required"] == ["code", "message", "data"]
