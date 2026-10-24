"""Tests for the v0.8+ ``detectionType`` request field on
``api_server.SubmitRequest`` and the threading of the value through the
HTTP request handler into the in-memory ``TaskStore``.

The schema is the public contract; the response builders and the
``TaskStatus.detection_type`` field are the internal pluming. Together
they guarantee:

* A request without ``detectionType`` is accepted (Pydantic ``None``
  default) and the submitted task has ``detection_type == "twoIllegal"``.
* A request with ``detectionType`` set to one of the three enum values
  is accepted and the value flows through verbatim.
* A request with an unknown ``detectionType`` is rejected with HTTP 422
  (Pydantic ``Literal[...]`` validation).
* The poll response (``GET /two-violation/tasks/{taskId}``) and the
  terminal callback both carry the ``detectionType`` value back to the
  caller.
* The archived ``<out_dir>/request.json`` payload records the value.

These tests run at the unit level — they do NOT spin up uvicorn. We
invoke the Pydantic schema directly, call ``_poll_ok`` /
``_poll_fail`` / ``_archive_metadata`` as plain functions, and exercise
``submit()`` by monkeypatching ``api_server.store`` with a recording
stub (the FastAPI route handler is just a thin wrapper around the same
function).

Run::

    python -m pytest tests/service/test_api_server_detection_type.py -v
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parent.parent.parent / "scripts" / "service"
_ALGO = _SERVICE.parent / "algorithm"
sys.path.insert(0, str(_SERVICE))
sys.path.insert(0, str(_ALGO))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def api_module(monkeypatch_module):
    """Import api_server once per module with the heavy imports stubbed.

    We set env vars BEFORE import so api_server's "pre-flight OK" branch
    degrades to a no-op (the test does not need a real tileset), and so
    OUTPUT_BASE_DIR points inside the test directory (no /var/lib
    write needed).
    """
    # Force re-import in case another test in this session polluted sys.modules.
    for mod_name in list(sys.modules):
        if mod_name == "api_server" or mod_name.startswith("api_server."):
            del sys.modules[mod_name]
    if "task_manager" in sys.modules:
        del sys.modules["task_manager"]

    out_dir = Path(__file__).resolve().parent / "_test_output"
    out_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch_module.setenv("OUTPUT_BASE_DIR", str(out_dir))
    monkeypatch_module.setenv("MAX_CONCURRENT_TASKS", "1")
    monkeypatch_module.setenv("ALGO_DISABLE_OOM_PREFLIGHT", "1")

    return pytest.importorskip("api_server")


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture
def stub_store(api_module, monkeypatch):
    """Replace api_server.store with a recording stub.

    Captures every ``submit(...)`` call so tests can assert on the
    ``detection_type`` kwarg flowing through.
    """
    captured: dict[str, list] = {"submits": []}

    class _StubStatus:
        def __init__(self, task_id, detection_type="twoIllegal"):
            self.task_id = task_id
            self.detection_type = detection_type
            self.state = "PENDING"
            self.progress = 0
            self.step = "waiting"
            self.three_dtiles_paths = []
            self.oss_chunk_urls = {}
            self.oss_instance_url = None
            self.error_message = None
            self.output_dir = str(
                Path(__file__).resolve().parent / "_test_output" / task_id,
            )

    class _StubStore:
        def __init__(self):
            self._status: dict[str, _StubStatus] = {}
            self._lock = threading.Lock()

        def submit(self, *, task_id, base_model_path, compare_model_path,
                  xml_path=None, position_mode=None, area_coordinates=None,
                  radius=None, detection_type="twoIllegal"):
            captured["submits"].append({
                "task_id": task_id,
                "detection_type": detection_type,
            })
            with self._lock:
                st = _StubStatus(task_id=task_id, detection_type=detection_type)
                self._status[task_id] = st
            return st

        def get(self, task_id):
            return self._status.get(task_id)

    store = _StubStore()
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(
        api_module, "_preflight_with_timeout",
        lambda base_path, timeout_s: {"status": "ok", "n_b": 0},
    )
    return store, captured


def _call_submit(api_module, body):
    """Invoke the submit() route handler directly with a dict body.

    FastAPI normally parses the body via Pydantic before invoking the
    handler. We replicate that minimal step by constructing
    ``SubmitRequest(**body)`` ourselves; if Pydantic rejects, the
    exception propagates and the test asserts on it.
    """
    req = api_module.SubmitRequest(**body)
    return api_module.submit(req)


def test_public_failure_policy(api_module, caplog):
    from error_policy import PUBLIC_FAILURE_MESSAGE
    reason = "ValueError: secret internal path /model/private"
    assert api_module._submit_fail("FAIL-1", reason)["data"]["errorMessage"] == PUBLIC_FAILURE_MESSAGE
    assert reason in caplog.text
    assert api_module._poll_fail("MISSING", reason)["data"]["errorMessage"] == PUBLIC_FAILURE_MESSAGE


@pytest.mark.parametrize("state", ["FAILED", "SUCCESS"])
def test_callback_hides_internal_error(api_module, monkeypatch, state):
    from task_manager import TaskStore, TaskStatus
    from error_policy import PUBLIC_FAILURE_MESSAGE
    from types import SimpleNamespace
    status = TaskStatus(task_id="CB-1", state=state, error_message="OSS upload failed: private detail")
    captured = []
    monkeypatch.setattr("task_manager.requests.post", lambda *a, **kw: (
        captured.append(kw["json"]) or SimpleNamespace(
            status_code=200, raise_for_status=lambda: None)))
    store = SimpleNamespace(callback_url="https://example.invalid/callback",
                            callback_timeout=10, callback_max_retries=1)
    TaskStore._send_callback(store, status)
    assert captured[0]["errorMessage"] == (PUBLIC_FAILURE_MESSAGE if state == "FAILED" else None)
    assert status.error_message == "OSS upload failed: private detail"


def test_diagnostics_preserve_traceback(tmp_path):
    from run_pipeline_subprocess import StatusWriter
    writer = StatusWriter(tmp_path)
    writer.record_error("Traceback: original technical detail")
    writer.write(step="failed", progress=0, status="FAILED", error="ValueError: bad model")
    assert "Traceback: original technical detail" in (tmp_path / "error.log").read_text()
    assert json.loads((tmp_path / "status.json").read_text())["error"] == "ValueError: bad model"


# ---------------------------------------------------------------------------
# Pydantic schema contract
# ---------------------------------------------------------------------------
def test_submit_request_default_detection_type_is_none(api_module):
    """Missing detectionType → Pydantic accepts (default None)."""
    req = api_module.SubmitRequest(
        taskId="X", baseModelPath="/a", compareModelPath="/b",
    )
    assert req.detectionType is None


def test_submit_request_explicit_construction_progress(api_module):
    req = api_module.SubmitRequest(
        taskId="X", baseModelPath="/a", compareModelPath="/b",
        detectionType="constructionProgress",
    )
    assert req.detectionType == "constructionProgress"


def test_submit_request_explicit_land_slide(api_module):
    req = api_module.SubmitRequest(
        taskId="X", baseModelPath="/a", compareModelPath="/b",
        detectionType="landSlide",
    )
    assert req.detectionType == "landSlide"


def test_submit_request_invalid_detection_type_rejected(api_module):
    """Literal[...] validation: unknown value must raise ValidationError."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        api_module.SubmitRequest(
            taskId="X", baseModelPath="/a", compareModelPath="/b",
            detectionType="bogus",
        )
    # The error path must mention detectionType
    assert "detectionType" in str(exc.value)


# ---------------------------------------------------------------------------
# submit() handler propagation
# ---------------------------------------------------------------------------
def test_submit_default_detection_type_propagates_to_store(api_module, stub_store):
    _, captured = stub_store
    body = {"taskId": "TW-DET-DEFAULT",
            "baseModelPath": "/tmp/a", "compareModelPath": "/tmp/b"}
    resp = _call_submit(api_module, body)
    assert resp["code"] == 0
    assert captured["submits"], "store.submit was not called"
    assert captured["submits"][-1]["detection_type"] == "twoIllegal"


def test_submit_explicit_construction_progress_propagates(api_module, stub_store):
    _, captured = stub_store
    body = {"taskId": "TW-DET-CP",
            "baseModelPath": "/tmp/a", "compareModelPath": "/tmp/b",
            "detectionType": "constructionProgress"}
    resp = _call_submit(api_module, body)
    assert resp["code"] == 0
    assert captured["submits"][-1]["detection_type"] == "constructionProgress"


def test_submit_explicit_land_slide_propagates(api_module, stub_store):
    _, captured = stub_store
    body = {"taskId": "TW-DET-LS",
            "baseModelPath": "/tmp/a", "compareModelPath": "/tmp/b",
            "detectionType": "landSlide"}
    resp = _call_submit(api_module, body)
    assert resp["code"] == 0
    assert captured["submits"][-1]["detection_type"] == "landSlide"


# ---------------------------------------------------------------------------
# request.json archive
# ---------------------------------------------------------------------------
def test_archive_metadata_records_detection_type(api_module):
    out_dir = Path(__file__).resolve().parent / "_test_output" / "_archive_ls"
    out_dir.mkdir(parents=True, exist_ok=True)
    api_module._archive_metadata(
        out_dir, "TW-ARCHIVE-LS",
        position_mode=None, area_coordinates=None, radius=None,
        xml_path=None,
        base_model_path="/tmp/a", compare_model_path="/tmp/b",
        base_model_path_resolved="/tmp/a",
        compare_model_path_resolved="/tmp/b",
        detection_type="landSlide",
        submitted_at="2026-08-25T00:00:00+00:00",
    )
    payload = json.loads((out_dir / "request.json").read_text())
    assert payload.get("detectionType") == "landSlide"


def test_archive_metadata_records_two_illegal_when_missing(api_module):
    out_dir = (Path(__file__).resolve().parent / "_test_output"
               / "_archive_def")
    out_dir.mkdir(parents=True, exist_ok=True)
    api_module._archive_metadata(
        out_dir, "TW-ARCHIVE-DEF",
        position_mode=None, area_coordinates=None, radius=None,
        xml_path=None,
        base_model_path="/tmp/a", compare_model_path="/tmp/b",
        base_model_path_resolved="/tmp/a",
        compare_model_path_resolved="/tmp/b",
        # detection_type kwarg intentionally omitted (simulates missing arg)
        submitted_at="2026-08-25T00:00:00+00:00",
    )
    payload = json.loads((out_dir / "request.json").read_text())
    assert payload.get("detectionType") == "twoIllegal"


# ---------------------------------------------------------------------------
# _poll_ok / _poll_fail response builders
# ---------------------------------------------------------------------------
def _make_status(**kwargs):
    return type("S", (), {
        "task_id": "X", "progress": 50, "state": "RUNNING", "step": "x",
        "three_dtiles_paths": [], "oss_chunk_urls": {}, "oss_instance_url": None,
        "detection_type": "twoIllegal",
        **kwargs,
    })()


def test_poll_ok_includes_detection_type(api_module):
    status = _make_status(detection_type="landSlide")
    payload = api_module._poll_ok(status)
    assert payload["data"]["detectionType"] == "landSlide"


def test_poll_ok_default_detection_type(api_module):
    status = _make_status()      # default = twoIllegal
    payload = api_module._poll_ok(status)
    assert payload["data"]["detectionType"] == "twoIllegal"


def test_poll_fail_with_status_includes_detection_type(api_module):
    status = _make_status(
        state="FAILED", detection_type="constructionProgress",
    )
    payload = api_module._poll_fail("X", "boom", status)
    assert payload["code"] == 500
    assert payload["data"]["detectionType"] == "constructionProgress"


def test_poll_fail_without_status_uses_default(api_module):
    payload = api_module._poll_fail("X", "boom", None)
    assert payload["code"] == 500
    assert payload["data"]["detectionType"] == "twoIllegal"


def test_poll_response_data_has_eight_fields(api_module):
    """Pin the response contract: ``data`` must have exactly 8 keys."""
    status = _make_status()
    payload = api_module._poll_ok(status)
    assert len(payload["data"]) == 8
    expected = {
        "taskId", "progress", "status", "step",
        "3dtilesUrl", "instanceJsonUrl", "detectionType", "errorMessage",
    }
    assert set(payload["data"].keys()) == expected
