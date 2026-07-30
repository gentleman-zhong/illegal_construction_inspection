"""Extract POSITION (+ optional RGB) from every leaf b3dm of a 3D Tiles tileset.

Walks the tileset JSON tree, parses every leaf .b3dm, and emits the
POSITION vertices as a single (N, 3) float32 array in the local-ENU
frame (East, North, Up in metres, origin = model centroid). When
``--with-color`` is set (default), also emits per-vertex RGB sampled
from each b3dm's JPEG ``baseColorTexture`` (verified across L20-L23:
this codebase's b3dms carry no ``COLOR_0`` attribute — color is
baked into the texture, so we sample at every vertex's UV with the
standard glTF V-flip).

The b3dm POSITION is in some glTF local frame that depends on how the
tileset was generated (the ``asset.gltfUpAxis`` field tells us the up
axis). ``tileset.json``'s root transform always assumes ENU input (its
3 rotation columns match the standard (East, North, Up) basis at the
model centroid), so we need to convert each b3dm POSITION from its
source frame to ENU.

Two known source conventions from osgb2tiles3 / CesiumLab:

1. **Y-up with z=South** (wuxi_260205): b3dm = (E, U, S). To reach
   ENU: permute (X, Y, Z) → (X, Z, Y) and negate the new y column.
   Verified across 6 tiles: ``bbox Y center ≈ -b3dm z center``.

2. **Z-up** (jiujiang_2502, ``asset.gltfUpAxis = "Z"``): b3dm is already
   in (E, N, U) — no transform needed.

Rather than hard-coding one convention, we auto-detect the mapping by
sampling a few leaf b3dms, reading the corresponding leaf bbox centers
in tileset.json, and finding the signed-permutation matrix that best
aligns b3dm POSITION centroids to bbox centers. This produces the
correct ENU output for any convention emitted by osgb2tiles3.

Outputs (under <out_dir>/):
  - points.ply        ASCII PLY of the ENU points (x=East, y=North, z=Up).
                      With --with-color (default) also has
                      ``property uchar red / green / blue`` columns
                      sampled from each b3dm's baseColorTexture.
  - transform.json    {"transform": [16 floats]}  — the column-major 4×4
                      root matrix copied verbatim from tileset.json.
                      Converts ENU back to ECEF:
                          ecef_h = transform @ [enu_xyz; 1]
                      Also carries ``axis_mapping`` and ``with_color``.

CLI (single tileset):
    python extract_leaf_vertices.py <tileset_root> -o <out_dir> [--no-color]

CLI (dual tileset, both PLYs unified into B's local ENU):
    python extract_leaf_vertices.py --compare <tileset_a> <tileset_b> -o <out_dir> [--no-color]

    Inputs are taken in order:
        tileset_a = reference model
        tileset_b = comparison model (its local ENU is the canonical frame)
    Outputs under <out_dir>/ for --compare mode:
        points_a.ply     A's POSITION (+ A's RGB), transformed into B's ENU
                         (RGB is invariant under the rigid transform)
        points_b.ply     B's POSITION (+ B's RGB), verbatim (already in B's ENU)
        transform.json   {"transform": [16 floats]} — B's root transform
                         (column-major 4×4, B's ENU → ECEF). Plus
                         axis_mapping_a / axis_mapping_b / source_a /
                         source_b / with_color for traceability.
"""
from __future__ import annotations

import argparse
import io
import json
import multiprocessing
import os
import struct
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from algo_config import (      # noqa: E402
    EXTRACT_CHUNK_DIVISOR,
    EXTRACT_DETECT_SAMPLES,
    EXTRACT_MAX_WORKERS,
    EXTRACT_MIN_LEAVES,
)

try:
    from tqdm import tqdm
    _HAVE_TQDM = True
except ImportError:  # pragma: no cover
    _HAVE_TQDM = False


# ---------------------------------------------------------------------------
# glTF / b3dm binary parsing
# ---------------------------------------------------------------------------
_COMPONENT_INFO: dict[int, tuple[int, np.dtype]] = {
    5120: (1, np.dtype("<i1")),
    5121: (1, np.dtype("<u1")),
    5122: (2, np.dtype("<i2")),
    5123: (4, np.dtype("<u4")),
    5125: (4, np.dtype("<u4")),
    5126: (4, np.dtype("<f4")),
}
_NUM_COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


@dataclass
class B3dm:
    gltf: dict
    bin_payload: bytes


def _align8(n: int) -> int:
    return n if n == 0 else ((n + 7) // 8) * 8


def _b3dm_position_count(path: Path) -> int:
    """Read only the b3dm header + GLB JSON chunk; skip the BIN chunk.

    Used by Pass-1 of :func:`extract_point_cloud` to count vertices per
    tile without paying the cost of fully parsing the b3dm (which reads
    the entire file into memory to extract the BIN payload). Skips the
    BIN chunk entirely by seeking past it after reading the GLB JSON.
    """
    with path.open("rb") as f:
        head = f.read(28)
        if head[:4] != b"b3dm":
            raise ValueError(f"{path}: not a b3dm file (magic={head[:4]!r})")
        ftj, ftb, btj, _ = struct.unpack_from("<IIII", head, 12)
        cursor = 28
        ftj_end = cursor + _align8(ftj)
        ftb_end = ftj_end + _align8(ftb)
        btj_end = ftb_end + _align8(btj)
        btb_end = btj_end + _align8(btj)
        f.seek(btb_end)
        # GLB container: 4-byte magic + 4-byte version + 4-byte length
        # followed by chunks, each: 4-byte length + 4-byte type + data
        if f.read(4) != b"glTF":
            raise ValueError(f"{path}: expected GLB magic")
        f.read(4)  # GLB version (uint32)
        f.read(4)  # GLB total length (uint32)
        json_chunk_len = struct.unpack("<I", f.read(4))[0]
        if f.read(4) != b"JSON":
            raise ValueError(f"{path}: first GLB chunk is not JSON")
        gltf = json.loads(f.read(json_chunk_len))
        # Walk meshes → first primitive with POSITION attribute
        for mesh in gltf.get("meshes", []):
            for prim in mesh.get("primitives", []):
                attrs = prim.get("attributes", {})
                if "POSITION" in attrs:
                    return int(gltf["accessors"][attrs["POSITION"]]["count"])
        return 0


# Public alias for the same function — external callers (e.g. the
# service-layer pre-flight estimator in api_server.py) need to count
# b3dm vertex totals without paying the cost of the full parse. The
# leading underscore stays on the original for backward compat with any
# in-tree caller that imports the private name.
b3dm_position_count = _b3dm_position_count


def parse_b3dm(path: Path) -> B3dm:
    """Parse a .b3dm file: 28-byte header, feature/batch tables, embedded GLB."""
    data = path.read_bytes()
    if data[:4] != b"b3dm":
        raise ValueError(f"{path}: not a b3dm file (magic={data[:4]!r})")
    if struct.unpack_from("<I", data, 4)[0] != 1:
        warnings.warn(f"{path}: b3dm version != 1")

    ftj, ftb, btj, btb = struct.unpack_from("<IIII", data, 12)
    cursor = 28
    ftj_end = cursor + _align8(ftj)
    ftb_end = ftj_end + _align8(ftb)
    btj_end = ftb_end + _align8(btj)
    btb_end = btj_end + _align8(btb)

    ft = json.loads(data[cursor:ftj_end]) if ftj else {}
    bt = json.loads(data[ftb_end:btj_end]) if btj else {}

    glb_off = btb_end
    if data[glb_off:glb_off + 4] != b"glTF":
        raise ValueError(f"{path}: expected GLB magic at offset {glb_off}")

    json_chunk_len = struct.unpack_from("<I", data, glb_off + 12)[0]
    if data[glb_off + 16:glb_off + 20] != b"JSON":
        raise ValueError(f"{path}: first GLB chunk is not JSON")
    gltf = json.loads(data[glb_off + 20:glb_off + 20 + json_chunk_len])

    bin_off = glb_off + 20 + _align8(json_chunk_len)
    bin_chunk_len = struct.unpack_from("<I", data, bin_off)[0]
    if data[bin_off + 4:bin_off + 8] != b"BIN\x00":
        raise ValueError(f"{path}: second GLB chunk is not BIN")
    bin_payload = data[bin_off + 8:bin_off + 8 + bin_chunk_len]

    return B3dm(gltf=gltf, bin_payload=bin_payload)


def read_accessor(gltf: dict, bin_payload: bytes, idx: int) -> np.ndarray:
    """Materialise accessor `idx` as a writable (count, n_components) array."""
    acc = gltf["accessors"][idx]
    bv = gltf["bufferViews"][acc["bufferView"]]
    itemsize, dtype = _COMPONENT_INFO[acc["componentType"]]
    n_comp = _NUM_COMPONENTS[acc["type"]]
    count = acc["count"]

    bv_off = bv.get("byteOffset", 0)
    acc_off = acc.get("byteOffset", 0)
    stride = bv.get("byteStride")
    base = bv_off + acc_off

    if stride is None or stride == itemsize * n_comp:
        raw = np.frombuffer(bin_payload, dtype=dtype, count=count * n_comp, offset=base)
        return np.array(raw.reshape(count, n_comp), copy=True)

    n_per_row = stride // itemsize
    raw = np.frombuffer(bin_payload, dtype=dtype, count=count * n_per_row, offset=base)
    return np.array(raw.reshape(count, n_per_row)[:, :n_comp], copy=True)


# ---------------------------------------------------------------------------
# Tileset tree walking + root transform
# ---------------------------------------------------------------------------
def find_leaf_b3dms(tileset_root: Path) -> list[Path]:
    """Discover every leaf .b3dm by walking the tileset JSON tree."""
    return [p for p, _, _ in find_leaf_b3dms_with_bbox(tileset_root)]


def find_leaf_b3dms_with_bbox(tileset_root: Path
                              ) -> list[tuple[Path, list[float] | None, list[float] | None]]:
    """Discover every leaf .b3dm and capture its bbox center + half-axes.

    Returns a list of ``(b3dm_path, bbox_center, bbox_extents)`` tuples
    sorted by path. ``bbox_center`` is the ``[cx, cy, cz]`` slice of the
    leaf node's ``boundingVolume.box`` (in the tile-local frame) and
    ``bbox_extents`` is ``[hx, hy, hz]`` (the half-axis lengths from
    ``box[3:6]``). Either may be ``None`` if the leaf has no
    box-format boundingVolume (e.g. only a ``region`` volume is
    provided — uncommon for osgb2tiles3 output).

    The center + extents together form an axis-aligned bbox in B's local
    frame, used by ``stage_extract`` to pre-filter leaves against the
    ROI polygon (R1 in the v0.8-regression plan) without parsing
    individual b3dm POSITION arrays.
    """
    out: list[tuple[Path, list[float] | None, list[float] | None]] = []
    seen: set[Path] = set()
    ts_path = tileset_root / "tileset.json"
    if not ts_path.is_file():
        raise RuntimeError(f"no tileset.json at {ts_path}")
    _walk_tree(json.loads(ts_path.read_text())["root"], ts_path.parent, out, seen)
    out.sort(key=lambda t: str(t[0]))
    return out


def _bbox_center(node: dict) -> list[float] | None:
    bv = node.get("boundingVolume", {})
    box = bv.get("box")
    if isinstance(box, list) and len(box) >= 3:
        return [float(box[0]), float(box[1]), float(box[2])]
    return None


def _bbox_extents(node: dict) -> list[float] | None:
    """Half-axis lengths from ``boundingVolume.box[3:6]``.

    The 3D-Tiles ``box`` is ``[cx, cy, cz, ex, ey, ez, ...]`` where the
    six remaining slots encode an *oriented* bbox via three half-axes.
    For most osgb2tiles3 outputs the orientation is identity (i.e. the
    box is axis-aligned), so the half-axes read as plain (hx, hy, hz).
    A few tilesets carry mild rotation — we keep the conservative
    XY-only check downstream (z extent is unused for the ROI polygon
    containment which is 2D in B's local ENU).
    """
    bv = node.get("boundingVolume", {})
    box = bv.get("box")
    if isinstance(box, list) and len(box) >= 6:
        return [float(box[3]), float(box[4]), float(box[5])]
    return None


def _walk_tree(node: dict, ref_dir: Path,
               out: list[tuple[Path, list[float] | None, list[float] | None]],
               seen: set[Path]) -> None:
    content = node.get("content")
    children = node.get("children", [])

    if content is None:
        for c in children:
            _walk_tree(c, ref_dir, out, seen)
        return

    uri = content.get("uri", "")
    if uri.endswith(".json"):
        sub_path = (ref_dir / uri).resolve()
        try:
            sub = json.loads(sub_path.read_text())
        except FileNotFoundError:
            warnings.warn(f"missing sub-tileset: {sub_path}")
            return
        sub_root = sub["root"]
        for c in sub_root.get("children", []):
            _walk_tree(c, sub_path.parent, out, seen)
        sub_content = sub_root.get("content")
        if sub_content is not None and not sub_root.get("children"):
            uri2 = sub_content.get("uri", "")
            if uri2.endswith(".b3dm"):
                _try_record(sub_path.parent / uri2,
                            _bbox_center(sub_root),
                            _bbox_extents(sub_root),
                            out, seen)
    elif uri.endswith(".b3dm"):
        if children:
            for c in children:
                _walk_tree(c, ref_dir, out, seen)
        else:
            _try_record(ref_dir / uri,
                        _bbox_center(node),
                        _bbox_extents(node),
                        out, seen)
    else:
        warnings.warn(f"unsupported content.uri extension: {uri!r}")
        for c in children:
            _walk_tree(c, ref_dir, out, seen)


def _try_record(p: Path,
               bbox_center: list[float] | None,
               bbox_extents: list[float] | None,
               out: list[tuple[Path, list[float] | None, list[float] | None]],
               seen: set[Path]) -> None:
    rp = p.resolve()
    if rp in seen:
        return
    if rp.is_file():
        seen.add(rp)
        out.append((rp, bbox_center, bbox_extents))
    else:
        warnings.warn(f"missing leaf b3dm: {rp}")


def load_root_transform(tileset_root: Path) -> list[float]:
    """Return the 16-element column-major flat array from tileset.json verbatim."""
    ts = json.loads((tileset_root / "tileset.json").read_text())
    t = ts.get("root", {}).get("transform")
    if t is None:
        raise RuntimeError(f"{tileset_root}/tileset.json has no root.transform")
    return [float(x) for x in t]


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------
def _first_primitive(gltf: dict) -> dict:
    return gltf["meshes"][0]["primitives"][0]


def _detect_axis_mapping(
    samples: list[tuple[np.ndarray, list[float]]],
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Find the best signed-permutation mapping from b3dm POSITION to bbox frame.

    For each candidate ``(perm, signs)`` in the 6 × 8 = 48 possibilities,
    compute the residual ``sum_i ||bbox_i - signs * b3dm_i[perm]||²``.
    Return the (perm, signs) that minimises it.

    Permutation convention: ``perm[k]`` is the source b3dm axis used for
    output axis ``k`` (0 = x/East, 1 = y/North, 2 = z/Up). ``signs[k]``
    is +1 or -1.

    Returns ``(perm, signs)``.
    """
    from itertools import permutations, product
    best: tuple | None = None
    best_err = float("inf")
    for perm in permutations(range(3)):
        for signs in product((-1, 1), repeat=3):
            err = 0.0
            count = 0
            for pos_arr, bbox in samples:
                if bbox is None:
                    continue
                c = pos_arr.mean(axis=0)
                for k in range(3):
                    pred_k = signs[k] * c[perm[k]]
                    err += (bbox[k] - pred_k) ** 2
                count += 1
            if count == 0:
                continue
            if err < best_err:
                best_err = err
                best = (perm, signs)
    if best is None:  # pragma: no cover
        raise RuntimeError("axis-mapping detection: no usable samples")
    return best


def _apply_axis_mapping(
    pos: np.ndarray,
    perm: tuple[int, int, int],
    signs: tuple[int, int, int],
) -> np.ndarray:
    """Apply a (perm, signs) mapping to a (N, 3) POSITION array.

    Output axis ``k`` is ``signs[k] * pos[:, perm[k]]``.
    """
    out = np.empty_like(pos)
    for k in range(3):
        out[:, k] = signs[k] * pos[:, perm[k]]
    return out


# ---------------------------------------------------------------------------
# Per-tile extraction worker (designed for multiprocessing.Pool.imap)
# ---------------------------------------------------------------------------
def _extract_one_tile_worker(args):
    """Read one b3dm, return its axis-mapped POSITION and (optionally) RGB.

    Module-level so the function reference is picklable to child
    processes via ``fork``. Called with a single ``args`` tuple so the
    whole job can be dispatched atomically through
    ``multiprocessing.Pool.imap`` (preserves input ordering and lets
    the main process stream results chunk-by-chunk).

    Parameters
    ----------
    args : tuple
        ``(path, start, end, axis_mapping, with_color)`` where
        ``start`` / ``end`` are the slot in the big pre-allocated
        output arrays this tile owns (used purely for shape checking
        against the b3dm's vertex_count), ``axis_mapping`` is the
        ``(perm, signs)`` tuple from Pass-0 detection, ``with_color``
        is a bool — when False we skip texture sampling entirely.

    Returns
    -------
    pos : (end - start, 3) float32
        Axis-mapped POSITION in ENU.
    tile_colors : (end - start, 3) uint8 or None
        Per-vertex RGB (black-filled when ``with_color=False`` or when
        the b3dm has no reachable texture). ``None`` when
        ``with_color=False``.
    missing_path : str or None
        The b3dm path when this tile had no ``TEXCOORD_0`` attribute
        and we fell back to black; ``None`` otherwise. Lets the main
        process print the "first tile without texture" warning
        exactly once at the end, matching the original serial code.
    """
    path, start, end, axis_mapping, with_color = args
    b = parse_b3dm(path)
    prim = _first_primitive(b.gltf)
    pos_idx = prim["attributes"]["POSITION"]
    pos = read_accessor(b.gltf, b.bin_payload, pos_idx).astype(
        np.float32, copy=False
    )
    perm, signs = axis_mapping
    pos = _apply_axis_mapping(pos, perm, signs)
    if pos.shape != (end - start, 3):
        raise RuntimeError(
            f"{path}: expected ({end - start}, 3), got {pos.shape}"
        )
    if not with_color:
        return pos, None, None
    if "TEXCOORD_0" in prim["attributes"]:
        return pos, extract_vertex_colors(b.gltf, b.bin_payload, prim), None
    return pos, np.zeros((end - start, 3), dtype=np.uint8), path


def extract_point_cloud(
    tileset_root: str | Path,
    *,
    progress: bool = True,
    detect_samples: int = 8,
    with_color: bool = True,
    workers: int = 0,
    keep_paths: list[Path] | None = None,
) -> tuple[np.ndarray, np.ndarray | None, list[float],
           tuple[tuple[int, int, int], tuple[int, int, int]]]:
    """Walk the tileset tree, extract ENU POSITION (+ optional COLOR) from every leaf b3dm.

    The b3dm POSITION may be in any glTF local frame (Y-up or Z-up,
    with or without South z-axis). We auto-detect the correct mapping to
    ENU by sampling ``detect_samples`` leaf b3dms, reading their bbox
    centers from tileset.json, and finding the signed-permutation matrix
    that best aligns b3dm POSITION centroids to bbox centers. The same
    mapping is then applied to every b3dm in the tileset.

    Per-vertex RGB is **not** stored as ``COLOR_0`` in this codebase's
    b3dms (verified across L20-L23): color is baked into a JPEG
    ``baseColorTexture`` referenced from
    ``material.pbrMetallicRoughness.baseColorTexture``. When
    ``with_color`` is True, this function samples that JPEG at each
    vertex's UV (with the standard glTF V-flip) to produce a parallel
    ``(N, 3) uint8`` array. B3dms with no reachable texture fall back
    to black for that tile.

    Parameters
    ----------
    tileset_root : str | Path
    progress : bool
        Show tqdm progress bars while counting / extracting.
    detect_samples : int
        Number of leaves to sample for axis-mapping detection.
    with_color : bool
        If False, skip texture sampling entirely and return
        ``colors=None`` (saves ~30-50% wall time on wuxi-scale tilesets).
    workers : int
        ``0`` (default) auto-picks ``min(os.cpu_count() or 1, 8)``.
        Pass ``1`` to force the serial path. Effective parallelism is
        bounded by the leaf count: when fewer than 30 leaves the
        function falls back to serial regardless of ``workers`` to
        avoid fork-overhead dominating the wall time. The pool uses
        ``fork`` so workers inherit the parent's already-imported
        modules (PIL, numpy, plyfile) — Linux only.
    keep_paths : list[Path] | None
        If supplied, skip the JSON-tree walk and only extract from
        these paths (in that order). Used by ``stage_extract`` after
        the ROI polygon pre-filter to skip leaves whose bbox lies
        entirely outside the ROI (R1 in the v0.8 regression fix).

    Returns
    -------
    points : np.ndarray of shape (N, 3), dtype float32
        ENU coordinates (East, North, Up in metres) relative to the
        model centroid on the WGS84 surface. Concatenated across all
        leaf tiles in ``find_leaf_b3dms`` order.
    colors : np.ndarray of shape (N, 3), dtype uint8, or ``None``
        Per-vertex RGB in ``[0, 255]``. ``None`` when ``with_color``
        is False. Each row aligns 1-to-1 with ``points``.
    transform : list of 16 floats
        The column-major 4×4 root transform copied verbatim from
        ``tileset_root/tileset.json``. Apply via
            ``ecef_h = transform_4x4 @ np.append(enu_xyz, 1.0)``
        (reshape ``transform`` to (4, 4) with ``order='F'`` first).
    axis_mapping : ((perm), (signs)) tuple
        The auto-detected mapping applied to the points. ``perm[k]`` is
        the source b3dm axis for output axis ``k``; ``signs[k]`` is ±1.
    """
    root = Path(tileset_root)
    if keep_paths is not None:
        # The caller has already done the bbox-vs-ROI filter; build a
        # synthetic leaves list using the same 3-tuple shape as
        # ``find_leaf_b3dms_with_bbox``. Without a bbox Pass 0 cannot
        # choose axis-mapping samples, so we re-run a lightweight bbox
        # discovery on the kept paths to recover the centres.
        if not keep_paths:
            raise RuntimeError(
                f"keep_paths is empty after ROI bbox pre-filter under {root}; "
                "check ROI polygon vs tileset extent"
            )
        # Look up bboxes in batch via re-walking the tileset (cheap,
        # ~few hundred ms total for 10k leaves). Could be optimized
        # later by passing the bboxes in directly.
        bboxes: dict[Path, list[float] | None] = {
            p: bc for (p, bc, _bx) in find_leaf_b3dms_with_bbox(root)
        }
        leaves = [(p, bboxes.get(p), None) for p in keep_paths]
        # For leaves that fall outside the tileset's bbox index (e.g.
        # an external Path), drop gracefully to ``(p, None, None)`` —
        # Pass 0 will skip them with ``if bbox is None: continue``.
        leaves = [
            (p, bc, be) if bc is not None else (p, None, None)
            for (p, bc, be) in leaves
        ]
    else:
        leaves = find_leaf_b3dms_with_bbox(root)
        if not leaves:
            raise RuntimeError(f"No leaf b3dms found under {root}")

    transform = load_root_transform(root)

    bar = (lambda it, **kw: tqdm(it, **kw)) if (progress and _HAVE_TQDM) else (lambda it, **kw: it)

    # Pass 0: detect axis mapping on the first detect_samples leaves
    # that have a usable bbox. The leaves tuple shape is 3-tuple
    # ``(path, bbox_center_3, bbox_extents_3)``; we ignore the
    # extents here (axis-mapping only needs the center).
    samples: list[tuple[np.ndarray, list[float]]] = []
    for path, bbox, _bx in leaves:
        if bbox is None:
            continue
        b = parse_b3dm(path)
        pos_idx = _first_primitive(b.gltf)["attributes"]["POSITION"]
        pos = read_accessor(b.gltf, b.bin_payload, pos_idx).astype(np.float32, copy=False)
        samples.append((pos, bbox))
        if len(samples) >= EXTRACT_DETECT_SAMPLES:
            break
    if not samples:
        raise RuntimeError(
            f"Could not find any leaf b3dm with bbox box-format for axis-mapping "
            f"detection under {root}"
        )
    axis_mapping = _detect_axis_mapping(samples)
    perm, signs = axis_mapping
    pretty = ", ".join(
        f"out[{k}] = {signs[k]:+d} * b3dm[{perm[k]}]" for k in range(3)
    )
    print(f"[axis-map] detected: {pretty}", file=sys.stderr)

    # Pass 1: count (header-only — skip the BIN chunk entirely)
    counts = np.empty(len(leaves), dtype=np.int64)
    for i in bar(range(len(leaves)), total=len(leaves), desc="count  ", unit="tile"):
        path, _, _ = leaves[i]
        counts[i] = _b3dm_position_count(path)

    # Pass 2: extract POSITION (+ COLOR) + apply axis mapping
    total = int(counts.sum())
    points = np.empty((total, 3), dtype=np.float32)
    colors = np.zeros((total, 3), dtype=np.uint8) if with_color else None
    offsets = np.concatenate([[0], counts.cumsum()]).astype(np.int64)

    n_workers = workers if workers > 0 else min(os.cpu_count() or 1, EXTRACT_MAX_WORKERS)
    # Defensive fallback: if we are invoked from inside a daemon
    # process (e.g. a future caller that still uses multiprocessing.Pool
    # rather than ProcessPoolExecutor for fan-out), the inner pool
    # would crash with ``daemonic processes are not allowed to have
    # children``. The current call site ``stage_extract`` already uses
    # ProcessPoolExecutor (non-daemon), so the assertion never fires,
    # but the check is cheap insurance against future regressions.
    use_pool = (
        n_workers > 1
        and len(leaves) >= EXTRACT_MIN_LEAVES
        and not multiprocessing.current_process().daemon
    )
    if progress and use_pool and _HAVE_TQDM:
        print(f"[extract] parallel pass-2: {n_workers} workers, "
              f"{len(leaves)} tiles", file=sys.stderr)

    missing_texture_warned = False
    if use_pool:
        tile_args = [
            (path, int(offsets[i]), int(offsets[i + 1]),
             axis_mapping, with_color)
            for i, (path, _, _) in enumerate(leaves)
        ]
        # Use ProcessPoolExecutor (instead of multiprocessing.Pool)
        # because its worker processes are non-daemonic — ``stage_extract``
        # in run_pipeline.py fans out across two tilesets via a
        # multiprocessing.Pool, and Pool workers are daemonic, which
        # forbids nested pool creation. ProcessPoolExecutor on Linux
        # still uses fork + copy-on-write + the parent's already-imported
        # PIL/numpy, so the per-tile startup cost is the same.
        # Bigger chunks amortise IPC; ~16 pulls per worker is the sweet
        # spot for tilesets of any reasonable size.
        chunksize = max(1, len(leaves) // (n_workers * EXTRACT_CHUNK_DIVISOR))
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            iterator = pool.map(
                _extract_one_tile_worker, tile_args,
                chunksize=chunksize,
            )
            for i, (pos, tile_colors, missing_path) in enumerate(
                bar(iterator, total=len(leaves),
                    desc="color  " if with_color else "extract",
                    unit="tile")
            ):
                start = int(offsets[i])
                end = int(offsets[i + 1])
                points[start:end] = pos
                if with_color:
                    if missing_path is not None and not missing_texture_warned:
                        print(
                            "[color] at least one tile (e.g. "
                            f"{missing_path}) has no TEXCOORD_0; "
                            "filling such tiles with black",
                            file=sys.stderr,
                        )
                        missing_texture_warned = True
                    if tile_colors is not None:
                        colors[start:end] = tile_colors
    else:
        for i in bar(range(len(leaves)), total=len(leaves),
                     desc="color  " if with_color else "extract",
                     unit="tile"):
            start = int(offsets[i])
            end = int(offsets[i + 1])
            path, _, _ = leaves[i]
            b = parse_b3dm(path)
            prim = _first_primitive(b.gltf)
            pos_idx = prim["attributes"]["POSITION"]
            pos = read_accessor(b.gltf, b.bin_payload, pos_idx).astype(np.float32, copy=False)
            pos = _apply_axis_mapping(pos, perm, signs)
            if pos.shape != (end - start, 3):
                raise RuntimeError(f"{path}: expected ({end - start}, 3), got {pos.shape}")
            points[start:end] = pos

            if with_color:
                if "TEXCOORD_0" in prim["attributes"]:
                    tile_colors = extract_vertex_colors(b.gltf, b.bin_payload, prim)
                else:
                    if not missing_texture_warned:
                        print(
                            f"[color] {path} has no TEXCOORD_0; filling that tile with black",
                            file=sys.stderr,
                        )
                        missing_texture_warned = True
                    tile_colors = np.zeros((end - start, 3), dtype=np.uint8)
                colors[start:end] = tile_colors

    return points, colors, transform, axis_mapping


# ---------------------------------------------------------------------------
# Vertex color extraction (sample b3dm baseColorTexture at each UV)
# ---------------------------------------------------------------------------
def _decode_image_bytes(gltf: dict, bin_payload: bytes, image: dict) -> bytes | None:
    """Return raw image bytes from a glTF ``image`` entry.

    Handles the bufferView-in-BIN case (universal for GLB-bundled b3dm
    emitted by osgb2tiles3 / CesiumLab) and the inline ``data:`` URI
    case (rare). Returns ``None`` for external URI references we can't
    resolve relative to the parent b3dm.
    """
    if "bufferView" in image:
        bv = gltf["bufferViews"][image["bufferView"]]
        off = bv.get("byteOffset", 0)
        return bin_payload[off:off + bv["byteLength"]]
    uri = image.get("uri", "")
    if uri.startswith("data:"):
        import base64
        return base64.b64decode(uri.split(",", 1)[1])
    return None


def extract_vertex_colors(
    gltf: dict,
    bin_payload: bytes,
    primitive: dict,
    *,
    fallback: tuple[int, int, int] = (0, 0, 0),
) -> np.ndarray:
    """Sample the primitive's ``baseColorTexture`` at every vertex's UV.

    Returns an ``(N, 3) uint8`` array. The JPEG is decoded once via PIL
    and the entire vertex set is sampled with a vectorised
    ``arr[py, px]`` lookup (microseconds). If no texture is reachable
    (no material / no ``baseColorTexture`` / external URI), returns
    ``np.tile(np.uint8(fallback), (N, 1))`` so the caller can still
    write a 6-column PLY.

    glTF's V axis goes up (V=0 at the bottom), while image V goes down
    (V=0 at the top); we flip V before sampling.
    """
    n = int(gltf["accessors"][primitive["attributes"]["POSITION"]]["count"])
    mat_idx = primitive.get("material")
    if mat_idx is None:
        return np.tile(np.uint8(fallback), (n, 1))

    mat = gltf["materials"][mat_idx]
    tex_ref = mat.get("pbrMetallicRoughness", {}).get("baseColorTexture")
    if tex_ref is None:
        return np.tile(np.uint8(fallback), (n, 1))

    img = gltf["images"][gltf["textures"][tex_ref["index"]]["source"]]
    jpeg_bytes = _decode_image_bytes(gltf, bin_payload, img)
    if jpeg_bytes is None:
        return np.tile(np.uint8(fallback), (n, 1))

    # Local imports — Pillow is heavy and only needed when with_color=True.
    from PIL import Image
    pil_img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    arr = np.asarray(pil_img)                       # (H, W, 3) uint8
    H, W, _ = arr.shape

    uv = read_accessor(gltf, bin_payload, primitive["attributes"]["TEXCOORD_0"])
    uv = np.asarray(uv, dtype=np.float64)
    px = np.clip((uv[:, 0] * (W - 1)).astype(np.int64), 0, W - 1)
    # NOTE: glTF 2.0 spec says V=0 at the bottom of the texture (so
    # ``py = (1 - v) * (H - 1)`` would be the "textbook" mapping).
    # However, the b3dms produced by osgb2tiles3 / CesiumLab (the only
    # source in this codebase, verified across 1859 files in wuxi_260205
    # + wuxi_251022) have the JPEG pre-flipped at packaging time:
    # ``uv.v == 0`` already corresponds to the top of the image, so
    # doing the textbook flip here double-flips and pins the majority
    # of vertex samples to a (typically all-black) top row of the
    # texture, which manifests as "extract colors come out black /
    # very dark". We therefore map V directly: V=0 -> py=0.
    py = np.clip((uv[:, 1] * (H - 1)).astype(np.int64), 0, H - 1)
    return arr[py, px].astype(np.uint8, copy=False)


# ---------------------------------------------------------------------------
# Pair extraction (A → B's local ENU)
# ---------------------------------------------------------------------------
def _write_xyz_ply(path: Path, points: np.ndarray) -> None:
    """Write an ASCII PLY with only float x/y/z properties.

    Matches the byte-level output format used by the single-tileset mode
    of this module: ASCII, ``property float x/y/z``, ``fmt="%.4f"`` per coord.
    The input is explicitly truncated to float32 before writing (no-op if
    already float32) so the on-disk precision is identical to the legacy
    inline writer.
    """
    with path.open("w") as f:
        f.write("ply\nformat ascii 1.0\n")
        f.write(f"element vertex {len(points)}\n")
        f.write("property float x\nproperty float y\nproperty float z\nend_header\n")
        np.savetxt(f, points.astype(np.float32, copy=False), fmt="%.4f", delimiter=" ")


def _write_xyzrgb_ply(path: Path, points: np.ndarray, colors: np.ndarray) -> None:
    """Write an ASCII PLY with float x/y/z + uchar red/green/blue.

    Same format conventions as :func:`_write_xyz_ply` (ASCII, float32
    coords, ``fmt="%.4f"``) plus three uchar color columns sampled from
    the b3dm baseColorTexture by :func:`extract_vertex_colors`. Color
    values are ``0-255``.
    """
    pts = points.astype(np.float32, copy=False)
    cols = colors.astype(np.uint8, copy=False)
    rows = np.column_stack([pts, cols])
    with path.open("w") as f:
        f.write("ply\nformat ascii 1.0\n")
        f.write(f"element vertex {len(pts)}\n")
        f.write("property float x\nproperty float y\nproperty float z\n")
        f.write("property uchar red\nproperty uchar green\nproperty uchar blue\n")
        f.write("end_header\n")
        np.savetxt(
            f,
            rows,
            fmt=["%.4f", "%.4f", "%.4f", "%d", "%d", "%d"],
            delimiter=" ",
        )


def extract_pair_aligned(
    tileset_a: str | Path,
    tileset_b: str | Path,
    out_dir:   str | Path,
    *,
    quiet: bool = False,
    with_color: bool = True,
) -> dict:
    """Extract POSITION (+ optional COLOR) from two tilesets and align them into B's local ENU.

    A's POSITION (in A's local ENU) is composed through ``inv(T_b) @ T_a``
    to land in B's local ENU. B's POSITION is written verbatim (it is
    already in B's ENU, which is the unified frame). Per-vertex RGB is
    invariant under rigid transforms, so A's colors are reused as-is for
    ``points_a.ply``; B's colors are written verbatim for ``points_b.ply``.

    Parameters
    ----------
    tileset_a, tileset_b : str | Path
        Two 3D Tiles roots. ``tileset_a`` is the reference model;
        ``tileset_b`` is the comparison model whose local ENU is used as
        the canonical frame for both output PLYs.
    out_dir : str | Path
        Directory to receive ``points_a.ply``, ``points_b.ply`` and a
        shared ``transform.json`` (B's root transform).
    quiet : bool
        If True, suppress tqdm progress bars while reading b3dms.
    with_color : bool
        Forwarded to :func:`extract_point_cloud`. If False, the output
        PLYs are xyz-only (no uchar red/green/blue properties).

    Returns
    -------
    dict with keys
        n_a, n_b          : int   point counts
        elapsed           : float seconds
        transform         : list[float] (16, column-major) — B's transform
        axis_mapping_a    : ((perm), (signs))
        axis_mapping_b    : ((perm), (signs))
        with_color        : bool
    """
    root_a, root_b, out = Path(tileset_a), Path(tileset_b), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    points_a, colors_a, transform_a, axis_mapping_a = extract_point_cloud(
        root_a, progress=not quiet, with_color=with_color
    )
    points_b, colors_b, transform_b, axis_mapping_b = extract_point_cloud(
        root_b, progress=not quiet, with_color=with_color
    )

    # Build 4×4 matrices from the column-major flat lists persisted by
    # tileset.json's root.transform (and re-emitted by this module).
    T_a = np.asarray(transform_a, dtype=np.float64).reshape(4, 4, order="F")
    T_b = np.asarray(transform_b, dtype=np.float64).reshape(4, 4, order="F")
    # Compose: A's ENU → ECEF (T_a) → B's ENU (inv(T_b)).
    T = np.linalg.inv(T_b) @ T_a

    # Apply to row vectors [x, y, z, 1] @ T.T, then drop the homogeneous column.
    points_a_h = np.hstack([
        points_a.astype(np.float64, copy=False),
        np.ones((len(points_a), 1), dtype=np.float64),
    ])
    points_a_in_b = (points_a_h @ T.T)[:, :3].astype(np.float32, copy=False)

    if with_color:
        _write_xyzrgb_ply(out / "points_a.ply", points_a_in_b, colors_a)
        _write_xyzrgb_ply(out / "points_b.ply", points_b,       colors_b)
    else:
        _write_xyz_ply(out / "points_a.ply", points_a_in_b)
        _write_xyz_ply(out / "points_b.ply", points_b)

    (out / "transform.json").write_text(
        json.dumps(
            {
                "transform":      transform_b,
                "axis_mapping_a": [list(axis_mapping_a[0]), list(axis_mapping_a[1])],
                "axis_mapping_b": [list(axis_mapping_b[0]), list(axis_mapping_b[1])],
                "source_a":       str(root_a),
                "source_b":       str(root_b),
                "with_color":     with_color,
            },
            indent=2,
        )
    )

    return {
        "n_a":            int(points_a.shape[0]),
        "n_b":            int(points_b.shape[0]),
        "elapsed":        time.time() - t0,
        "transform":      transform_b,
        "axis_mapping_a": axis_mapping_a,
        "axis_mapping_b": axis_mapping_b,
        "with_color":     with_color,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Dual-tileset aligned mode: --compare tileset_a tileset_b -o out_dir
    if argv and argv[0] == "--compare":
        argv = argv[1:]
        p = argparse.ArgumentParser(
            prog="extract_leaf_vertices.py --compare",
            description=(
                "Extract two tilesets and align them into B's local ENU. "
                "A is the reference model; B is the comparison model "
                "whose local ENU is the canonical frame for both outputs. "
                "By default the output PLYs include per-vertex RGB sampled "
                "from each b3dm's baseColorTexture; pass --no-color to "
                "emit xyz-only PLYs."
            ),
        )
        p.add_argument("tileset_a", type=Path)
        p.add_argument("tileset_b", type=Path)
        p.add_argument("-o", "--out-dir", type=Path, required=True)
        p.add_argument("-q", "--quiet", action="store_true")
        p.add_argument("--with-color", dest="with_color", action="store_true", default=True,
                       help="Sample each b3dm's baseColorTexture at every UV and emit "
                            "uchar red/green/blue in the output PLYs (default).")
        p.add_argument("--no-color", dest="with_color", action="store_false",
                       help="Skip texture sampling; emit xyz-only PLYs (faster, "
                            "matches the legacy output format).")
        a = p.parse_args(argv)
        result = extract_pair_aligned(
            a.tileset_a, a.tileset_b, a.out_dir,
            quiet=a.quiet, with_color=a.with_color,
        )
        color_msg = " (with RGB)" if result["with_color"] else " (xyz-only)"
        print(
            f"\nAligned pair -> {a.out_dir}{color_msg}\n"
            f"  points_a.ply:   {result['n_a']:,} pts "
            f"(A transformed into B's ENU)\n"
            f"  points_b.ply:   {result['n_b']:,} pts (B verbatim)\n"
            f"  transform.json: B's transform (B's ENU -> ECEF)\n"
            f"Elapsed: {result['elapsed']:.2f}s",
            file=sys.stderr,
        )
        return 0

    # Single-tileset mode (default).
    p = argparse.ArgumentParser(
        description="Extract ENU point cloud (+ optional RGB) + transform JSON from a 3D Tiles tileset.",
    )
    p.add_argument("tileset_root", type=Path)
    p.add_argument("-o", "--out-dir", type=Path, required=True)
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--with-color", dest="with_color", action="store_true", default=True,
                   help="Sample each b3dm's baseColorTexture at every UV and emit "
                        "uchar red/green/blue in points.ply (default).")
    p.add_argument("--no-color", dest="with_color", action="store_false",
                   help="Skip texture sampling; emit xyz-only points.ply "
                        "(faster, matches the legacy output format).")
    args = p.parse_args(argv)

    t0 = time.time()
    points, colors, transform, axis_mapping = extract_point_cloud(
        args.tileset_root, progress=not args.quiet, with_color=args.with_color
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)

    ply_path = args.out_dir / "points.ply"
    if colors is not None:
        _write_xyzrgb_ply(ply_path, points, colors)
    else:
        _write_xyz_ply(ply_path, points)

    (args.out_dir / "transform.json").write_text(
        json.dumps(
            {
                "transform":   transform,
                "axis_mapping": [list(axis_mapping[0]), list(axis_mapping[1])],
                "with_color":   colors is not None,
            },
            indent=2,
        )
    )

    color_msg = " (with RGB)" if colors is not None else " (xyz-only)"
    print(
        f"\nWrote {len(points):,} ENU points -> {ply_path}{color_msg}\n"
        f"Wrote transform (16 floats, column-major) -> {args.out_dir / 'transform.json'}\n"
        f"Elapsed: {time.time() - t0:.2f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
