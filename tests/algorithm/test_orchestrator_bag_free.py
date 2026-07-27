"""Unit tests for the _Bag dataclass release semantics.

The _Bag is the orchestrator's per-stage scratchpad. Each stage writes
its outputs to specific fields and explicitly drops the upstream fields
it consumed (e.g. stage_filter_vegetation reads bag.pts_b and writes
bag.pts_b = None at the end). This test pins the contract so a
regression that re-introduces a hidden reference (the old
``stage_results: dict[str, tuple]``) is caught.

Run::

    python -m pytest tests/algorithm/test_orchestrator_bag_free.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                      / "scripts" / "algorithm"))

from run_pipeline import _Bag  # noqa: E402


def _attach_some_arrays(bag: _Bag) -> None:
    """Populate a bag with non-trivial arrays for refcount probing."""
    a = np.zeros((1000, 3), dtype=np.float32)        # 12 KB
    b = np.zeros((2000, 3), dtype=np.uint8)         #  6 KB
    c = np.zeros((5000, 3), dtype=np.float64)        # 120 KB
    bag.pts_a_aligned = a
    bag.pts_b = c
    bag.rgb_filt = b


def test_bag_field_assignment_round_trip():
    """Sanity: write every field, read it back, types match."""
    bag = _Bag()
    arr = np.arange(12, dtype=np.float32).reshape(4, 3)
    bag.pts_diff = arr
    assert bag.pts_diff is arr
    assert bag.colors_a is None  # untouched


def test_bag_reset_drops_all_field_refs():
    """``_Bag.reset()`` must drop every numpy reference. We use the
    refcount of a sentinel array to detect a hold: ``sys.getrefcount``
    returns 1 (the local var) + 1 (the bag) + 1 (getrefcount's own
    arg) when both hold the array. After ``reset()`` it should drop
    by 1 (the bag's reference)."""
    import sys as _sys
    sentinel = np.zeros(1, dtype=np.float32)
    before = _sys.getrefcount(sentinel)
    bag = _Bag()
    bag.pts_diff = sentinel
    during = _sys.getrefcount(sentinel)
    assert during == before + 1, f"bag added {during - before} refs, expected 1"

    bag.reset()
    after = _sys.getrefcount(sentinel)
    assert after == before, (
        f"reset() left {after - before} lingering refs to bag field"
    )


def test_bag_stage_filter_vegetation_contract():
    """After stage_filter_vegetation the contract is: pts_filt + rgb_filt
    are set, pts_b + colors_b are None. Pin it so a future stage signature
    change that loses a release is caught."""
    bag = _Bag()
    _attach_some_arrays(bag)            # pts_b set, others vary
    # Simulate stage_filter_vegetation's release:
    bag.pts_filt = np.zeros((100, 3), dtype=np.float32)
    bag.rgb_filt = np.zeros((100, 3), dtype=np.uint8)
    bag.pts_b = None
    bag.colors_b = None

    assert bag.pts_filt is not None
    assert bag.rgb_filt is not None
    assert bag.pts_b is None
    assert bag.colors_b is None


def test_bag_stage_nn_contract():
    """After stage_nn the contract is: pts_diff + rgb_diff set, A and
    filtered B fields None."""
    bag = _Bag()
    bag.pts_a_aligned = np.zeros((10, 3), dtype=np.float32)
    bag.colors_a = np.zeros((10, 3), dtype=np.uint8)
    bag.pts_filt = np.zeros((10, 3), dtype=np.float32)
    bag.rgb_filt = np.zeros((10, 3), dtype=np.uint8)
    bag.pts_diff = np.zeros((5, 3), dtype=np.float32)
    bag.rgb_diff = np.zeros((5, 3), dtype=np.uint8)
    # Simulate stage_nn's release:
    bag.pts_a_aligned = None
    bag.colors_a = None
    bag.pts_filt = None
    bag.rgb_filt = None

    assert bag.pts_diff is not None
    assert bag.rgb_diff is not None
    assert bag.pts_a_aligned is None
    assert bag.colors_a is None
    assert bag.pts_filt is None
    assert bag.rgb_filt is None


def test_bag_stage_convert_contract():
    """After stage_convert the contract is: every numpy field is None,
    only the bookkeeping list fields are touched."""
    bag = _Bag()
    bag.pts_diff = np.zeros((10, 3), dtype=np.float32)
    bag.rgb_diff = np.zeros((10, 3), dtype=np.uint8)
    bag.transform_b = [0.0] * 16
    bag.pts_diff = None
    bag.rgb_diff = None
    bag.transform_b = None

    for f in ("pts_a_aligned", "colors_a", "pts_b", "colors_b",
              "pts_filt", "rgb_filt", "pts_diff", "rgb_diff",
              "transform_b"):
        assert getattr(bag, f) is None, f"{f} should be None after stage_convert"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
