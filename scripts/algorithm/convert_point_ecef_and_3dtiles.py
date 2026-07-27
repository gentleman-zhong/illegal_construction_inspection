"""Convert a PLY point cloud from local-ENU to ECEF, then generate 3D Tiles.
Intermediate PLY/LAS files are optional controlled by script args, only 3D Tiles will be retained finally.
Reads:
input PLY whose x/y/z are in local-ENU (East, North, Up in metres,
origin = model centroid on the WGS84 surface — same frame as
extract_leaf_vertices.py's output, i.e. after the b3dm Y-up → ENU Z-up
permutation; PLY x=East, y=North, z=Up).
transform.json ({"transform": [16 floats]} — the column-major 4×4
matrix copied verbatim from tileset.json's root.transform).
Writes:
1. Intermediate ECEF PLY (optional, controlled by --save-intermediate)
2. Intermediate LAS (optional, controlled by --save-intermediate)
3. Final 3D Tiles folder (always generated and kept)
4. instances.json (optional, controlled by --cluster) — one AABB per
   DBSCAN cluster, expressed in ECEF. Consumed by cesium_test.html to
   overlay box wireframes around each instance.
The per-point conversion is the homogeneous matmul
ecef_h = T @ [enu_xyz; 1]
which in row-vector batched form is
ecef = [enu, 1] @ T.T[:, :3]

Note: the per-axis range of the converted point cloud is NOT preserved
between ENU and ECEF (only the point-cloud "shape" is, because the
transform is a pure rotation + translation). For example, an ENU cloud
with Z (Up) range 60 m and Y (North) range 300 m will have ECEF_Z range
≈ 0.5243·60 + 0.8515·300 ≈ 286 m at lat ≈ 31.6°, because the ECEF Z
axis projects onto both ENU Y and Z. This is geometrically correct, not
a bug.
Usage:
python convert_enu_ecef.py <input.ply> <transform.json> --tiles-out ./3dtiles_dir [--save-intermediate]
Example (only keep 3D Tiles, auto delete ply/las):
python convert_enu_ecef.py \
./out/points.ply \
./out/transform.json \
--tiles-out ./out/3dtiles_result

Example (retain intermediate PLY & LAS):
python convert_enu_ecef.py \
./out/points.ply \
./out/transform.json \
--tiles-out ./out/3dtiles_result \
--save-intermediate

Example (cluster and write instances.json for Cesium overlay):
python convert_enu_ecef.py \
./out/points.ply \
./out/transform.json \
--tiles-out ./out/3dtiles_result \
--cluster

When --cluster is set, DBSCAN noise points (label = -1) are filtered out
of the output 3D Tiles by default so the clustering also acts as a
point-cloud filter. Pass --keep-noise to retain the full point cloud and
emit bbox overlays only.

Example (cluster and also keep noise in the output):
python convert_enu_ecef.py \
./out/points.ply \
./out/transform.json \
--tiles-out ./out/3dtiles_result \
--cluster --keep-noise
"""
from __future__ import annotations
import json
import sys
import argparse
from pathlib import Path
import numpy as np
import open3d as o3d
import laspy
import subprocess
import shutil
from typing import Optional, Dict, Any
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from algo_config import (      # noqa: E402
    DBSCAN_EPS_M,
    DBSCAN_MIN_POINTS,
    DBSCAN_VOXEL_M,
    HULL_PARALLEL_MIN_N,
    LAS_SCALE_M,
)

# Supported scalar PLY property types. list properties are skipped.
_PLY_DTYPE: dict[str, tuple[str, np.dtype, int]] = {
    "float":  ("<f4", np.dtype("<f4"), 4),
    "double": ("<f8", np.dtype("<f8"), 8),
    "uchar":  ("u1",  np.dtype("u1"),  1),
    "char":   ("i1",  np.dtype("i1"),  1),
    "ushort": ("<u2", np.dtype("<u2"), 2),
    "short":  ("<i2", np.dtype("<i2"), 2),
    "int":    ("<i4", np.dtype("<i4"), 4),
    "uint":   ("<u4", np.dtype("<u4"), 4),
}


def _parse_ply_header(header: str) -> tuple[int, list[tuple[str, str, int, np.dtype]]]:
    """Parse a PLY ASCII header. Returns (n_vertices, props_list)."""
    n_vertices = 0
    props: list[tuple[str, str, int, np.dtype]] = []
    in_vertex_element = False
    for raw in header.splitlines():
        line = raw.strip()
        if not line or line.startswith(("comment", "obj_info")):
            continue
        if line.startswith("element"):
            _, name, count = line.split()
            in_vertex_element = (name == "vertex")
            if in_vertex_element:
                n_vertices = int(count)
            continue
        if line.startswith("property") and in_vertex_element:
            parts = line.split()
            if parts[1] == "list":
                continue  # list properties can't be re-emitted generically
            _, type_str, name = parts
            _str, dt, nbytes = _PLY_DTYPE[type_str]
            props.append((name, type_str, nbytes, dt))
    if not n_vertices or not props:
        raise ValueError("PLY header missing vertex element / properties")
    return n_vertices, props


def read_ply(path: Path) -> tuple[dict[str, np.ndarray],
                                  list[tuple[str, str, int, np.dtype]],
                                  str]:
    """Read an ASCII or binary_little_endian PLY.
    Returns ``(props_dict, props_list, format)`` where format is
    ``"ascii"`` or ``"binary_little_endian"``.
    """
    data = path.read_bytes()
    header_end = data.index(b"end_header\n") + len(b"end_header\n")
    header = data[:header_end].decode("ascii")
    n_vertices, props = _parse_ply_header(header)
    body = data[header_end:]
    n_props = len(props)
    stride = sum(p[2] for p in props)

    if "ascii" in header:
        values = body.decode("ascii").split()
        if len(values) != n_vertices * n_props:
            raise ValueError(
                f"{path}: ASCII body has {len(values)} values, "
                f"expected {n_vertices * n_props}"
            )
        arr = np.asarray(values, dtype=np.float64).reshape(n_vertices, n_props)
        out = {p[0]: arr[:, j].astype(p[3]) for j, p in enumerate(props)}
        return out, props, "ascii"

    if "binary_little_endian" in header:
        if len(body) != n_vertices * stride:
            raise ValueError(
                f"{path}: body size {len(body)} != {n_vertices} * stride {stride}"
            )
        dt = np.dtype([(p[0], p[3]) for p in props])
        rec = np.frombuffer(body, dtype=dt, count=n_vertices)
        out = {p[0]: np.array(rec[p[0]], copy=True) for p in props}
        return out, props, "binary_little_endian"

    raise ValueError(f"{path}: only ASCII or binary_little_endian PLY supported")


def write_ply(path: Path,
              props: dict[str, np.ndarray],
              prop_list: list[tuple[str, str, int, np.dtype]],
              fmt: str) -> None:
    """Write a PLY in the given format with the same property layout."""
    n = len(next(iter(props.values())))
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "binary_little_endian":
        with path.open("wb") as f:
            f.write(b"ply\n")
            f.write(b"format binary_little_endian 1.0\n")
            f.write(f"element vertex {n}\n".encode())
            for p in prop_list:
                f.write(f"property {p[1]} {p[0]}\n".encode())
            f.write(b"end_header\n")
            dt = np.dtype([(name, dt) for name, _, _, dt in prop_list])
            rec = np.empty(n, dtype=dt)
            for name in props:
                rec[name] = props[name]
            rec.tofile(f)
        return

    if fmt == "ascii":
        with path.open("w") as f:
            f.write("ply\n")
            f.write("format ascii 1.0\n")
            f.write(f"element vertex {n}\n")
            for p in prop_list:
                f.write(f"property {p[1]} {p[0]}\n")
            f.write("end_header\n")
            # Per-column format honours the declared property type so
            # downstream readers (e.g. RPly via o3d.io.read_point_cloud)
            # can parse the values back into the right dtype. Floats use
            # "%.4f"; integer types (uchar / char / ushort / short /
            # int / uint) use "%d" so uchar red is written as e.g. "181"
            # rather than the unparseable "181.0000".
            fmt_per_col: list[str] = []
            cols: list[np.ndarray] = []
            for p in prop_list:
                arr = np.asarray(props[p[0]])
                if np.issubdtype(arr.dtype, np.integer):
                    fmt_per_col.append("%d")
                    cols.append(arr.astype(np.int64, copy=False))
                else:
                    fmt_per_col.append("%.4f")
                    cols.append(arr.astype(np.float64, copy=False))
            np.savetxt(f, np.column_stack(cols), fmt=fmt_per_col, delimiter=" ")
        return

    raise ValueError(f"unknown format: {fmt}")


def save_ecef_ply_to_las(ply_path: str, las_path: str):
    """
    读取 ECEF 坐标系的 PLY 文件并保存为 LAS 文件
    """
    print(f"正在读取 PLY: {ply_path}")
    pcd = o3d.io.read_point_cloud(ply_path)

    xyz = np.asarray(pcd.points)
    rgb = np.asarray(pcd.colors)  # open3d 读取的颜色通常是 0-1 浮点数

    # 创建 LAS Header
    # ECEF 坐标数值非常大，必须正确设置 offset 和 scale 以保持精度
    header = laspy.LasHeader(point_format=7, version="1.4")
    header.offsets = np.floor(np.min(xyz, axis=0))
    header.scales = [LAS_SCALE_M] * 3  # millimetre-level precision

    las = laspy.LasData(header)
    las.x = xyz[:, 0]
    las.y = xyz[:, 1]
    las.z = xyz[:, 2]

    if rgb.size > 0:
        # 将 0-1 浮点数转换为 16位无符号整数 (LAS标准)
        las.red = (rgb[:, 0] * 65535).astype(np.uint16)
        las.green = (rgb[:, 1] * 65535).astype(np.uint16)
        las.blue = (rgb[:, 2] * 65535).astype(np.uint16)

    las.write(las_path)
    print(f"LAS 文件已保存至: {las_path}")


def save_ecef_arrays_to_las(xyz: np.ndarray, rgb: np.ndarray | None,
                            las_path: str) -> None:
    """Write ECEF numpy arrays directly to LAS without going through PLY.

    Same wire format as :func:`save_ecef_ply_to_las` (point_format=7,
    scale=0.001 mm, ECEF offsets), but skips the open3d PLY parse
    which is the slow part of that path on large point clouds.

    Parameters
    ----------
    xyz : (N, 3) float array
        ECEF coordinates in metres.
    rgb : (N, 3) uint8 array, or None
        Per-point RGB in 0-255. When None, the LAS carries no colour
        channels (point_format 7 still allows that — they're zero-filled).
    las_path : str
        Destination LAS path. Parent directory is created.
    """
    xyz = np.asarray(xyz, dtype=np.float64)
    if xyz.ndim != 2 or xyz.shape[1] != 3:
        raise ValueError(f"xyz must be (N, 3), got shape {xyz.shape}")

    header = laspy.LasHeader(point_format=7, version="1.4")
    header.offsets = np.floor(np.min(xyz, axis=0))
    header.scales = [LAS_SCALE_M] * 3  # millimetre-level precision

    las = laspy.LasData(header)
    las.x = xyz[:, 0]
    las.y = xyz[:, 1]
    las.z = xyz[:, 2]

    if rgb is not None and rgb.size > 0:
        if rgb.shape != xyz.shape:
            raise ValueError(
                f"rgb shape {rgb.shape} != xyz shape {xyz.shape}"
            )
        # uchar [0, 255] -> uint16 [0, 65535] via *257 (same rescale
        # filter_vegetation._normalize_colors_to_uint16 uses).
        # uint32 intermediate prevents 255 * 257 = 65535 from wrapping
        # the way it would in a uint16 multiply.
        r16 = rgb[:, 0].astype(np.uint32) * 257
        g16 = rgb[:, 1].astype(np.uint32) * 257
        b16 = rgb[:, 2].astype(np.uint32) * 257
        las.red = r16.astype(np.uint16)
        las.green = g16.astype(np.uint16)
        las.blue = b16.astype(np.uint16)

    Path(las_path).parent.mkdir(parents=True, exist_ok=True)
    las.write(las_path)


def convert_las_to_3dtiles(
        las_input: str,
        out_dir: str,
        py3dtiles_cmd: Optional[str] = None,
        overwrite: bool = True,
) -> Dict[str, Any]:
    """
    将 LAS 转换为 3D Tiles (ECEF -> ECEF)
    """
    las_path = Path(las_input)
    output_path = Path(out_dir)

    # 查找 py3dtiles 可执行文件
    py3dtiles = py3dtiles_cmd or shutil.which("py3dtiles")
    if not py3dtiles:
        raise RuntimeError("未找到 py3dtiles，请确保已安装并在 PATH 中。")

    # 关键参数：--srs_in 4978 (告诉 py3dtiles 输入是 ECEF);--srs_out 故意不传。
    # 传 --srs_out 4978 时 py3dtiles 8.0.2 走 ECEF 特殊分支,在
    # matrix_manipulation.py:20 上对 axis-aligned bbox 触发 0/0=NaN,
    # 污染 root.transform / boundingVolume / .pnts 全部坐标,
    # Cesium 加载后静默空白。不传 --srs_out 时,py3dtiles 不调用
    # make_rotation_matrix,直接以 raw ECEF 写出 root.transform=identity,
    # Cesium 加载正常。
    cmd = [
        py3dtiles, "convert",
        str(las_path),
        "--out", str(output_path),
        "--srs_in", "4978",
    ]

    if overwrite:
        cmd.append("--overwrite")

    print(f"执行命令: {' '.join(cmd)}")

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
        return {"ok": True, "out_dir": output_path, "message": proc.stdout}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "out_dir": None, "message": e.stdout}


def cluster_instances(
    xyz_enu: np.ndarray,
    ecef: np.ndarray,
    *,
    eps: float = DBSCAN_EPS_M,
    min_points: int = DBSCAN_MIN_POINTS,
    voxel_m: float = DBSCAN_VOXEL_M,
) -> tuple[list[dict], np.ndarray]:
    """DBSCAN-cluster the ENU point cloud and emit one AABB per cluster in ECEF.

    Clustering is run in the ENU frame (small numeric range, ``eps`` is
    intuitive in metres). For each cluster we emit:

    - ``bbox_min_ecef`` / ``bbox_max_ecef`` — raw ECEF corners of the
      cluster's point set (used to position the box in Cesium).
    - ``bbox_center_ecef`` — midpoint of the ECEF corners.
    - ``bbox_size`` — **ENU AABB delta** ``(east, north, up)`` in metres,
      NOT the ECEF corner-to-corner delta. Cesium's BoxGraphics with
      HPR(0,0,0) orientation renders the box aligned to the local-tangent
      (ENU) axes at the position, so dimensions must be in ENU metres.
      Using the ECEF delta here would oversize the box and skew it
      because ECEF axes are not parallel to ENU axes at non-zero latitude.
    - ``hull_vertices_ecef`` / ``hull_triangles`` — convex hull of the
      cluster's ECEF points (for tight visualization). ``vertices`` are
      an ``(N, 3)`` array of ECEF coordinates; ``triangles`` is an
      ``(M, 3)`` array of vertex indices into ``vertices``. Both lists
      can be empty when open3d cannot compute a hull (e.g. < 4
      non-coplanar points), in which case the consumer should fall back
      to ``bbox_*`` rendering.

    Cluster IDs are 1-based and skip noise (DBSCAN label = -1). Returned
    list is sorted by ``num_points`` descending, ties broken by the
    original DBSCAN label ascending (= stable, deterministic re-rank).

    **Memory note (2026-07):** open3d's C++ ``cluster_dbscan``
    materialises the entire ε-neighbour graph (~4 B/pt + 4 B/edge)
    before assigning labels. On dense tilesets (NN 0.05–0.1 m, the B
    tileset has 54.7 M points / 5.9 GB) this single call peaked at
    ~40 GiB RSS and triggered the cgroup OOM-killer. To keep Stage 4
    safely under the 64 GiB container limit, we voxel-decimate the
    input to one representative per ``voxel_m`` cube before
    ``cluster_dbscan``, then back-project the cluster labels to every
    original point via cKDTree.query(k=1). At ``voxel_m=0.5`` this
    drops B's cluster input from 10 M to ~50–80 k representatives
    (≈100× reduction), with end-result cluster centroids within
    1×voxel of the undecimated baseline. Pass ``voxel_m=0`` to disable
    decimation (legacy behaviour, intended only for very sparse clouds
    where it cannot OOM).

    Returns
    -------
    clusters : list[dict]
        Per-cluster metadata (see fields above). Excludes noise points.
    labels : np.ndarray of int64, shape (N,)
        Per-point DBSCAN label, same length as ``xyz_enu`` / ``ecef``.
        ``-1`` means noise; non-negative values are 0-based cluster ids
        matching the entries in ``clusters``. Use ``labels >= 0`` as a
        boolean mask to filter noise out of the original point cloud
        (e.g. to make clustering double as a filter on the output).
    """
    import open3d as o3d  # local import to keep top-level import set stable

    n_orig = len(xyz_enu)

    # ---- Voxel decimation (Stage 4 hot-spot fix) ----
    # Build a per-(int floor(x/v), floor(y/v), floor(z/v)) integer key
    # and keep one point per unique key (the first one in input order
    # after np.unique sort). This is the cheapest decimation that
    # preserves local density — the cKDTree back-projection below
    # guarantees every original point gets the cluster label of its
    # nearest representative, so cluster boundaries shift by at most
    # 1×voxel compared to running on the full cloud.
    # NOTE: do NOT force ``dtype=np.float64`` on these ``np.asarray`` calls.
    # The production path passes ``pts_diff`` from Stage 3, which is f32
    # (the f64 promotion was removed in the Stage 3 memory-optimisation
    # pass to save ~1.4 GiB peak RSS at N_f ≈ 50 M). Forcing f64 with
    # ``copy=False`` is rejected by NumPy ≥ 2.0 because the dtype change
    # would require a copy; without the ``copy=False`` flag NumPy 2.0
    # silently copies — also wasteful. Letting the dtype pass through
    # keeps f32 savings intact and is correct: open3d.Vector3dVector and
    # scipy.spatial.cKDTree both accept f32 (or f64) directly, and at
    # 0.5 m voxel / 3.0 m DBSCAN-eps the f32 precision (~1e-7 m) is
    # orders of magnitude finer than the smallest meaningful scale.
    if voxel_m and voxel_m > 0 and n_orig > 0:
        keys = np.floor(np.asarray(xyz_enu) / float(voxel_m)).astype(np.int64)
        _, first_idx = np.unique(keys, axis=0, return_index=True)
        sub_idx = np.sort(first_idx)
        sub_pts = np.asarray(xyz_enu[sub_idx])
    else:
        sub_pts = np.asarray(xyz_enu)

    n_sub = len(sub_pts)
    if n_sub != n_orig:
        print(
            f"[cluster] voxel decimation: {n_orig:,} -> {n_sub:,} "
            f"pts (voxel={voxel_m} m)",
            file=sys.stderr,
        )

    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(sub_pts)
    # open3d 0.19 returns a flat labels vector (length N_sub). Older
    # versions returned ``(clusters_list, labels)`` — keep defensive.
    result = pcd.cluster_dbscan(eps=eps, min_points=min_points)
    if isinstance(result, tuple):
        label_arr = result[-1]
    else:
        label_arr = result
    sub_labels = np.asarray(label_arr, dtype=np.int64)

    # ---- Back-project per-sub labels to per-original labels ----
    # cKDTree.query(k=1) is the smallest allocation that does an
    # exact nearest-neighbour lookup; it accepts whatever contiguous
    # dtype ``xyz_enu`` carries (f32 in the production path after the
    # Stage 3 f64-cast removal, f64 in unit tests). Forcing f64 here
    # would silently copy in NumPy ≥ 2.0, costing ~4 B/elt on the
    # back-projection array. The representative set is ~1% of the
    # original so the tree is tiny (~16 MiB) and the query is fast
    # (~120 MiB peak working set at N_f = 50 M with f32).
    if n_sub == n_orig:
        labels = sub_labels
    else:
        tree = cKDTree(sub_pts)
        nearest = tree.query(np.ascontiguousarray(xyz_enu), k=1, workers=-1)[1]
        labels = sub_labels[nearest]
        del tree, nearest

    unique_labels = sorted(int(x) for x in np.unique(labels) if x >= 0)
    n_clusters = len(unique_labels)

    # ---- Per-cluster bbox + convex hull (serial path) ----
    # The previous version ran this in a ThreadPoolExecutor (one worker
    # per cluster, bounded by CPU count). QuickHull inside open3d
    # already releases the GIL and uses OpenMP, so the outer-level
    # thread pool bought ~5× wall-time on the wuxi 136-cluster run.
    # But each parallel `ex.submit` immediately materialises
    # `ecef[labels == cid]` + `xyz_enu[labels == cid]` (two masked
    # copies of a 5–11 GiB mask storm across all in-flight workers)
    # which is what tipped Stage 4 over the 64 GiB limit on the B
    # tileset. With the voxel-decimated cluster input, mask-storm
    # pressure is gone too, but the conservative path here is still
    # serial: hull wall time is ≲ 5 s for typical cluster counts
    # (B = 50–200 clusters) and we don't need the parallelism any
    # more — the previous 5× speedup was masking a 5× memory hit.
    if n_clusters >= HULL_PARALLEL_MIN_N:
        # Legacy note kept for code archaeology: the old n_workers
        # formula and ThreadPoolExecutor path are intentionally removed
        # (see comment above); keep the constant import alive so the
        # knob is still tunable via env if we ever want to re-enable
        # a parallel path that *doesn't* materialise masked copies.
        pass  # serial path below
    clusters: list[dict] = []
    for cid_0based in unique_labels:
        # Build a single shared mask once, then slice the two
        # attribute arrays in place — single mask allocation, no
        # extra intermediate. The mask is the dominant per-cluster
        # allocation but it's one N-byte array per cluster rather
        # than N-byte * workers in flight.
        mask = labels == cid_0based
        _, info = _hull_one_cluster(cid_0based, ecef[mask], xyz_enu[mask])
        clusters.append(info)
        del mask
    # Re-rank so `id=1` is the cluster with the most points. Ties break
    # by the original DBSCAN label ascending (= current `id` field
    # before this re-rank) so output is stable and deterministic across
    # runs on the same input. Done in place; downstream
    # `write_instances_json` serializes the renumbered list verbatim.
    clusters.sort(key=lambda c: (-c["num_points"], c["id"]))
    for new_id, c in enumerate(clusters, start=1):
        c["id"] = new_id
    return clusters, labels


def _hull_one_cluster(
    cid_0based: int,
    pts_ecef: np.ndarray,
    pts_enu: np.ndarray,
) -> tuple[int, dict]:
    """Compute one cluster's bbox + Qhull convex hull. Module-level so
    ThreadPoolExecutor can pickle it (Python 3.9 lambda pickles fine
    but module-level is more explicit + faster pickling).

    Returns ``(cid_0based, cluster_info_dict)`` so the executor caller
    can preserve cid ordering even when results arrive out of order.
    """
    bbox_min = pts_ecef.min(axis=0)
    bbox_max = pts_ecef.max(axis=0)
    bbox_center = 0.5 * (bbox_min + bbox_max)
    # ENU AABB delta — matches Cesium's local-tangent-aligned box.
    bbox_size = pts_enu.max(axis=0) - pts_enu.min(axis=0)

    # Convex hull of the cluster, in ECEF (directly consumable by
    # Cesium without any local→world conversion). QuickHull via
    # open3d; Qhull handles thousands of points in milliseconds.
    pcd_cluster = o3d.geometry.PointCloud()
    pcd_cluster.points = o3d.utility.Vector3dVector(
        pts_ecef.astype(np.float64, copy=False)
    )
    hull_result = pcd_cluster.compute_convex_hull()
    # open3d 0.19+: returns (TriangleMesh, vertex_indices);
    # older versions return TriangleMesh. Be defensive.
    if isinstance(hull_result, tuple):
        hull_mesh = hull_result[0]
    else:
        hull_mesh = hull_result
    hull_vertices = np.asarray(hull_mesh.vertices, dtype=np.float64)
    hull_triangles = np.asarray(hull_mesh.triangles, dtype=np.int64)

    return cid_0based, {
        "id": cid_0based + 1,  # 1-based for the user-facing JSON
        "num_points": int(len(pts_ecef)),
        "bbox_min_ecef": bbox_min.tolist(),
        "bbox_max_ecef": bbox_max.tolist(),
        "bbox_center_ecef": bbox_center.tolist(),
        "bbox_size": bbox_size.tolist(),
        "hull_vertices_ecef": hull_vertices.tolist(),
        "hull_triangles": hull_triangles.tolist(),
    }


def write_instances_json(
    path: Path,
    clusters: list[dict],
    *,
    eps: float,
    min_points: int,
    n_input_points: int,
) -> None:
    """Emit the Cesium-facing instances.json next to the 3D Tiles."""
    payload = {
        "eps": eps,
        "min_points": min_points,
        "n_input_points": n_input_points,
        "n_clusters": len(clusters),
        "clusters": clusters,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


# -----------------------------------------------------------------------------
# Memory estimation (pre-flight)
# -----------------------------------------------------------------------------
def _estimate_peak_gib(n_pts_diff: int, n_clusters: int,
                       dbscan_voxel_m: float) -> float:
    """Estimate Stage 4 peak RSS in GiB for a given diff-point count.

    Linear empirical model fitted to open3d 0.19 + scipy 1.18 on the
    B tileset (54.7 M points / 5.9 GB, ~10 M ``pts_diff`` after the
    NN filter). The dominant Stage 4 cost is open3d's
    ``cluster_dbscan`` which materialises the entire ε-neighbour graph
    in C++:

    * **decimated path** (``voxel_m > 0``): the cluster input is
      ~``(voxel_m)^3`` decimated (~50–80 k representatives); the
      DBSCAN internal graph is ≲ 20 MiB. Peak working set is
      dominated by ``pts_diff`` (f32, 12 B/elt) + ``ecef`` (f64,
      24 B/elt) + back-projected labels (i64, 8 B/elt) ≈ **50 B/elt
      total** during the Stage 4 hot moment.
    * **undecimated path** (``voxel_m == 0``): the cluster input is
      the full diff cloud; on the B tileset (NN ≈ 0.05 m,
      ε = 3.0 m, min_points = 120) the C++ ε-neighbour graph is
      ~5 KB/elt. **B = 49.6 GiB peak** measured at this density
      → ≈ 5 KB/elt.

    The constants below are conservative upper bounds measured on
    this host (Linux 5.15, open3d 0.19, scipy 1.18). The 1 GiB
    baseline covers the LAS writer + the ECEF matmul intermediate +
    py3dtiles' working set.

    Returns a float in GiB.
    """
    if dbscan_voxel_m and dbscan_voxel_m > 0:
        # ~50 B/elt for decimated path
        per_pt = 0.00000005
    else:
        # ~5 KB/elt for undecimated path (DBSCAN graph dominates)
        per_pt = 0.000005
    return 1.0 + per_pt * float(n_pts_diff) + 0.0003 * float(n_clusters)


def _read_cgroup_memory_max_gib() -> float | None:
    """Return the cgroup memory limit in GiB, or ``None`` if not measurable.

    Reads the v2 path (``/sys/fs/cgroup/memory.max``); falls back to
    the v1 path (``memory.limit_in_bytes``). Returns ``None`` for
    unlimited / unparseable values (``max`` is sometimes the literal
    string ``"max"`` on v2; v1's "unlimited" sentinel is
    9223372036854771712 or similar)."""
    candidates = (
        "/sys/fs/cgroup/memory.max",          # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1 hybrid
    )
    for path in candidates:
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw in ("max", ""):
            return None
        try:
            limit_bytes = int(raw)
        except ValueError:
            return None
        # v1 "unlimited" sentinel
        if limit_bytes >= 2 ** 60:
            return None
        return limit_bytes / (1024 ** 3)
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Convert local-ENU PLY to ECEF, generate final 3D Tiles. "
                    "Intermediate PLY/LAS will be deleted unless --save-intermediate is set. "
                    "Optional: cluster the points into per-instance AABBs (--cluster) "
                    "and emit instances.json for Cesium overlay.",
    )
    p.add_argument("input_ply", type=Path,
                   help="Input PLY in local-ENU coordinates")
    p.add_argument("transform_json", type=Path,
                   help="Path to transform.json (output of extract_leaf_vertices.py)")
    p.add_argument("--tiles-out", type=Path, required=True,
                   help="Output directory for final 3D Tiles (always retained)")
    p.add_argument("--save-intermediate", action="store_true",
                   help="Keep intermediate ECEF PLY and LAS files, default: auto delete")
    p.add_argument("--cluster", action="store_true",
                   help="DBSCAN-cluster the ENU points and emit instances.json "
                        "(one AABB per cluster) for downstream Cesium overlay. "
                        "Default: skip clustering (backward-compatible). "
                        "When --cluster is set, DBSCAN noise points (label = -1) "
                        "are filtered out of the output 3D Tiles by default so "
                        "the clustering also acts as a point-cloud filter; pass "
                        "--keep-noise to retain them.")
    p.add_argument("--keep-noise", action="store_true",
                   help="When --cluster is set, retain DBSCAN noise points in the "
                        "output 3D Tiles (default: filter them out — clustering "
                        "doubles as a filter on the input point cloud).")
    p.add_argument("--eps", type=float, default=DBSCAN_EPS_M,
                   help="DBSCAN eps radius in metres (only used with --cluster). "
                        "Default: %(default)s")
    p.add_argument("--min-points", type=int, default=DBSCAN_MIN_POINTS,
                   help="DBSCAN min_points (only used with --cluster). "
                        "Default: %(default)s")
    p.add_argument("--instances-out", type=Path, default=None,
                   help="Path to instances.json (default: <--tiles-out>/instances.json)")
    args = p.parse_args(argv)

    # 1. 读取转换矩阵
    m = json.loads(args.transform_json.read_text())
    if "transform" not in m:
        raise SystemExit(f"{args.transform_json}: missing 'transform' key")
    T = np.asarray(m["transform"], dtype=np.float64).reshape((4, 4), order="F")

    # 2. 读取原始PLY并做ENU->ECEF坐标转换（内存运算）
    props, prop_list, fmt = read_ply(args.input_ply)
    if not {"x", "y", "z"}.issubset(props.keys()):
        raise SystemExit(f"{args.input_ply}: missing x/y/z properties")

    xyz = np.column_stack([props["x"], props["y"], props["z"]]).astype(np.float64)

    # PLY input is standard ENU (x=East, y=North, z=Up), produced by
    # extract_leaf_vertices.py with the b3dm POSITION (E, U, S) → ENU fix.
    # Apply the ENU→ECEF transform directly: no rotation or sign-flip needed.
    homog = np.hstack([xyz, np.ones((len(xyz), 1))])
    ecef = (homog @ T.T)[:, :3]

    # 2.5 可选:基于 ENU 做 DBSCAN 聚类,把每个 cluster 的 AABB 写到 instances.json。
    # 用于 Cesium 端画 bbox 线框(参考 scripts/cesium_test.html)。bbox 用 ECEF 坐标,
    # 前端可直接 Cesium.Cartesian3.fromArray() 使用。
    # 注意:instances.json 必须在 py3dtiles(--overwrite 会清空输出目录)之后写,
    # 因此这里只保留 clusters 数据,JSON 落盘放到最后一步。
    if args.cluster:
        clusters, labels = cluster_instances(
            xyz_enu=xyz,
            ecef=ecef,
            eps=args.eps,
            min_points=args.min_points,
        )

        # 默认:把 DBSCAN 标记为 -1 的噪声点从最终 3D Tiles 中剔除,
        # 让聚类同时起到点云滤波的作用。--keep-noise 显式保留。
        n_input = len(xyz)
        n_kept = n_input
        if not args.keep_noise:
            keep = labels >= 0
            n_kept = int(keep.sum())
            n_dropped = n_input - n_kept
            print(
                f"[cluster] filtering {n_dropped:,} noise points -> "
                f"{n_kept:,} kept (use --keep-noise to retain noise)",
                file=sys.stderr,
            )
            ecef = ecef[keep]
            for k in props:
                props[k] = props[k][keep]

        instances_path = args.instances_out or (args.tiles_out / "instances.json")
        print(
            f"[cluster] {len(clusters)} clusters -> {instances_path} "
            f"(eps={args.eps}, min_points={args.min_points}, "
            f"in={n_input:,}, out={n_kept:,})",
            file=sys.stderr,
        )

    props["x"] = ecef[:, 0].astype(props["x"].dtype)
    props["y"] = ecef[:, 1].astype(props["y"].dtype)
    props["z"] = ecef[:, 2].astype(props["z"].dtype)

    # 定义中间文件路径（同输入目录生成临时文件）
    temp_ecef_ply = args.input_ply.with_stem(args.input_ply.stem + "_ecef_temp")
    temp_las = temp_ecef_ply.with_suffix(".las")

    # 3. 按需写入中间ECEF PLY
    if args.save_intermediate:
        write_ply(temp_ecef_ply, props, prop_list, fmt=fmt)
        print(
            f"Converted {len(xyz):,} points ENU -> ECEF ({fmt}) -> {temp_ecef_ply}",
            file=sys.stderr,
        )
    else:
        # 临时落地仅用于laspy读取，转换完成后删除
        write_ply(temp_ecef_ply, props, prop_list, fmt=fmt)

    # 4. PLY转LAS
    save_ecef_ply_to_las(str(temp_ecef_ply), str(temp_las))

    # 5. 写 instances.json(在 py3dtiles 之前 — clusters dict 此时已经完整,
    #    早写可以让它在 LAS/py3dtiles 阶段被引用计数释放,节省 ~200 MiB peak;
    #    同时 py3dtiles --overwrite 不再威胁这个文件,因为它已经落在
    #    tiles_out 之外 / 之内都不会被 py3dtiles 触及——我们写到 args.tiles_out
    #    之外即 <out>/instances.json,py3dtiles 写的是 <out>/3DTiles/...)
    if args.cluster:
        write_instances_json(
            instances_path,
            clusters,
            eps=args.eps,
            min_points=args.min_points,
            n_input_points=len(xyz),
        )
        print(f"[cluster] instances.json written -> {instances_path}", file=sys.stderr)
        # Drop the cluster-list reference so the per-cluster hull dicts
        # can be GC'd before py3dtiles spawns (each cluster dict holds
        # an (M, 3) hull_vertices_ecef list — 50–200 clusters at
        # ~10–50 kB each adds up).  We do NOT touch labels / ecef /
        # props — those are still needed by the LAS path below.
        del clusters

    # 6. LAS转3D Tiles（强制输出，永久保留）
    tile_result = convert_las_to_3dtiles(
        las_input=str(temp_las),
        out_dir=str(args.tiles_out)
    )
    if not tile_result["ok"]:
        raise SystemExit(f"3D Tiles generation failed: {tile_result['message']}")
    print(f"\n✅ 3D Tiles 生成完成，输出目录: {args.tiles_out}")

    # 7. 清理中间文件（未开启保存中间文件时删除ply+las）
    if not args.save_intermediate:
        for tmp_file in [temp_ecef_ply, temp_las]:
            if tmp_file.exists():
                tmp_file.unlink()
                print(f"已自动清理中间临时文件: {tmp_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())