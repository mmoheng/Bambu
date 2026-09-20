"""Synthetic mesh fixtures for tests — no external STL/3MF sample files
needed. Each function returns a "triangle soup" (an (N, 3, 3) array of
raw, non-deduplicated triangle vertices, the way a real exporter would
produce them) with verified outward-facing winding, which
`write_stl_binary` serializes to a real binary STL file. Tests then load
that file through the actual `mesh_io.load_mesh` path — exercising the
real loader/welding code instead of hand-building `Mesh` objects.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np


def box_triangle_soup(x0, y0, z0, x1, y1, z1) -> np.ndarray:
    """A single axis-aligned box, 12 triangles, verified outward winding."""
    c = {
        "000": (x0, y0, z0),
        "100": (x1, y0, z0),
        "110": (x1, y1, z0),
        "010": (x0, y1, z0),
        "001": (x0, y0, z1),
        "101": (x1, y0, z1),
        "111": (x1, y1, z1),
        "011": (x0, y1, z1),
    }
    quads = [
        ("001", "101", "111", "011"),  # +Z
        ("000", "010", "110", "100"),  # -Z
        ("100", "110", "111", "101"),  # +X
        ("001", "011", "010", "000"),  # -X
        ("010", "011", "111", "110"),  # +Y
        ("000", "100", "101", "001"),  # -Y
    ]
    tris = []
    for a, b, cc, d in quads:
        tris.append([c[a], c[b], c[cc]])
        tris.append([c[a], c[cc], c[d]])
    return np.array(tris, dtype=np.float64)


def cube_soup(size: float = 10.0, origin=(0.0, 0.0, 0.0)) -> np.ndarray:
    x0, y0, z0 = origin
    return box_triangle_soup(x0, y0, z0, x0 + size, y0 + size, z0 + size)


def bridge_soup() -> np.ndarray:
    """Two 10x10x30 pillars 60mm apart (center-to-center via 50mm gap),
    connected by a 60x10x10 beam on top. The beam's underside between the
    pillars (x=10..50) has nothing below it — a genuine overhang/bridge.
    The beam ends (x=0..10 and x=50..60) sit exactly on the pillar tops,
    so those coincide and weld into real connectivity.
    """
    pillar_a = box_triangle_soup(0, 0, 0, 10, 10, 30)
    pillar_b = box_triangle_soup(50, 0, 0, 60, 10, 30)
    beam = box_triangle_soup(0, 0, 30, 60, 10, 40)
    return np.concatenate([pillar_a, pillar_b, beam], axis=0)


def two_separate_cubes_soup() -> np.ndarray:
    a = cube_soup(5.0, origin=(0, 0, 0))
    b = cube_soup(5.0, origin=(100, 100, 0))
    return np.concatenate([a, b], axis=0)


def annulus_soup(inner_r: float, outer_r: float, n_segments: int = 16, z: float = 0.0) -> np.ndarray:
    """A flat ring (NOT a solid, no thickness) with two open circular
    boundaries — purely a fixture for exercising boundary-loop / hole
    detection, not a printable shape.
    """
    angles = np.linspace(0, 2 * np.pi, n_segments, endpoint=False)
    outer = np.stack([outer_r * np.cos(angles), outer_r * np.sin(angles), np.full(n_segments, z)], axis=1)
    inner = np.stack([inner_r * np.cos(angles), inner_r * np.sin(angles), np.full(n_segments, z)], axis=1)

    tris = []
    for i in range(n_segments):
        j = (i + 1) % n_segments
        # Two triangles per quad segment; winding chosen so the normal
        # points +Z (doesn't matter for the boundary-loop test, but keeps
        # the fixture sane if reused elsewhere).
        tris.append([outer[i], outer[j], inner[j]])
        tris.append([outer[i], inner[j], inner[i]])
    return np.array(tris, dtype=np.float64)


def write_stl_binary(triangle_soup: np.ndarray, path: str | Path) -> None:
    path = Path(path)
    tri_count = triangle_soup.shape[0]
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", tri_count))
        for tri in triangle_soup:
            normal = np.cross(tri[1] - tri[0], tri[2] - tri[0])
            n = np.linalg.norm(normal)
            normal = normal / n if n > 1e-12 else np.zeros(3)
            f.write(struct.pack("<3f", *normal.astype(np.float32)))
            for v in tri:
                f.write(struct.pack("<3f", *v.astype(np.float32)))
            f.write(struct.pack("<H", 0))
