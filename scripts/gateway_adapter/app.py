"""Independent SDK bridge. Does not import or execute the algorithm service."""
import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from algo_sdk import AlgoServiceSDK, EndpointSpec, SDKConfig
from algo_sdk.standard_api import mount_fastapi

log = logging.getLogger(__name__)
FAILURE = "模型对比失败，请联系管理员。"
TASK_PATTERN = r"^[A-Za-z0-9_\-.]{1,128}$"
QUERY_SCHEMA = {
    "type": "object", "required": ["taskId"],
    "properties": {"taskId": {"type": "string", "pattern": TASK_PATTERN}},
    "additionalProperties": False,
}
SUBMIT_SCHEMA = {
    "type": "object", "required": ["taskId", "baseModelPath", "compareModelPath"],
    "properties": {
        **QUERY_SCHEMA["properties"],
        "baseModelPath": {"type": "string", "minLength": 1},
        "compareModelPath": {"type": "string", "minLength": 1},
        "xmlFile": {"type": ["string", "null"]},
        "positionMode": {"type": ["string", "null"]},
        "areaCoordinates": {"type": ["array", "null"], "items": {"type": "object"}},
        "radius": {"type": ["number", "null"]},
        "detectionType": {"enum": ["twoIllegal", "constructionProgress", "landSlide", None]},
    },
    "additionalProperties": True,
}
OUTPUT_SCHEMA = {
    "type": "object", "required": ["taskId", "status", "errorMessage"],
    "properties": {
        "taskId": {"type": "string"},
        "status": {"enum": ["PENDING", "RUNNING", "SUCCESS", "FAILED"]},
        "errorMessage": {"type": ["string", "null"]},
    }, "additionalProperties": True,
}
ENVELOPE_SCHEMA = {
    "type": "object", "required": ["code", "message", "data"],
    "properties": {"code": {"type": "integer"}, "message": {"type": "string"},
                   "data": OUTPUT_SCHEMA},
    "additionalProperties": True,
}


def create_app(*, sdk=None, transport=None):
    base = os.environ.get("ALGORITHM_SERVICE_URL", "http://127.0.0.1:6601").rstrip("/")
    url = urlsplit(base)
    if url.scheme not in ("http", "https") or not url.hostname or url.query or url.fragment or url.username:
        raise ValueError("ALGORITHM_SERVICE_URL must be an HTTP(S) service base URL")
    timeout = float(os.environ.get("ADAPTER_REQUEST_TIMEOUT_S", "45"))
    if not 0 < timeout <= 300:
        raise ValueError("ADAPTER_REQUEST_TIMEOUT_S must be in (0, 300]")
    endpoints = [
        EndpointSpec(name=name, path=path, input_schema=schema, method=method,
                     output_schema=ENVELOPE_SCHEMA, timeout_ms=int((timeout + 10) * 1000),
                     first_byte_timeout_ms=int((timeout + 10) * 1000),
                     idle_timeout_ms=int((timeout + 10) * 1000),
                     max_duration_ms=int((timeout + 10) * 1000))
        for name, path, schema, method in (
            ("submit_task", "/two-violation/compare", SUBMIT_SCHEMA, "post"),
            ("query_task", "/two-violation/tasks/{taskId}", QUERY_SCHEMA, "get"),
        )
    ]
    if sdk is None:
        config = SDKConfig.from_env(endpoints=endpoints, description="两违巡查异步任务适配服务")
        config.unreachable_exit_enabled = False
        config.validate()
        sdk = AlgoServiceSDK(config)

    async def monitor(client):
        while True:
            healthy = False
            try:
                response = await client.get(base + "/healthz", timeout=3)
                healthy = response.status_code == 200 and response.json().get("status") == "ok"
            except (httpx.HTTPError, ValueError, AttributeError):
                log.warning("algorithm health check failed", exc_info=True)
            sdk.mark_model_loaded(healthy)
            await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(app):
        # No HTTP retries: an interrupted submission may already be accepted.
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                                     timeout=httpx.Timeout(timeout, connect=5)) as client:
            app.state.client = client
            health_task = asyncio.create_task(monitor(client))
            bootstrap_task = asyncio.create_task(sdk.bootstrap())
            def report_failure(task):
                if not task.cancelled() and task.exception() is not None:
                    log.error("SDK background task failed", exc_info=task.exception())
            bootstrap_task.add_done_callback(report_failure)
            try:
                yield
            finally:
                for task in (health_task, bootstrap_task):
                    task.cancel()
                await asyncio.gather(health_task, bootstrap_task, return_exceptions=True)
                await sdk.shutdown()

    app = FastAPI(title="Illegal inspection gateway adapter", lifespan=lifespan)
    mount_fastapi(app, sdk)

    @app.get("/healthz")
    async def liveness():
        return {"status": "ok"}

    def failure(task_id, status):
        return JSONResponse(status_code=status, content={
            "taskId": task_id, "status": "FAILED", "errorMessage": FAILURE})

    async def forward(request, *, submit):
        import re
        sdk.track_request(1)
        task_id = ""
        try:
            try:
                body = await request.json()
            except ValueError:
                return failure(task_id, 422)
            if not isinstance(body, dict):
                return failure(task_id, 422)
            task_id = body.get("taskId", "")
            if not isinstance(task_id, str) or not re.fullmatch(TASK_PATTERN, task_id):
                return failure("", 422)
            if submit:
                response = await app.state.client.post(base + "/two-violation/compare", json=body)
            else:
                response = await app.state.client.get(base + "/two-violation/tasks/" + quote(task_id, safe=""))
            if response.status_code >= 400:
                log.error("[%s] upstream HTTP failure: status=%s body=%s", task_id,
                          response.status_code, response.text[:2000])
                return failure(task_id, response.status_code if response.status_code < 500 else 502)
            payload = response.json()
            data = payload.get("data")
            if (not isinstance(data, dict) or data.get("taskId") != task_id
                    or data.get("status") not in ("PENDING", "RUNNING", "SUCCESS", "FAILED")
                    or payload.get("code") not in (0, 500)):
                raise ValueError("unexpected algorithm response contract")
            data = dict(data)
            data["errorMessage"] = FAILURE if data["status"] == "FAILED" else None
            # Original API cannot distinguish missing-task from failed-task
            # after error sanitization. Preserve its FAILED query semantics.
            if submit and (payload["code"] != 0 or data["status"] == "FAILED"):
                return JSONResponse(status_code=409, content=data)
            return data
        except (httpx.HTTPError, ValueError, AttributeError):
            log.exception("[%s] upstream request failed; submit=%s; acceptance may be unknown",
                          task_id, submit)
            return failure(task_id, 502)
        finally:
            sdk.track_request(-1)

    @app.post("/gateway/tasks/submit")
    async def submit_task(request: Request):
        return await forward(request, submit=True)

    @app.post("/gateway/tasks/query")
    async def query_task(request: Request):
        return await forward(request, submit=False)

    async def forward_compatible(request: Request):
        """Preserve the original API contract; never retry a submission.

        These native routes are registered with the GET-capable SDK and are
        also usable directly. Legacy bridge routes remain as local aliases.
        """
        sdk.track_request(1)
        task_id = request.path_params.get("taskId", "")
        try:
            # raw_path preserves percent-encoded characters in a path parameter.
            path = request.scope["raw_path"].decode("ascii")
            query = request.scope.get("query_string", b"").decode("ascii")
            body = await request.body()
            headers = {}
            if "content-type" in request.headers:
                headers["content-type"] = request.headers["content-type"]
            response = await app.state.client.request(
                request.method, base + path + ("?" + query if query else ""),
                content=body, headers=headers,
            )
            # Preserve code/message/data, business HTTP 200 failures and
            # FastAPI validation errors exactly as the original service emits.
            return Response(content=response.content, status_code=response.status_code,
                            headers={"content-type": response.headers.get(
                                "content-type", "application/json")})
        except httpx.HTTPError:
            log.exception("[%s] compatibility upstream request failed; method=%s; "
                          "submission acceptance may be unknown", task_id, request.method)
            return JSONResponse(status_code=502, content={
                "code": 500, "message": "failed", "data": {
                    "taskId": task_id, "status": "FAILED", "errorMessage": FAILURE}})
        finally:
            sdk.track_request(-1)

    @app.post("/two-violation/compare")
    async def compatible_submit(request: Request):
        return await forward_compatible(request)

    @app.get("/two-violation/tasks/{taskId}")
    async def compatible_query(taskId: str, request: Request):
        return await forward_compatible(request)

    return app
