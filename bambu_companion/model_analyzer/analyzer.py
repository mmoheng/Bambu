"""Top-level model analysis entry point.

`analyze()` loads an STL/3MF file and returns a populated
`schemas.AnalysisResult` covering everything the project spec's Model
Analyzer section asks for: dimensions/bounding box, A1 bed-fit, thin
walls, overhangs, bridges, bed-contact area, small features,
fit-sensitive geometry, candidate orientations, and manifold/geometry
warnings.

Known V1 scope limits (documented rather than silently glossed over, per
the project's "flag uncertainty" rule):
- Thin-wall thickness is estimated by sampled inward ray casts, not an
  exact medial-axis/shape-diameter computation. Good for flagging
  trouble spots, not a certified measurement.
- Fit-sensitive "hole" detection only finds OPEN (non-watertight)
  boundary loops. A proper through-hole in a clean, watertight solid is
  a closed cylindrical surface, not an open edge, and is NOT detected in
  V1 — that needs primitive/curvature fitting, left as future work.
- Small-feature detection only catches separate small bodies (multi-body
  files), not small single-body protrusions on an otherwise large part —
  that also needs local-feature-size analysis, left as future work.
- Orientation candidates rank flat-resting-face options by a simple
  bed-contact-vs-overhang score. It does not simulate physical tipping
  stability (no center-of-mass/support-polygon check) the way a full
  slicer's auto-orient does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
from scipy.spatial import ConvexHull
from scipy.spatial.transform import Rotation

from ..schemas import (
    AnalysisResult,
    BedFitResult,
    BoundingBox,
    BridgeSummary,
    FitSensitiveFeature,
    GeometryWarning,
    OrientationCandidate,
    OverhangSummary,
    SmallFeature,
    ThinWallRegion,
    ThinWallSummary,
)
from . import geometry as geo
from .mesh_io import Mesh, load_mesh

# Bambu Lab A1 build volume, confirmed against Bambu's official tech-specs
# page (256 x 256 x 256 mm). NOTE: the *usable* volume can be smaller than
# this depending on accessories/orientation per Bambu's own "print volume
# limitations" documentation (e.g. certain configurations reduce usable
# height) — this constant is the raw chamber volume, not a guaranteed
# printable envelope. Treat `fits=True` as "worth attempting", not a
# guarantee; the temporary-profile slice step is still the real check.
A1_BUILD_VOLUME_MM = (256.0, 256.0, 256.0)

BED_CONTACT_EPS_MM = 0.05


def analyze(
    path: str | Path,
    *,
    build_volume_mm: tuple[float, float, float] = A1_BUILD_VOLUME_MM,
    build_volume_margin_mm: float = 0.0,
    nozzle_diameter_mm: float = 0.4,
    current_wall_loops: int = 2,
    overhang_threshold_deg: float = 45.0,
    bridge_threshold_deg: float = 10.0,
    small_feature_threshold_mm: float = 3.0,
    thin_wall_sample_count: int = 250,
    orientation_candidate_count: int = 3,
    random_seed: int = 0,
    object_ids: Iterable[str] | None = None,
) -> AnalysisResult:
    mesh = load_mesh(path, object_ids=object_ids)
    normals, areas = geo.face_normals_and_areas(mesh)
    tilts = geo.face_tilt_from_horizontal_deg(normals)

    bbox = _bounding_box(mesh)
    bed_fit = _bed_fit(bbox, build_volume_mm, build_volume_margin_mm)

    bed_z = bbox.min[2]
    bed_contact_area = _bed_contact_area(mesh, areas, bed_z)

    overhangs = _overhangs(mesh, normals, areas, tilts, bed_z, overhang_threshold_deg)
    bridges = _bridges(mesh, normals, areas, tilts, bed_z, bridge_threshold_deg)

    thin_walls = _thin_walls(
        mesh,
        normals,
        areas,
        nozzle_diameter_mm=nozzle_diameter_mm,
        current_wall_loops=current_wall_loops,
        sample_count=thin_wall_sample_count,
        seed=random_seed,
    )

    components = geo.connected_components(mesh)
    small_features = _small_features(mesh, components, small_feature_threshold_mm)
    fit_sensitive = _fit_sensitive_features(mesh)

    orientation_candidates = _orientation_candidates(
        mesh,
        overhang_threshold_deg=overhang_threshold_deg,
        top_k=orientation_candidate_count,
    )

    topo = geo.edge_topology(mesh)
    warnings: list[GeometryWarning] = []
    if topo["boundary_edge_count"] > 0:
        warnings.append(
            GeometryWarning(
                kind="open_boundary",
                detail=(
                    f"{topo['boundary_edge_count']} open (non-watertight) edges found. "
                    "The mesh has holes in its surface; slicing may produce unexpected "
                    "results at these locations."
                ),
            )
        )
    if topo["nonmanifold_edge_count"] > 0:
        warnings.append(
            GeometryWarning(
                kind="non_manifold_edges",
                detail=(
                    f"{topo['nonmanifold_edge_count']} edges are shared by more than 2 "
                    "faces (non-manifold geometry). Consider running mesh repair before "
                    "slicing."
                ),
            )
        )
    if len(components) > 1:
        warnings.append(
            GeometryWarning(
                kind="multiple_bodies",
                detail=(
                    f"File contains {len(components)} separate, disconnected bodies. "
                    "Confirm this is intentional (e.g. a multi-part plate) rather than a "
                    "broken single part."
                ),
            )
        )

    return AnalysisResult(
        source_file=str(path),
        bounding_box=bbox,
        bed_fit=bed_fit,
        bed_contact_area_mm2=bed_contact_area,
        overhangs=overhangs,
        bridges=bridges,
        thin_walls=thin_walls,
        small_features=small_features,
        fit_sensitive_features=fit_sensitive,
        orientation_candidates=orientation_candidates,
        warnings=warnings,
        body_count=len(components),
        is_watertight=topo["is_watertight"],
        face_count=mesh.face_count,
        vertex_count=mesh.vertex_count,
    )


# ---------------------------------------------------------------------------
# Individual analyses
# ---------------------------------------------------------------------------


def _bounding_box(mesh: Mesh) -> BoundingBox:
    mn = mesh.vertices.min(axis=0)
    mx = mesh.vertices.max(axis=0)
    return BoundingBox(min=tuple(mn.tolist()), max=tuple(mx.tolist()))


def _bed_fit(
    bbox: BoundingBox,
    build_volume_mm: tuple[float, float, float],
    margin_mm: float,
) -> BedFitResult:
    usable = tuple(v - 2 * margin_mm for v in build_volume_mm)
    size = bbox.size
    margins = tuple(usable[i] - size[i] for i in range(3))
    fits = all(m >= 0 for m in margins)
    notes = []
    if not fits:
        overflow_axes = [axis for axis, m in zip("XYZ", margins) if m < 0]
        notes.append(
            f"Model exceeds the A1 build volume on axis/axes: {', '.join(overflow_axes)}. "
            "Consider splitting the model, scaling it down, or reorienting — reorientation "
            "only helps if a different candidate orientation (see orientation_candidates) "
            "has a smaller footprint on the offending axis."
        )
    return BedFitResult(
        fits=fits,
        build_volume_mm=build_volume_mm,
        model_size_mm=size,
        margin_mm=margins,
        notes=notes,
    )


def _bed_contact_area(mesh: Mesh, areas: np.ndarray, bed_z: float) -> float:
    # A face only counts as "resting on the bed" if it's flat against it —
    # i.e. ALL of its vertices are at bed height, not just one (a vertical
    # side wall has exactly one edge touching the bed but is not contact
    # area). Using max_z (not min_z) is what enforces that.
    face_max_z = mesh.vertices[mesh.faces][:, :, 2].max(axis=1)
    on_bed = face_max_z <= bed_z + BED_CONTACT_EPS_MM
    return float(areas[on_bed].sum())


def _overhangs(
    mesh: Mesh,
    normals: np.ndarray,
    areas: np.ndarray,
    tilts: np.ndarray,
    bed_z: float,
    threshold_deg: float,
) -> OverhangSummary:
    face_min_z = mesh.vertices[mesh.faces][:, :, 2].min(axis=1)
    above_bed = face_min_z > bed_z + BED_CONTACT_EPS_MM
    facing_down = normals[:, 2] < -1e-6
    flagged = above_bed & facing_down & (tilts < threshold_deg)

    worst = float(tilts[flagged].min()) if flagged.any() else None
    islands = geo.connected_components_of_subset(mesh, np.nonzero(flagged)[0])
    return OverhangSummary(
        threshold_deg=threshold_deg,
        overhang_area_mm2=float(areas[flagged].sum()),
        overhang_face_count=int(flagged.sum()),
        worst_face_tilt_deg=worst,
        island_count=len(islands),
        largest_island_area_mm2=max((float(areas[i].sum()) for i in islands), default=0.0),
    )


def _bridges(
    mesh: Mesh,
    normals: np.ndarray,
    areas: np.ndarray,
    tilts: np.ndarray,
    bed_z: float,
    threshold_deg: float,
) -> BridgeSummary:
    face_min_z = mesh.vertices[mesh.faces][:, :, 2].min(axis=1)
    above_bed = face_min_z > bed_z + BED_CONTACT_EPS_MM
    facing_down = normals[:, 2] < -1e-6
    flagged = above_bed & facing_down & (tilts < threshold_deg)
    face_idx = np.nonzero(flagged)[0]

    if face_idx.size == 0:
        return BridgeSummary(bridge_face_count=0, bridge_area_mm2=0.0, longest_span_mm=0.0)

    islands = geo.connected_components_of_subset(mesh, face_idx)
    longest_span = 0.0
    for island in islands:
        pts = mesh.vertices[mesh.faces[island]].reshape(-1, 3)[:, :2]
        span = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))
        longest_span = max(longest_span, span)

    return BridgeSummary(
        bridge_face_count=int(face_idx.size),
        bridge_area_mm2=float(areas[face_idx].sum()),
        longest_span_mm=longest_span,
    )


def _thin_walls(
    mesh: Mesh,
    normals: np.ndarray,
    areas: np.ndarray,
    *,
    nozzle_diameter_mm: float,
    current_wall_loops: int,
    sample_count: int,
    seed: int,
) -> ThinWallSummary:
    # Threshold: roughly what `current_wall_loops` perimeters of this
    # nozzle would need to physically close a wall. This is a stand-in for
    # the actual line-width setting; tune via `nozzle_diameter_mm` /
    # `current_wall_loops` if your profile uses a non-default line width.
    threshold_mm = max(nozzle_diameter_mm * max(current_wall_loops, 1), nozzle_diameter_mm)

    valid = areas > geo.EPS
    if not valid.any():
        return ThinWallSummary(checked_samples=0, thin_sample_count=0, min_thickness_mm=None)

    rng = np.random.default_rng(seed)
    face_indices = np.nonzero(valid)[0]
    probs = areas[face_indices] / areas[face_indices].sum()
    n = min(sample_count, face_indices.size * 4)
    sampled_faces = rng.choice(face_indices, size=n, p=probs)

    tris = geo.face_vertices(mesh)[sampled_faces]  # (n,3,3)
    # Random barycentric point per sampled face.
    r1 = rng.random(n)
    r2 = rng.random(n)
    sqrt_r1 = np.sqrt(r1)
    bary_a = 1 - sqrt_r1
    bary_b = sqrt_r1 * (1 - r2)
    bary_c = sqrt_r1 * r2
    points = (
        bary_a[:, None] * tris[:, 0]
        + bary_b[:, None] * tris[:, 1]
        + bary_c[:, None] * tris[:, 2]
    )

    face_normals = normals[sampled_faces]
    offset = 1e-4  # nudge inside the surface so the ray doesn't immediately re-hit its own face
    origins = points - face_normals * offset
    directions = -face_normals

    distances = geo.ray_mesh_first_hit_distance(origins, directions, mesh)
    finite = np.isfinite(distances)

    checked = int(finite.sum())
    if checked == 0:
        return ThinWallSummary(checked_samples=n, thin_sample_count=0, min_thickness_mm=None)

    thicknesses = distances[finite]
    thin_mask = thicknesses < threshold_mm
    worst_order = np.argsort(thicknesses)[: min(5, thicknesses.size)]
    worst_points = points[finite][worst_order]
    worst_thicknesses = thicknesses[worst_order]

    return ThinWallSummary(
        checked_samples=checked,
        thin_sample_count=int(thin_mask.sum()),
        min_thickness_mm=float(thicknesses.min()),
        worst_regions=[
            ThinWallRegion(location_mm=tuple(p.tolist()), thickness_mm=float(t))
            for p, t in zip(worst_points, worst_thicknesses)
        ],
    )


def _small_features(
    mesh: Mesh, components: list[np.ndarray], threshold_mm: float
) -> list[SmallFeature]:
    if len(components) <= 1:
        return []
    features = []
    for comp_faces in components:
        verts = mesh.vertices[mesh.faces[comp_faces]].reshape(-1, 3)
        size = verts.max(axis=0) - verts.min(axis=0)
        max_dim = float(size.max())
        if max_dim < threshold_mm:
            centroid = verts.mean(axis=0)
            features.append(
                SmallFeature(
                    location_mm=tuple(centroid.tolist()),
                    size_mm=max_dim,
                    description=(
                        f"Separate small body, largest dimension {max_dim:.2f} mm "
                        f"(below {threshold_mm:.2f} mm threshold). Verify this print will "
                        "hold up structurally / adhere reliably at this scale."
                    ),
                )
            )
    return features


def _fit_sensitive_features(mesh: Mesh) -> list[FitSensitiveFeature]:
    """Flags roughly-circular OPEN boundary loops as candidate holes.
    See module docstring: this does not find closed/watertight
    through-holes, only open ones — a real but partial detector."""
    features: list[FitSensitiveFeature] = []
    for loop in geo.boundary_loops(mesh):
        if loop.shape[0] < 6:
            continue  # too few points to call it a circle with any confidence
        pts = mesh.vertices[loop]
        centroid = pts.mean(axis=0)
        radii = np.linalg.norm(pts - centroid, axis=1)
        mean_r = float(radii.mean())
        if mean_r < geo.EPS:
            continue
        variation = float(radii.std() / mean_r)
        if variation < 0.15:  # roughly circular
            features.append(
                FitSensitiveFeature(
                    kind="hole",
                    location_mm=tuple(centroid.tolist()),
                    diameter_mm=mean_r * 2,
                    boundary_loop_length=int(loop.shape[0]),
                )
            )
    return features


def _orientation_candidates(
    mesh: Mesh, *, overhang_threshold_deg: float, top_k: int
) -> list[OrientationCandidate]:
    normals, areas = geo.face_normals_and_areas(mesh)
    valid = areas > geo.EPS
    if not valid.any():
        return []

    # Cluster faces by (quantized) normal direction to find candidate flat
    # resting facets, weighted by total area per direction.
    quantized = np.round(normals[valid], decimals=2)
    unique_dirs, inverse = np.unique(quantized, axis=0, return_inverse=True)
    dir_areas = np.zeros(unique_dirs.shape[0])
    valid_areas = areas[valid]
    np.add.at(dir_areas, inverse, valid_areas)

    order = np.argsort(-dir_areas)
    candidates_dirs = unique_dirs[order][: max(top_k * 3, top_k)]  # oversample, dedupe by score later

    results: list[OrientationCandidate] = []
    seen_desc = set()
    for d in candidates_dirs:
        norm = np.linalg.norm(d)
        if norm < 1e-6:
            continue
        d_unit = d / norm
        rest_face_area = float(dir_areas[np.argmin(np.linalg.norm(unique_dirs - d, axis=1))])

        rotation, _ = Rotation.align_vectors([[0, 0, -1]], [d_unit])
        rot_matrix = rotation.as_matrix()

        rotated_verts = mesh.vertices @ rot_matrix.T
        rotated_normals = normals @ rot_matrix.T
        rotated_tilts = geo.face_tilt_from_horizontal_deg(rotated_normals)

        bed_z = float(rotated_verts[:, 2].min())
        rotated_faces_z = rotated_verts[mesh.faces]
        face_min_z = rotated_faces_z[:, :, 2].min(axis=1)
        face_max_z = rotated_faces_z[:, :, 2].max(axis=1)

        on_bed = face_max_z <= bed_z + BED_CONTACT_EPS_MM
        bed_contact = float(areas[on_bed].sum())

        facing_down = rotated_normals[:, 2] < -1e-6
        above_bed = face_min_z > bed_z + BED_CONTACT_EPS_MM
        overhang_mask = above_bed & facing_down & (rotated_tilts < overhang_threshold_deg)
        overhang_area = float(areas[overhang_mask].sum())

        score = bed_contact - 0.5 * overhang_area
        desc_key = tuple(np.round(rot_matrix, 3).flatten().tolist())
        if desc_key in seen_desc:
            continue
        seen_desc.add(desc_key)

        results.append(
            OrientationCandidate(
                rotation_matrix=tuple(tuple(row) for row in rot_matrix.tolist()),
                description=(
                    f"Rest on facet with area {rest_face_area:.1f} mm^2 "
                    f"(normal {tuple(round(x, 2) for x in d_unit.tolist())} in the model's "
                    "original orientation)"
                ),
                rest_face_area_mm2=rest_face_area,
                overhang_area_mm2=overhang_area,
                bed_contact_area_mm2=bed_contact,
                score=score,
                notes=[
                    "Score = bed_contact_area - 0.5 * overhang_area (simple heuristic). "
                    "Does not simulate tipping/stability from center of mass."
                ],
            )
        )

    results.sort(key=lambda c: -c.score)
    return results[:top_k]
