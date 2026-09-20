"""Minimal, dependency-free STL and 3MF loaders.

Why hand-rolled instead of trimesh/numpy-stl: this project targets a
locked-down build/dev environment with no package-index access, and the
Windows bridge should stay light to install. Both formats are simple
enough to parse directly with the standard library plus numpy (which is
assumed available everywhere this runs).

Only geometry is extracted — for 3MF that means triangle meshes from
<object>/<mesh> elements with any <build> transform applied. Slicer
metadata (plate layout, per-object settings, etc.) in a Bambu Studio
project 3MF is intentionally ignored; the analyzer only cares about the
solid geometry.

Returns a ``Mesh`` (vertices: (N,3) float64 array, faces: (M,3) int64
array of vertex indices).
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class MeshLoadError(ValueError):
    """Raised when a file can't be parsed as a supported mesh format."""


@dataclass
class Mesh:
    vertices: np.ndarray  # (N, 3) float64
    faces: np.ndarray  # (M, 3) int64, indices into vertices

    @property
    def face_count(self) -> int:
        return int(self.faces.shape[0])

    @property
    def vertex_count(self) -> int:
        return int(self.vertices.shape[0])

    def face_vertices(self) -> np.ndarray:
        """Returns (M, 3, 3): for each face, its 3 vertex positions."""
        return self.vertices[self.faces]


def load_mesh(path: str | Path) -> Mesh:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".stl":
        return _load_stl(path)
    if suffix == ".3mf":
        return _load_3mf(path)
    raise MeshLoadError(
        f"Unsupported file type '{suffix}'. Bambu Companion V1 supports .stl and .3mf."
    )


# ---------------------------------------------------------------------------
# STL
# ---------------------------------------------------------------------------


def _load_stl(path: Path) -> Mesh:
    data = path.read_bytes()
    if _looks_like_ascii_stl(data):
        return _load_stl_ascii(data)
    return _load_stl_binary(data)


def _looks_like_ascii_stl(data: bytes) -> bool:
    head = data[:5].lower()
    if head != b"solid":
        return False
    # Some binary STLs start their 80-byte header with the word "solid" too
    # (a well-known gotcha). Binary files have a reliable structure we can
    # cross-check: byte 80-83 is a uint32 triangle count, and the total
    # file size must equal 84 + count * 50 exactly.
    if len(data) < 84:
        return True
    (tri_count,) = struct.unpack_from("<I", data, 80)
    expected_binary_size = 84 + tri_count * 50
    if expected_binary_size == len(data):
        return False
    return True


def _load_stl_binary(data: bytes) -> Mesh:
    if len(data) < 84:
        raise MeshLoadError("STL file too small to contain a valid binary header.")
    (tri_count,) = struct.unpack_from("<I", data, 80)
    expected_size = 84 + tri_count * 50
    if expected_size > len(data):
        raise MeshLoadError(
            f"Binary STL header claims {tri_count} triangles but the file is truncated."
        )

    # Each triangle record: 12f normal (unused, we recompute), 9f vertices, 2x uint16 attr
    record_dtype = np.dtype(
        [
            ("normal", "<f4", (3,)),
            ("v0", "<f4", (3,)),
            ("v1", "<f4", (3,)),
            ("v2", "<f4", (3,)),
            ("attr", "<u2"),
        ]
    )
    records = np.frombuffer(data, dtype=record_dtype, count=tri_count, offset=84)
    raw_vertices = np.concatenate(
        [records["v0"], records["v1"], records["v2"]], axis=0
    ).astype(np.float64)
    faces = np.arange(tri_count * 3, dtype=np.int64).reshape(tri_count, 3, order="F")
    return _weld_vertices(raw_vertices, faces)


def _load_stl_ascii(data: bytes) -> Mesh:
    text = data.decode("utf-8", errors="replace")
    verts: list[tuple[float, float, float]] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            parts = line.split()
            if len(parts) != 4:
                continue
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
    if not verts or len(verts) % 3 != 0:
        raise MeshLoadError(
            "ASCII STL parse produced a non-multiple-of-3 vertex count; file may be malformed."
        )
    raw_vertices = np.array(verts, dtype=np.float64)
    tri_count = len(verts) // 3
    faces = np.arange(tri_count * 3, dtype=np.int64).reshape(tri_count, 3)
    return _weld_vertices(raw_vertices, faces)


def _weld_vertices(raw_vertices: np.ndarray, faces_into_raw: np.ndarray, decimals: int = 6) -> Mesh:
    """STL stores an independent vertex triple per triangle (no sharing).
    We weld coincident vertices (rounded to `decimals`) so downstream
    analysis (connected components, manifold/edge checks, thin-wall ray
    casts) sees a real mesh topology instead of 3x-duplicated points.
    """
    rounded = np.round(raw_vertices, decimals=decimals)
    unique_vertices, inverse = np.unique(rounded, axis=0, return_inverse=True)
    inverse = inverse.reshape(-1)
    faces = inverse[faces_into_raw.reshape(-1)].reshape(faces_into_raw.shape)
    return Mesh(vertices=unique_vertices.astype(np.float64), faces=faces.astype(np.int64))


# ---------------------------------------------------------------------------
# 3MF
# ---------------------------------------------------------------------------

_3MF_NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"


def _load_3mf(path: Path) -> Mesh:
    with zipfile.ZipFile(path) as zf:
        model_path = _find_3mf_model_part(zf)
        with zf.open(model_path) as f:
            root = ET.parse(f).getroot()

    objects: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    resources = root.find(f"{_3MF_NS}resources")
    if resources is None:
        raise MeshLoadError("3MF file has no <resources> element.")

    for obj in resources.findall(f"{_3MF_NS}object"):
        mesh_el = obj.find(f"{_3MF_NS}mesh")
        if mesh_el is None:
            continue  # components-only object; skipped in V1
        obj_id = obj.get("id")
        objects[obj_id] = _parse_3mf_mesh(mesh_el)

    build = root.find(f"{_3MF_NS}build")
    if build is None or not list(build):
        # No build items (unusual) — fall back to concatenating every object as-is.
        items = [(obj_id, None) for obj_id in objects]
    else:
        items = []
        for item in build.findall(f"{_3MF_NS}item"):
            obj_id = item.get("objectid")
            transform_attr = item.get("transform")
            transform = _parse_3mf_transform(transform_attr) if transform_attr else None
            items.append((obj_id, transform))

    all_vertices: list[np.ndarray] = []
    all_faces: list[np.ndarray] = []
    vertex_offset = 0
    for obj_id, transform in items:
        if obj_id not in objects:
            continue
        verts, faces = objects[obj_id]
        if transform is not None:
            linear, translation = transform
            verts = verts @ linear + translation
        all_vertices.append(verts)
        all_faces.append(faces + vertex_offset)
        vertex_offset += verts.shape[0]

    if not all_vertices:
        raise MeshLoadError("3MF file contained no usable mesh geometry.")

    return Mesh(
        vertices=np.concatenate(all_vertices, axis=0),
        faces=np.concatenate(all_faces, axis=0),
    )


def _find_3mf_model_part(zf: zipfile.ZipFile) -> str:
    # Standard location; fall back to scanning for any *.model file if a
    # nonstandard package doesn't use it (seen in some exporter quirks).
    if "3D/3dmodel.model" in zf.namelist():
        return "3D/3dmodel.model"
    for name in zf.namelist():
        if name.lower().endswith(".model"):
            return name
    raise MeshLoadError("3MF package has no 3D model part (3D/3dmodel.model).")


def _parse_3mf_mesh(mesh_el: ET.Element) -> tuple[np.ndarray, np.ndarray]:
    vertices_el = mesh_el.find(f"{_3MF_NS}vertices")
    triangles_el = mesh_el.find(f"{_3MF_NS}triangles")
    if vertices_el is None or triangles_el is None:
        raise MeshLoadError("3MF <mesh> is missing <vertices> or <triangles>.")

    verts = np.array(
        [
            (float(v.get("x")), float(v.get("y")), float(v.get("z")))
            for v in vertices_el.findall(f"{_3MF_NS}vertex")
        ],
        dtype=np.float64,
    )
    faces = np.array(
        [
            (int(t.get("v1")), int(t.get("v2")), int(t.get("v3")))
            for t in triangles_el.findall(f"{_3MF_NS}triangle")
        ],
        dtype=np.int64,
    )
    return verts, faces


def _parse_3mf_transform(attr: str) -> tuple[np.ndarray, np.ndarray]:
    """3MF transform attribute: 12 space-separated floats, row-major for a
    4x3 matrix (3x3 linear part + 1x3 translation). Applied to a point p
    (as a row vector) as: p' = p @ linear + translation.
    """
    values = [float(x) for x in attr.split()]
    if len(values) != 12:
        raise MeshLoadError(f"Unexpected 3MF transform with {len(values)} values (expected 12).")
    m = np.array(values, dtype=np.float64).reshape(4, 3)
    linear = m[:3, :]
    translation = m[3, :]
    return linear, translation
