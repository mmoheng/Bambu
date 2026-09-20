"""Low-level mesh math shared by the analyzer: normals/areas, edge/topology
checks, connected components, and a vectorized ray-triangle intersection
used for the thin-wall thickness estimate.

Everything here operates on a `mesh_io.Mesh` (numpy vertices/faces) with
only numpy/scipy/networkx as dependencies (all three are assumed
preinstalled — see the project README for the environment this was
developed against).
"""

from __future__ import annotations

import networkx as nx
import numpy as np

from .mesh_io import Mesh

EPS = 1e-9


def face_vertices(mesh: Mesh) -> np.ndarray:
    """(M, 3, 3) — for each face, its 3 vertex positions."""
    return mesh.vertices[mesh.faces]


def face_normals_and_areas(mesh: Mesh) -> tuple[np.ndarray, np.ndarray]:
    """Returns (unit_normals (M,3), areas_mm2 (M,)).

    Degenerate faces (zero area) get a zero normal; callers should treat
    an area of ~0 as "ignore this face" rather than dividing by it.
    """
    tris = face_vertices(mesh)
    e1 = tris[:, 1] - tris[:, 0]
    e2 = tris[:, 2] - tris[:, 0]
    cross = np.cross(e1, e2)
    lengths = np.linalg.norm(cross, axis=1)
    areas = lengths / 2.0
    normals = np.zeros_like(cross)
    nonzero = lengths > EPS
    normals[nonzero] = cross[nonzero] / lengths[nonzero, None]
    return normals, areas


def face_centroids(mesh: Mesh) -> np.ndarray:
    return face_vertices(mesh).mean(axis=1)


def face_tilt_from_horizontal_deg(normals: np.ndarray) -> np.ndarray:
    """Angle of the FACE PLANE relative to the horizontal (XY) build plate.

    0 deg  = face lies flat/horizontal (normal is vertical) — e.g. the
             underside of a flat overhang or a bridge ceiling.
    90 deg = face is a vertical wall (normal is horizontal) — fully
             self-supporting layer-on-layer.

    This is a plain geometric measurement, not a reproduction of Bambu
    Studio's internal support algorithm. It's meant to rank/flag geometry
    for the optimizer's recommendations, not to predict slicer behavior
    exactly. Calibrate `threshold_deg` in analyzer.py against real slices
    from your printer before leaning on absolute numbers.
    """
    nz = np.clip(np.abs(normals[:, 2]), 0.0, 1.0)
    return np.degrees(np.arccos(nz))


def edge_topology(mesh: Mesh) -> dict:
    """Builds an undirected-edge -> incident-face-count map and returns a
    summary used for manifold/watertight checks.

    A closed, manifold mesh has every edge shared by exactly 2 faces. An
    edge shared by 1 face is a boundary (hole in the surface); shared by
    3+ faces is non-manifold (e.g. two shells fused along a seam).
    """
    faces = mesh.faces
    edges = np.concatenate(
        [
            faces[:, [0, 1]],
            faces[:, [1, 2]],
            faces[:, [2, 0]],
        ],
        axis=0,
    )
    edges_sorted = np.sort(edges, axis=1)
    unique_edges, counts = np.unique(edges_sorted, axis=0, return_counts=True)

    boundary_count = int(np.sum(counts == 1))
    nonmanifold_count = int(np.sum(counts > 2))
    is_watertight = boundary_count == 0 and nonmanifold_count == 0

    return {
        "unique_edges": unique_edges,
        "edge_face_counts": counts,
        "boundary_edge_count": boundary_count,
        "nonmanifold_edge_count": nonmanifold_count,
        "is_watertight": is_watertight,
    }


def connected_components(mesh: Mesh) -> list[np.ndarray]:
    """Face-adjacency connected components (faces sharing an edge), as a
    list of face-index arrays. Multiple components usually mean either
    multiple separate bodies in one file, or a single body with a
    non-manifold seam splitting it topologically.
    """
    faces = mesh.faces
    edges = np.concatenate(
        [faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]], axis=0
    )
    edges_sorted = np.sort(edges, axis=1)
    face_ids = np.tile(np.arange(faces.shape[0]), 3)

    edge_to_faces: dict[tuple[int, int], list[int]] = {}
    for (a, b), fid in zip(map(tuple, edges_sorted), face_ids):
        edge_to_faces.setdefault((int(a), int(b)), []).append(int(fid))

    g = nx.Graph()
    g.add_nodes_from(range(faces.shape[0]))
    for fids in edge_to_faces.values():
        for i in range(len(fids)):
            for j in range(i + 1, len(fids)):
                g.add_edge(fids[i], fids[j])

    return [np.array(sorted(c), dtype=np.int64) for c in nx.connected_components(g)]


def boundary_loops(mesh: Mesh) -> list[np.ndarray]:
    """Ordered vertex loops along open (boundary) edges — i.e. holes in
    the surface. Used as a cheap proxy for "round hole" fit-sensitive
    features. Returns a list of vertex-index arrays, each forming a
    closed loop (best-effort; a genuinely broken mesh may not close).
    """
    topo = edge_topology(mesh)
    boundary_edges = topo["unique_edges"][topo["edge_face_counts"] == 1]
    if boundary_edges.shape[0] == 0:
        return []

    adjacency: dict[int, list[int]] = {}
    for a, b in boundary_edges:
        adjacency.setdefault(int(a), []).append(int(b))
        adjacency.setdefault(int(b), []).append(int(a))

    visited_edges: set[tuple[int, int]] = set()
    loops: list[np.ndarray] = []

    for start in adjacency:
        for nxt in adjacency[start]:
            key = tuple(sorted((start, nxt)))
            if key in visited_edges:
                continue
            loop = [start]
            prev, cur = start, nxt
            visited_edges.add(key)
            safety = 0
            while cur != start and safety < len(boundary_edges) + 2:
                loop.append(cur)
                neighbors = [n for n in adjacency.get(cur, []) if n != prev]
                if not neighbors:
                    break
                nxt2 = neighbors[0]
                visited_edges.add(tuple(sorted((cur, nxt2))))
                prev, cur = cur, nxt2
                safety += 1
            if cur == start:
                loops.append(np.array(loop, dtype=np.int64))
    return loops


def connected_components_of_subset(mesh: Mesh, face_indices: np.ndarray) -> list[np.ndarray]:
    """Like `connected_components`, but restricted to a subset of faces
    (e.g. only faces flagged as bridges), where two faces are only
    considered adjacent if BOTH are in the subset. Used to split a flagged
    region (like "all bridge faces") into separate islands so span/area
    can be reported per-island instead of lumped together.
    """
    if face_indices.size == 0:
        return []
    faces = mesh.faces[face_indices]
    edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]], axis=0)
    edges_sorted = np.sort(edges, axis=1)
    local_face_ids = np.tile(np.arange(face_indices.shape[0]), 3)

    edge_to_faces: dict[tuple[int, int], list[int]] = {}
    for (a, b), fid in zip(map(tuple, edges_sorted), local_face_ids):
        edge_to_faces.setdefault((int(a), int(b)), []).append(int(fid))

    g = nx.Graph()
    g.add_nodes_from(range(face_indices.shape[0]))
    for fids in edge_to_faces.values():
        for i in range(len(fids)):
            for j in range(i + 1, len(fids)):
                g.add_edge(fids[i], fids[j])

    components = []
    for c in nx.connected_components(g):
        local_idx = np.array(sorted(c), dtype=np.int64)
        components.append(face_indices[local_idx])
    return components


def ray_mesh_first_hit_distance(
    origins: np.ndarray, directions: np.ndarray, mesh: Mesh
) -> np.ndarray:
    """Vectorized Moeller-Trumbore ray/triangle intersection: for each of
    N rays, returns the distance to the nearest intersected triangle (or
    np.inf if none hit). Brute-force O(N * M) over all M faces — fine for
    the sampled thin-wall check (a few hundred rays) on meshes up to a
    few tens of thousands of faces; not intended for real-time use on
    huge meshes (no BVH/acceleration structure — a documented TODO if
    this becomes a bottleneck on real parts).
    """
    tris = face_vertices(mesh)  # (M, 3, 3)
    v0, v1, v2 = tris[:, 0], tris[:, 1], tris[:, 2]
    e1 = v1 - v0
    e2 = v2 - v0

    n_rays = origins.shape[0]
    n_faces = tris.shape[0]
    best = np.full(n_rays, np.inf, dtype=np.float64)

    # Chunk over rays to keep peak memory bounded for larger meshes.
    chunk = max(1, min(n_rays, 200000 // max(n_faces, 1) + 1))
    for start in range(0, n_rays, chunk):
        o = origins[start : start + chunk]  # (k, 3)
        d = directions[start : start + chunk]  # (k, 3)
        k = o.shape[0]

        o_ = o[:, None, :]  # (k,1,3)
        d_ = d[:, None, :]
        pvec = np.cross(d_, e2[None, :, :])  # (k,M,3)
        det = np.sum(e1[None, :, :] * pvec, axis=2)  # (k,M)
        invalid_det = np.abs(det) < 1e-10
        safe_det = np.where(invalid_det, 1.0, det)

        tvec = o_ - v0[None, :, :]
        u = np.sum(tvec * pvec, axis=2) / safe_det
        qvec = np.cross(tvec, e1[None, :, :])
        v = np.sum(d_ * qvec, axis=2) / safe_det
        t = np.sum(e2[None, :, :] * qvec, axis=2) / safe_det

        hit = (
            (~invalid_det)
            & (u >= -1e-7)
            & (v >= -1e-7)
            & ((u + v) <= 1 + 1e-7)
            & (t > 1e-6)
        )
        t_masked = np.where(hit, t, np.inf)
        best[start : start + k] = np.min(t_masked, axis=1)

    return best
