"""Registry of Bambu Studio process-setting keys the optimizer knows how
to recommend, with a `verified` flag distinguishing two tiers:

- verified=True: confirmed against Bambu Studio's own GitHub issue
  tracker / profile discussion during this project's design review
  (2026-09-20) — see the memory bank's "Known Bambu Studio setting keys"
  section.
- verified=False: standard, widely-documented Bambu Studio/PrusaSlicer-
  lineage keys that are very likely correct (seen consistently across
  community configs and slicer forks) but were NOT individually
  re-confirmed against Bambu's source this session. Sanity-check these
  against the process JSON your installed Bambu Studio version actually
  uses (Help > Show Configuration Folder, or export a profile and look at
  the JSON) before trusting them in production.

This registry exists so the optimizer's output can honestly flag which
tier a given recommendation's key name falls into, rather than presenting
every key with equal confidence.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    verified: bool
    category: str


SETTINGS: dict[str, SettingSpec] = {
    spec.key: spec
    for spec in [
        SettingSpec("wall_loops", "Wall loops", True, "walls"),
        SettingSpec("top_shell_layers", "Top shell layers", True, "shells"),
        SettingSpec("bottom_shell_layers", "Bottom shell layers", True, "shells"),
        SettingSpec("sparse_infill_density", "Infill density", True, "infill"),
        SettingSpec("sparse_infill_pattern", "Infill pattern", True, "infill"),
        SettingSpec("support_threshold_angle", "Support threshold angle", True, "support"),
        SettingSpec("elefant_foot_compensation", "Elephant foot compensation", True, "compensation"),
        SettingSpec("xy_contour_compensation", "XY contour compensation", True, "compensation"),
        SettingSpec("xy_hole_compensation", "XY hole compensation", True, "compensation"),
        SettingSpec("layer_height", "Layer height", False, "quality"),
        SettingSpec("outer_wall_speed", "Outer wall speed", False, "speed"),
        SettingSpec("inner_wall_speed", "Inner wall speed", False, "speed"),
        SettingSpec("top_surface_speed", "Top surface speed", False, "speed"),
        SettingSpec("enable_support", "Enable support", False, "support"),
        SettingSpec("brim_width", "Brim width", False, "adhesion"),
        SettingSpec("brim_type", "Brim type", False, "adhesion"),
    ]
}


def label_for(key: str) -> str:
    spec = SETTINGS.get(key)
    return spec.label if spec else key


def is_verified(key: str) -> bool:
    spec = SETTINGS.get(key)
    return bool(spec and spec.verified)
