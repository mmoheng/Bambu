"""Shared data structures for the Bambu Companion model analyzer, print
optimizer, and (eventually) the bridge/CLI runner/ChatGPT-facing layers.

These are plain dataclasses with no external dependencies so every other
module can import from here without pulling in numpy, a web framework, or
anything platform-specific. Only ``model_analyzer`` and ``optimizer``
actually populate these today; the bridge/server modules are stubs (see
bambu_companion/bridge/README.md) that will produce/consume the same
shapes once they're built out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

Vec3 = tuple[float, float, float]
Mat3 = tuple[Vec3, Vec3, Vec3]


class PrintGoal(str, Enum):
    """The five optimization goals from the project spec
    (see Per-Print Optimization Workflow in the memory bank)."""

    VISUAL_QUALITY = "visual_quality"
    STRENGTH = "strength"
    FAST_PRINT = "fast_print"
    BALANCED = "balanced"
    DIMENSIONAL_ACCURACY = "dimensional_accuracy"


# ---------------------------------------------------------------------------
# Model Analyzer outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundingBox:
    min: Vec3
    max: Vec3

    @property
    def size(self) -> Vec3:
        return (
            self.max[0] - self.min[0],
            self.max[1] - self.min[1],
            self.max[2] - self.min[2],
        )


@dataclass(frozen=True)
class BedFitResult:
    fits: bool
    build_volume_mm: Vec3
    model_size_mm: Vec3
    margin_mm: Vec3  # how much room is left on each axis; negative = overflow
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OverhangSummary:
    threshold_deg: float
    overhang_area_mm2: float
    overhang_face_count: int
    worst_face_tilt_deg: Optional[float]  # 0 = flat overhang, 90 = vertical wall
    # Overhang faces grouped into connected regions ("islands"). One big
    # region needs support; forty 5 mm^2 ones (drips, text, small chamfers)
    # usually don't — the total area alone can't tell those apart.
    # island_count == 0 with overhang_face_count > 0 means "not computed"
    # (an AnalysisResult built by hand or by an older version).
    island_count: int = 0
    largest_island_area_mm2: float = 0.0


@dataclass(frozen=True)
class BridgeSummary:
    bridge_face_count: int
    bridge_area_mm2: float
    longest_span_mm: float


@dataclass(frozen=True)
class ThinWallRegion:
    location_mm: Vec3
    thickness_mm: float


@dataclass(frozen=True)
class ThinWallSummary:
    checked_samples: int
    thin_sample_count: int
    min_thickness_mm: Optional[float]
    worst_regions: list[ThinWallRegion] = field(default_factory=list)


@dataclass(frozen=True)
class SmallFeature:
    location_mm: Vec3
    size_mm: float
    description: str


@dataclass(frozen=True)
class FitSensitiveFeature:
    kind: str  # "hole" today; "boss" reserved for future use
    location_mm: Vec3
    diameter_mm: float
    boundary_loop_length: int


@dataclass(frozen=True)
class OrientationCandidate:
    rotation_matrix: Mat3
    description: str
    rest_face_area_mm2: float
    overhang_area_mm2: float
    bed_contact_area_mm2: float
    score: float  # higher is better; see optimizer/orientation ranking notes
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GeometryWarning:
    kind: str  # "non_manifold_edges", "open_boundary", "multiple_bodies", ...
    detail: str


@dataclass
class AnalysisResult:
    source_file: str
    bounding_box: BoundingBox
    bed_fit: BedFitResult
    bed_contact_area_mm2: float
    overhangs: OverhangSummary
    bridges: BridgeSummary
    thin_walls: ThinWallSummary
    small_features: list[SmallFeature]
    fit_sensitive_features: list[FitSensitiveFeature]
    orientation_candidates: list[OrientationCandidate]
    warnings: list[GeometryWarning]
    body_count: int
    is_watertight: bool
    face_count: int
    vertex_count: int


# ---------------------------------------------------------------------------
# Optimizer inputs / outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrintContext:
    printer: str
    nozzle_diameter_mm: float
    material: str  # "PLA", "PETG", ...
    goal: PrintGoal
    current_settings: dict[str, Any]
    ams_slot: Optional[str] = None


@dataclass(frozen=True)
class SettingChange:
    key: str
    label: str
    current_value: Any
    recommended_value: Any
    reason: str


@dataclass(frozen=True)
class OptimizationResult:
    goal: PrintGoal
    changes: list[SettingChange]
    uncertainties: list[str] = field(default_factory=list)
