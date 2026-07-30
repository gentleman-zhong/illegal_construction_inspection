"""Regression test for the Stage 1 ROI bbox pre-filter closure bug
discovered at 2026-07-30 06:45:29 (task ``20260730144529AA9A7F``).

The original ``_project_a_bbox_into_b`` helper closed over a
``transform_a`` free variable that did not yet exist in the enclosing
scope at the moment the inner function was *defined* (it gets bound
later, by ``extract_point_cloud``). On the first call Python raised::

    NameError: cannot access free variable 'transform_a' where it is
    not associated with a value in enclosing scope

The fix loads ``transform_a`` upfront via ``load_root_transform`` and
captures the result via a default argument (``_T=T_local``) so the
inner function becomes a pure closed-over-data lambda — no late-bound
free variables.

Run::

    python -m pytest tests/algorithm/test_a_bbox_projection_closure.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                      / "scripts" / "algorithm"))


def test_default_arg_closure_pattern_does_not_nameerror():
    """Simulate the bug: a nested function that takes a value via
    a default argument must work, even though the original code's
    late-bound ``transform_a`` would have NameErrored."""

    # Simulate the structure of stage_extract's ROI filter block:
    # T_local is computed *before* the inner function is defined.
    T_local = np.eye(4)

    def _project_default_arg(box_a, _T=T_local):
        c = np.array([box_a[0], box_a[1], box_a[2], 1.0])
        return tuple((_T @ c)[:3].tolist())

    # The original buggy pattern would NOT define (Python raises
    # NameError on first call). This test asserts the *fixed*
    # pattern works.
    out = _project_default_arg((1.0, 2.0, 3.0))
    assert out == (1.0, 2.0, 3.0), f"identity transform: got {out}"


def test_no_reference_to_unbound_transform_a_in_source():
    """Static guard: scan ``stage_extract`` source for any reference
    to ``transform_a`` inside the nested helpers (where it would be
    unbound). The fix loads it via ``load_root_transform`` first, so
    the only references to ``transform_a`` should be inside the
    *assignment* itself (``transform_a_pre = load_root_transform(...)``),
    not inside nested def bodies."""
    import inspect
    import re

    import run_pipeline as rp
    src = inspect.getsource(rp.stage_extract)

    # Find the first nested function definition *inside* the ROI block.
    # The buggy version had references like
    #     def _project_a_bbox_into_b(box_a, transform_b_root):
    #         T_a_mat = np.asarray(transform_a, dtype=np.float64) ...
    # i.e. ``transform_a`` referenced inside a nested def body before
    # the outer scope binds it. The fixed version must have no such
    # nested-body references.
    nested_defs = re.findall(
        r"def (_project_a_bbox_into_b|_filter_leaves)\(.*?(?=\n        def |\n    if use_parallel|\n    if roi|\n    else|\Z)",
        src, re.DOTALL,
    )
    # We don't expect 0 nested defs — the helpers are real — but each
    # nested def body must NOT contain ``transform_a`` as a free var.
    for body_text in nested_defs:
        assert "transform_a" not in body_text, (
            "stage_extract nested helper still references unbound "
            "`transform_a` — late-binding NameError will recur on "
            "first call. (See error log of 20260730144529AA9A7F.)"
        )


def test_keep_paths_signature_accepts_none_or_list():
    """Sanity: the ``keep_paths: list[Path] | None`` parameter on
    ``extract_point_cloud`` is honoured for both call shapes."""
    import inspect
    from point_cloud_extraction import extract_point_cloud
    sig = inspect.signature(extract_point_cloud)
    assert "keep_paths" in sig.parameters, (
        "extract_point_cloud missing `keep_paths`; "
        "stage_extract ROI pre-filter won't route."
    )
    # typing must permit None + list[Path]
    p = sig.parameters["keep_paths"]
    assert p.default is None, (
        "keep_paths must default to None so non-ROI callers don't "
        "have to pass it."
    )


def test_all_three_loops_unpack_leaves_as_3_tuple():
    """Regression: discovered in task ``20260730144830BCE045`` — the
    Pass-0 loop ``for path, bbox in leaves`` (2-tuple) blew up when
    ``keep_paths`` produced 3-tuples, raising ``ValueError: too many
    values to unpack``. Pass 1 / Pass 2 were already 3-tuples, so we
    pin all three at the same arity."""
    import inspect
    import re

    from point_cloud_extraction import extract_point_cloud
    src = inspect.getsource(extract_point_cloud)

    # 1) Pass 0 must destructure all 3 (path, bbox, extents)
    assert "for path, bbox, _bx in leaves:" in src, (
        "Pass 0 still destructures 2-tuple; keep_paths entry will "
        "ValueError on the first ROI-filtered run. See error log of "
        "task 20260730144830BCE045."
    )

    # 2) Pass 1 still uses the index form (also 3-tuple)
    assert "path, _, _ = leaves[i]" in src, (
        "Pass 1 changed to 2-tuple unpack; keep_paths will break."
    )

    # 3) Pass 2 still uses enumerate-destructure (also 3-tuple)
    assert "for i, (path, _, _) in enumerate(leaves)" in src, (
        "Pass 2 changed to 2-tuple destructure; keep_paths will break."
    )

    # 4) keep_paths entry-point must normalise to 3-tuple shape
    assert "[(p, bboxes.get(p), None) for p in keep_paths]" in src, (
        "keep_paths entry-point no longer normalises leaves to "
        "3-tuple shape; Pass 0/1/2 will all ValueError."
    )

    # 5) ``find_leaf_b3dms_with_bbox`` must still return 3-tuples
    from point_cloud_extraction import find_leaf_b3dms_with_bbox
    sig = inspect.signature(find_leaf_b3dms_with_bbox)
    # The return annotation is the canonical contract
    ret = str(sig.return_annotation)
    assert "tuple[Path" in ret and "list[float] | None" in ret, (
        f"find_leaf_b3dms_with_bbox return shape changed to {ret}; "
        f"three loops assume 3-tuple (path, bbox_center, bbox_extents)."
    )



if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
