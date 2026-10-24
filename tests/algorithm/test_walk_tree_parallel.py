"""Regression tests for v0.8 Stage 1 change #9 — `_walk_tree` now uses
a thread-pool-preloaded cache of sub-tileset.json instead of issuing
synchronous reads at each level.

Previously ``_walk_tree`` recursively called ``sub_path.read_text()``
at every `.json` content URI — on NFS this is ~25 ms per RPC, so a
200-leaf sub-tileset tree paid ~5 s of pure NFS latency per call, and
``find_leaf_b3dms_with_bbox`` was called twice per Stage 1 run (A + B),
costing ~120 s of the 442.8 s Stage 1 budget on b-class tilesets.

The fix introduces ``_preload_sub_tilesets`` which BFS-walks the
tileset hierarchy breadth-first and reads each layer with a
ThreadPoolExecutor (threads release the GIL during read). The walker
then looks up cached sub-tilesets instead of issuing sync reads.

Run::

    python -m pytest tests/algorithm/test_walk_tree_parallel.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                      / "scripts" / "algorithm"))


def _make_sub_tileset(root: Path, depth: int, fanout: int,
                      tile_seq: list[str]) -> dict:
    """Recursively write ``depth`` levels of sub-tileset.json, each
    pointing to ``fanout`` children via ``content.uri``. ``tile_seq``
    is appended with each `.b3dm` URI found in the leaves. Returns
    the root tileset's ``{"root": ...}`` dict."""
    children = []
    if depth == 0:
        # Leaf: emit one b3dm per child
        for i in range(fanout):
            uri = f"leaf_{len(tile_seq):04d}.b3dm"
            (root / uri).write_bytes(b"\x00")  # we don't parse leaves
            tile_seq.append(str((root / uri).resolve()))
            children.append({"content": {"uri": uri}})
        return {"root": {
            "boundingVolume": {"box": [0, 0, 0, 50, 50, 50]},
            "children": children,
        }}

    # Non-leaf: recurse into fanout subdirectories
    for i in range(fanout):
        sub_dir = root / f"L{i}"
        sub_dir.mkdir()
        sub_ts = _make_sub_tileset(sub_dir, depth - 1, fanout, tile_seq)
        sub_json = sub_dir / "tileset.json"
        sub_json.write_text(json.dumps(sub_ts))
        children.append({"content": {"uri": f"L{i}/tileset.json"}})
    return {"root": {
        "boundingVolume": {"box": [0, 0, 0, 100, 100, 100]},
        "children": children,
    }}


def test_preload_cache_returns_all_sub_tilesets(tmp_path: Path):
    """Cache must contain every sub-tileset.json under the root."""
    from point_cloud_extraction import _preload_sub_tilesets

    root = tmp_path / "ts"
    root.mkdir()
    tile_seq: list[str] = []
    ts = _make_sub_tileset(root, depth=3, fanout=2, tile_seq=tile_seq)
    ts_path = root / "tileset.json"
    ts_path.write_text(json.dumps(ts))
    cache = _preload_sub_tilesets(ts_path)
    # 3 levels deep, fanout 2: 1 (top) + 2 (L0,L1) + 4 (LL0..LL3) + 8
    # (LLL0..LLL7) = 15 sub-tilesets. The leaves don't have .json files,
    # they're .b3dm, so they're NOT in the cache.
    expected_json_count = 1 + 2 + 4 + 8
    assert len(cache) == expected_json_count, (
        f"_preload_sub_tilesets got {len(cache)} entries, "
        f"expected {expected_json_count} (1 root + 2 + 4 + 8 sub.json). "
        "BFS missing some levels."
    )
    # All cached entries must be valid JSON (parsed dicts, not raw text)
    for path, val in cache.items():
        assert isinstance(val, dict), (
            f"Cache entry for {path} is not a dict (got {type(val).__name__})"
        )
        assert "root" in val, f"Cache entry for {path} missing 'root'"


def test_find_leaf_b3dms_with_bbox_uses_cache(tmp_path: Path, monkeypatch):
    """``find_leaf_b3dms_with_bbox`` must call ``_preload_sub_tilesets``
    *before* walking, and must produce the same leaf list as the
    legacy on-demand path."""
    from point_cloud_extraction import (
        find_leaf_b3dms_with_bbox,
        _preload_sub_tilesets,
        _walk_tree,
    )

    root = tmp_path / "ts"
    root.mkdir()
    tile_seq: list[str] = []
    ts = _make_sub_tileset(root, depth=2, fanout=3, tile_seq=tile_seq)
    (root / "tileset.json").write_text(json.dumps(ts))

    preload_calls = {"n": 0}
    real_preload = _preload_sub_tilesets

    def counting_preload(ts_path):
        preload_calls["n"] += 1
        return real_preload(ts_path)

    monkeypatch.setattr(
        "point_cloud_extraction._preload_sub_tilesets", counting_preload,
    )
    leaves = find_leaf_b3dms_with_bbox(root)
    assert preload_calls["n"] == 1, (
        f"_preload_sub_tilesets called {preload_calls['n']} times; "
        "find_leaf_b3dms_with_bbox must call it exactly once."
    )
    # Same set of .b3dm paths as the tile_seq we built
    found = sorted(str(p) for (p, _, _) in leaves)
    expected = sorted(tile_seq)
    assert found == expected, (
        f"Leaf paths differ between parallel-preload path and "
        f"the synthetic tile_seq.\n"
        f"  Found:    {found[:3]}...{found[-3:]}\n"
        f"  Expected: {expected[:3]}...{expected[-3:]}"
    )


def test_walk_tree_legacy_path_still_works_without_cache(tmp_path: Path):
    """``_walk_tree(cache=None)`` must still fall back to on-demand
    ``read_text()`` (legacy callers / external tests)."""
    from point_cloud_extraction import _walk_tree

    root = tmp_path / "ts"
    root.mkdir()
    tile_seq: list[str] = []
    ts = _make_sub_tileset(root, depth=2, fanout=2, tile_seq=tile_seq)
    (root / "tileset.json").write_text(json.dumps(ts))

    root_node = json.loads((root / "tileset.json").read_text())["root"]
    out: list = []
    seen: set = set()
    _walk_tree(root_node, root, out, seen, cache=None)
    found = sorted(str(p) for (p, _, _) in out)
    expected = sorted(tile_seq)
    assert found == expected, (
        f"Legacy cache=None path produced wrong leaves:\n"
        f"  Found:    {found[:3]}...\n  Expected: {expected[:3]}..."
    )


def test_preload_handles_missing_sub_tileset_gracefully(tmp_path: Path):
    """A single missing sub-tileset.json must NOT abort preload.
    The walker was equally lenient (warning + continue), and the
    cache preload must match."""
    import warnings
    from point_cloud_extraction import _preload_sub_tilesets

    root = tmp_path / "ts"
    root.mkdir()
    # Build a 2-level tree but delete one of the .json files
    tile_seq: list[str] = []
    ts = _make_sub_tileset(root, depth=2, fanout=2, tile_seq=tile_seq)
    (root / "tileset.json").write_text(json.dumps(ts))
    # Delete L0/tileset.json — preload must skip it without raising.
    (root / "L0" / "tileset.json").unlink()
    # The preload code is lenient: it silently skips missing files
    # (returns None from _safe_read_text). We just verify that the
    # function returns without raising and the surviving entries are
    # in the cache.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cache = _preload_sub_tilesets(root / "tileset.json")
    # The cache should still contain the top + L1 (L0 was missing)
    assert (root / "tileset.json") in cache, "Top-level missing from cache"
    assert (root / "L1" / "tileset.json") in cache, "L1 missing from cache"
    assert (root / "L0" / "tileset.json") not in cache, (
        "L0 was deleted but somehow still in cache"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))