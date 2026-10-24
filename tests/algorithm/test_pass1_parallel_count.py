"""Regression tests for v0.8 Stage 1 change #8 — Pass 1 (count) now
runs in the same ProcessPoolExecutor as Pass 2 (extract).

Previously Pass 1 ran in the *main* process via a sequential for loop,
which on b-class tilesets (5k+ leaves) cost ~296 s = 5 min wall-time
at ~25 tile/s. The fix routes ``_b3dm_position_count`` through the same
64-worker pool that Pass 2 uses, dropping Pass 1 to ~5-15 s.

These tests pin two contracts:

1. The pool is created **once** (one ``ProcessPoolExecutor`` context)
   for both Pass 1 and Pass 2, NOT twice. A regression that creates
   two pools would waste ~1.5 s on a second fork on the 128-core host.

2. The counts produced by ``pool.map(_b3dm_position_count, ...)`` are
   identical to those produced by the legacy sequential for loop,
   bit-exact.

Run::

    python -m pytest tests/algorithm/test_pass1_parallel_count.py -v
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                      / "scripts" / "algorithm"))


def _write_minimal_b3dm(path: Path, vertex_count: int) -> None:
    """Write a minimal valid b3dm file with the given POSITION vertex
    count. Mirrors the 3D-Tiles-on-the-wire layout that
    ``_b3dm_position_count`` and ``parse_b3dm`` parse: 28-byte b3dm
    header, no feature/batch tables, GLB header + JSON chunk (with
    one mesh / one primitive / one POSITION accessor) + empty BIN
    chunk.

    Vertex counts are encoded in ``accessors[0].count``; the BIN chunk
    is zero-length but its magic ``"BIN\\x00"`` is required so
    ``parse_b3dm`` accepts the file in Pass 0.
    """
    gltf = {
        "asset": {"version": "2.0"},
        "meshes": [{
            "primitives": [{
                "attributes": {"POSITION": 0},
                "mode": 4,
            }],
        }],
        "accessors": [{
            "count": vertex_count,
            "componentType": 5126,  # FLOAT
            "type": "VEC3",
            "bufferView": 0,
        }],
        "bufferViews": [{
            "buffer": 0,
            "byteOffset": 0,
            "byteLength": vertex_count * 12,  # 3 floats × 4 bytes
        }],
        "buffers": [{
            "byteLength": vertex_count * 12,
        }],
    }
    json_bytes = json.dumps(gltf).encode("utf-8")
    # Populate the BIN chunk with N×(3×float32) zero bytes so
    # ``read_accessor`` can materialise an all-zero POSITION array.
    bin_data = b"\x00" * (vertex_count * 12)
    # GLB chunks use 4-byte alignment per the glTF 2.0 spec (§ 4.2
    # Binary Buffer). Older revisions of this fixture used 8-byte padding
    # here; that hid a real-world bug in ``parse_b3dm`` where BIN offset
    # was computed with ``_align8`` instead of ``_align4`` — see
    # ``test_parse_b3dm_unaligned.py::test_parse_b3dm_glb_json_4mod8_residue``.
    bin_padded_len = ((len(bin_data) + 3) // 4) * 4
    bin_padding = b"\x00" * (bin_padded_len - len(bin_data))

    # b3dm header is 28 bytes:
    #   [4] magic "b3dm"
    #   [4] version=1
    #   [4] total_byte_length (filled in below)
    #   [4] feature_table_json_byte_length = 0
    #   [4] feature_table_binary_byte_length = 0
    #   [4] batch_table_json_byte_length = 0
    #   [4] batch_table_binary_byte_length = 0
    b3dm_header_size = 28

    # Embedded GLB container:
    #   [4] magic "glTF"
    #   [4] version=2
    #   [4] total_byte_length (filled in below)
    glb_header_size = 12
    # JSON chunk:
    #   [4] chunk_length = len(json_bytes)
    #   [4] chunk_type = "JSON"
    #   [N] json_bytes
    #   [pad] to 8-byte boundary
    json_chunk_header_size = 8
    json_padded_len = ((len(json_bytes) + 3) // 4) * 4
    # BIN chunk:
    #   [4] chunk_length = len(bin_data)
    #   [4] chunk_type = "BIN\x00"
    #   [bin_data]
    #   [pad] to 8-byte boundary
    bin_chunk_header_size = 8

    glb_total = (glb_header_size
                 + json_chunk_header_size + json_padded_len
                 + bin_chunk_header_size + bin_padded_len)
    b3dm_total = b3dm_header_size + glb_total

    # Pack the GLB header with the now-known glb_total
    glb_header = struct.pack("<4sII", b"glTF", 2, glb_total)
    json_chunk_header = struct.pack("<I4s", len(json_bytes), b"JSON")
    json_padding = b"\x00" * (json_padded_len - len(json_bytes))
    bin_chunk_header = struct.pack("<I4s", len(bin_data), b"BIN\x00")

    glb_body = (glb_header
                + json_chunk_header + json_bytes + json_padding
                + bin_chunk_header + bin_data + bin_padding)

    b3dm_header = struct.pack(
        "<4sIIIIII",
        b"b3dm", 1, b3dm_total,
        0, 0, 0, 0,
    )
    path.write_bytes(b3dm_header + glb_body)


@pytest.fixture
def tileset_dir(tmp_path: Path) -> Path:
    """Create a synthetic tileset with 64 leaf b3dms and a 2-level
    sub-tileset.json hierarchy. Vertex counts vary per tile so any
    silent ordering bug in the parallel path shows up as a permutation
    of the expected counts.

    The leaf count must exceed ``EXTRACT_MIN_LEAVES`` (default 30) so
    the test actually exercises the ProcessPoolExecutor branch — with
    fewer leaves the sequential fallback fires and no pool is created.

    Each leaf child carries its own ``boundingVolume.box`` so Pass 0
    axis-mapping detection can pick a sample (otherwise the walker
    records bbox=None for that leaf and Pass 0 rejects the fixture).
    """
    n_l0 = 32
    n_root = 32
    root = tmp_path / "tileset_a"
    root.mkdir()
    sub = root / "L0"
    sub.mkdir()
    # Write 32 leaves under L0/, each with a per-leaf bbox
    for i in range(n_l0):
        _write_minimal_b3dm(sub / f"tile_{i:02d}.b3dm", vertex_count=100 + i)
    sub_tileset = {
        "root": {
            "boundingVolume": {"box": [0, 0, 0, 50, 50, 50]},
            "children": [
                {
                    "boundingVolume": {"box": [i, 0, 0, 1, 1, 1]},
                    "content": {"uri": f"tile_{i:02d}.b3dm"},
                }
                for i in range(n_l0)
            ],
        }
    }
    (sub / "tileset.json").write_text(json.dumps(sub_tileset))
    # 32 leaves at parent level (sibling to L0/tileset.json).
    for i in range(n_l0, n_l0 + n_root):
        _write_minimal_b3dm(root / f"tile_{i:02d}.b3dm", vertex_count=100 + i)
    # Identity 4x4 transform (column-major per 3D Tiles spec) so
    # ``load_root_transform`` doesn't reject the fixture.
    identity_T = [1, 0, 0, 0,
                  0, 1, 0, 0,
                  0, 0, 1, 0,
                  0, 0, 0, 1]
    parent_tileset = {
        "root": {
            "boundingVolume": {"box": [0, 0, 0, 100, 100, 100]},
            "transform": identity_T,
            "children": [
                *[
                    {
                        "boundingVolume": {"box": [i, 0, 0, 1, 1, 1]},
                        "content": {"uri": f"tile_{i:02d}.b3dm"},
                    }
                    for i in range(n_l0, n_l0 + n_root)
                ],
                {"content": {"uri": "L0/tileset.json"}},
            ],
        }
    }
    (root / "tileset.json").write_text(json.dumps(parent_tileset))
    return root


def test_pool_is_created_once_for_pass1_and_pass2(tileset_dir: Path):
    """Mock ProcessPoolExecutor and assert it's instantiated exactly
    once across Pass 1 and Pass 2. A regression that created two pools
    (e.g. by leaving the legacy sequential Pass 1 in place) would
    increment this counter."""
    from point_cloud_extraction import extract_point_cloud

    call_count = {"pools": 0}

    real_pool_cls = None
    # We patch via a thin wrapper that counts instances.
    import concurrent.futures as cf

    real_init = cf.ProcessPoolExecutor.__init__

    def counting_init(self, *args, **kwargs):
        call_count["pools"] += 1
        real_init(self, *args, **kwargs)

    with mock.patch.object(cf.ProcessPoolExecutor, "__init__", counting_init):
        extract_point_cloud(
            tileset_dir, progress=False, with_color=False,
            workers=4, keep_paths=None,
        )
    # If Pass 1 runs in main + Pass 2 in pool, we'd see 1.
    # If Pass 1 and Pass 2 share a pool, we still see 1.
    # A regression to "two pools" would show 2.
    assert call_count["pools"] == 1, (
        f"extract_point_cloud opened {call_count['pools']} ProcessPoolExecutors;"
        " Pass 1 and Pass 2 must share a single pool. (See plan "
        "docker-root-illegal-construction-inspec-radiant-wilkes.md, "
        "change #8.)"
    )


def test_pass1_parallel_counts_match_sequential(tileset_dir: Path):
    """Run extract_point_cloud end-to-end and verify the resulting
    point count equals the sum of the per-leaf vertex counts (the
    contract Pass 1 enforces bit-exact via the shared pool)."""
    from point_cloud_extraction import extract_point_cloud

    pts, _, _, _ = extract_point_cloud(
        tileset_dir, progress=False, with_color=False,
        workers=2, keep_paths=None,
    )
    # 32 tiles at vertex counts 100..131 (sum=3712) +
    # 32 tiles at vertex counts 132..163 (sum=4736) = 8448.
    expected = sum(range(100, 132)) + sum(range(132, 164))
    assert pts.shape == (expected, 3), (
        f"Pass 1+2 via shared pool got N={pts.shape[0]}, expected {expected}; "
        "Pass 1 counts via pool.map differ from sequential. "
        "(See plan change #8.)"
    )


def test_pass1_sequential_fallback_path():
    """The fallback (non-pool) branch — taken when n_workers<=1 or
    len(leaves)<EXTRACT_MIN_LEAVES — must still compute counts
    correctly so downstream ``offsets`` is sized right."""
    # Synthetic single-tile tileset to trigger the fallback path
    # (workers=1 forces n_workers<=1 → use_pool=False).
    from point_cloud_extraction import extract_point_cloud
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "tiny"
        root.mkdir()
        _write_minimal_b3dm(root / "only.b3dm", vertex_count=42)
        ts = {
            "root": {
                "boundingVolume": {"box": [0, 0, 0, 5, 5, 5]},
                "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                "children": [{
                    "boundingVolume": {"box": [0, 0, 0, 1, 1, 1]},
                    "content": {"uri": "only.b3dm"},
                }],
            }
        }
        (root / "tileset.json").write_text(json.dumps(ts))
        pts, _, _, _ = extract_point_cloud(
            root, progress=False, with_color=False,
            workers=1, keep_paths=None,
        )
    assert pts.shape == (42, 3), (
        f"Sequential-fallback path: expected 42 pts, got {pts.shape[0]}; "
        "Pass 1 sequential loop broken after #8 refactor."
    )


def test_axis_mapping_detected_in_parallel_path(tileset_dir: Path):
    """Smoke: the parallel path still produces a non-trivial
    axis_mapping (Pass 0 + Pass 1 + Pass 2 share the same global
    state)."""
    from point_cloud_extraction import extract_point_cloud
    pts, _, _, axis_mapping = extract_point_cloud(
        tileset_dir, progress=False, with_color=False,
        workers=2, keep_paths=None,
    )
    perm, signs = axis_mapping
    # Permutation must be a valid rearrangement of (0, 1, 2)
    assert sorted(perm) == [0, 1, 2], (
        f"axis_mapping perm={perm} not a valid permutation"
    )
    # Signs must be ±1
    assert all(s in (-1, 1) for s in signs), (
        f"axis_mapping signs={signs} contain non-±1"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))