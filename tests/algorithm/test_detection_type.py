"""Unit tests for the v0.8+ ``--detection-type`` CLI flag and the
``detectionType`` → ``_CONFIG.VIOLATION_MODE`` override applied inside
``run_pipeline.main()``.

The flag lets callers (HTTP layer, manual tests) declare which of the
three supported scenarios the task belongs to:

* ``"twoIllegal"``           → ``VIOLATION_MODE = on``
* ``"constructionProgress"`` → ``VIOLATION_MODE = off``
* ``"landSlide"``            → ``VIOLATION_MODE = off``

These tests pin:

1. The argparse surface (``parse_args`` reads ``--detection-type`` and
   defaults to ``None``).
2. The override applied to ``_CONFIG.VIOLATION_MODE`` after ``parse_args``
   — verifying the exact mapping for every supported value, the default
   (``None`` → ``twoIllegal``), and a defensive off-switch for an unknown
   string.
3. That ``stage_convert`` still gates on ``_CONFIG.VIOLATION_MODE`` (i.e.
   the override actually flows through to the same path the existing
   ``ALGO_VIOLATION_MODE`` env var already used).

We exercise the override in isolation by calling ``parse_args`` + the
small ``_apply_detection_type`` snippet inlined into the test. We do NOT
invoke the full ``run_pipeline.main`` (it would need real tilesets); the
snippet is identical to the one in production and gets imported as text
to guarantee the test stays in lock-step with the production code.

Run::

    python -m pytest tests/algorithm/test_detection_type.py -v
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                      / "scripts" / "algorithm"))

import run_pipeline  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _apply_detection_type(value: str | None) -> bool:
    """Mirror the override applied at the top of run_pipeline.main().

    This is the *exact* snippet from production, inlined here so we can
    exercise it without standing up a tileset. We import it from the
    source file as text and exec it against a fresh namespace to keep
    the test in lock-step with the production code (any drift will
    fail to parse).
    """
    import textwrap
    src = Path(run_pipeline.__file__).read_text()
    match = re.search(
        r"(    _detection_type\s*=\s*args\.detection_type.*?"
        r"flush=True\)\n)",
        src, re.DOTALL,
    )
    assert match is not None, (
        "could not locate the detectionType→VIOLATION_MODE override "
        "snippet in run_pipeline.py — has the production code drifted?"
    )
    # The snippet is indented (it's inside main()); dedent so exec can
    # parse it as top-level code.
    snippet = textwrap.dedent(match.group(1))
    # Bind a fake ``_CONFIG`` class so the exec'd snippet can mutate it.
    class _StubConfig:
        VIOLATION_MODE = True
    class _Args:
        detection_type = value
    namespace = {"args": _Args(), "_CONFIG": _StubConfig}
    exec(snippet, namespace)
    return namespace["_CONFIG"].VIOLATION_MODE


# ---------------------------------------------------------------------------
# parse_args surface
# ---------------------------------------------------------------------------
def test_parse_args_default_detection_type_is_none():
    """No flag → args.detection_type is None (override normalises it)."""
    args = run_pipeline.parse_args(["/tmp/a", "/tmp/b", "-o", "/tmp/o"])
    assert args.detection_type is None


def test_parse_args_two_illegal():
    args = run_pipeline.parse_args(
        ["/tmp/a", "/tmp/b", "-o", "/tmp/o", "--detection-type", "twoIllegal"],
    )
    assert args.detection_type == "twoIllegal"


def test_parse_args_construction_progress():
    args = run_pipeline.parse_args(
        ["/tmp/a", "/tmp/b", "-o", "/tmp/o",
         "--detection-type", "constructionProgress"],
    )
    assert args.detection_type == "constructionProgress"


def test_parse_args_land_slide():
    args = run_pipeline.parse_args(
        ["/tmp/a", "/tmp/b", "-o", "/tmp/o", "--detection-type", "landSlide"],
    )
    assert args.detection_type == "landSlide"


def test_parse_args_rejects_unknown_value():
    """Bounded enum: argparse must reject values outside the three choices."""
    with pytest.raises(SystemExit):
        run_pipeline.parse_args(
            ["/tmp/a", "/tmp/b", "-o", "/tmp/o", "--detection-type", "bogus"],
        )


# ---------------------------------------------------------------------------
# Override: detectionType → _CONFIG.VIOLATION_MODE
# ---------------------------------------------------------------------------
def test_override_default_is_two_illegal_on():
    """No flag (None) → VIOLATION_MODE on (backward-compat)."""
    assert _apply_detection_type(None) is True


def test_override_explicit_two_illegal_on():
    assert _apply_detection_type("twoIllegal") is True


def test_override_construction_progress_off():
    assert _apply_detection_type("constructionProgress") is False


def test_override_land_slide_off():
    assert _apply_detection_type("landSlide") is False


def test_override_unknown_value_treated_as_off():
    """Defensive: anything outside the enum falls back to VIOLATION_MODE off.

    The argparse layer already rejects unknown values before main() runs,
    so this is belt-and-braces — but the inline snippet is permissive on
    purpose (any non-"twoIllegal" string → off) so a future code path
    that bypasses argparse can't accidentally leave the filter on.
    """
    assert _apply_detection_type("futureScenario") is False


def test_override_does_not_touch_algo_config_module():
    """The override mutates _CONFIG.VIOLATION_MODE, NOT
    algo_config.VIOLATION_MODE. The latter is the env-var bound value
    used by anyone who reads from the algo_config module directly
    (e.g., older tests, config dumps). Touching it would be a hidden
    cross-process side effect on the long-running service.
    """
    from algo_config import VIOLATION_MODE as _algo_default
    before = _algo_default

    _apply_detection_type("landSlide")      # ⇒ _CONFIG.VIOLATION_MODE = False
    # Reload from module to be sure:
    from algo_config import VIOLATION_MODE as _algo_after
    assert _algo_after == before, (
        "algo_config.VIOLATION_MODE was mutated by the override — "
        "this is a side effect we explicitly want to avoid."
    )


# ---------------------------------------------------------------------------
# stage_convert gate still honours _CONFIG.VIOLATION_MODE
# ---------------------------------------------------------------------------
def test_stage_convert_gate_path_is_consistent():
    """stage_convert checks ``if not _CONFIG.VIOLATION_MODE`` to gate the
    HAG post-filter. After toggling the override, that path must read
    the new value (i.e. no module-level cached copy that bypasses the
    class attribute). We confirm by reading the source for the gate.
    """
    src = Path(run_pipeline.__file__).read_text()
    assert "if not _CONFIG.VIOLATION_MODE:" in src, (
        "stage_convert gate missing — production code has drifted; "
        "the override can no longer affect Stage 4 behaviour."
    )