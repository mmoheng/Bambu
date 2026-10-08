"""Registry of the Bambu Studio process-setting keys this project is
allowed to read, recommend and change.

Two jobs:

1. **Allowlist.** Nothing outside this table is ever written into a
   temporary profile or a project file (see `profiles/project_3mf.py`
   and the connector's `apply_settings`). That is the code-level form of
   the Safety Rules: no G-code keys, no machine limits, no temperatures
   — only ordinary process settings a person would change in Bambu
   Studio's Process panel.
2. **Types.** Bambu Studio stores every value as a string (`"2"`,
   `"15%"`, `"1"`), and since 2.x some keys as one-element lists
   (`["200"]`). `kind` tells `profiles/bambu_values.py` how to convert
   between that and the plain Python values the optimizer reasons with.

`verified` distinguishes two tiers:

- verified=True: the key name and value format were read out of a real
  `Metadata/project_settings.config` written by Bambu Studio
  02.08.04.57 for a Bambu Lab A1 0.4 nozzle (2026-10-07). Every key in
  this table was present in that file.
- verified=False: not seen in a real file. Nothing is in this tier right
  now; it stays so a future key can be added honestly.

Key names do change between Bambu Studio releases, so "verified" means
"verified for that build", not "forever" — `project_3mf.describe_project`
reports which registry keys a given file is missing.
"""

from __future__ import annotations

from dataclasses import dataclass

# Value kinds understood by profiles/bambu_values.py
KIND_INT = "int"
KIND_FLOAT = "float"
KIND_BOOL = "bool"  # stored "1" / "0"
KIND_PERCENT = "percent"  # stored "15%"
KIND_ENUM = "enum"  # stored as a plain string


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    verified: bool
    category: str
    kind: str = KIND_ENUM
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


def _spec(key, label, category, kind, *, choices=(), lo=None, hi=None, verified=True) -> SettingSpec:
    return SettingSpec(key, label, verified, category, kind, tuple(choices), lo, hi)


SETTINGS: dict[str, SettingSpec] = {
    spec.key: spec
    for spec in [
        # --- quality -------------------------------------------------------
        _spec("layer_height", "Layer height", "quality", KIND_FLOAT, lo=0.04, hi=0.6),
        _spec("initial_layer_print_height", "First layer height", "quality", KIND_FLOAT, lo=0.1, hi=0.6),
        _spec(
            "seam_position",
            "Seam position",
            "quality",
            KIND_ENUM,
            choices=("nearest", "aligned", "back", "random"),
        ),
        _spec("wall_generator", "Wall generator", "quality", KIND_ENUM, choices=("classic", "arachne")),
        _spec("detect_thin_wall", "Detect thin wall", "quality", KIND_BOOL),
        # --- walls / shells / infill --------------------------------------
        _spec("wall_loops", "Wall loops", "walls", KIND_INT, lo=1, hi=12),
        _spec("top_shell_layers", "Top shell layers", "shells", KIND_INT, lo=0, hi=20),
        _spec("bottom_shell_layers", "Bottom shell layers", "shells", KIND_INT, lo=0, hi=20),
        _spec("top_shell_thickness", "Top shell thickness", "shells", KIND_FLOAT, lo=0, hi=5),
        _spec("bottom_shell_thickness", "Bottom shell thickness", "shells", KIND_FLOAT, lo=0, hi=5),
        _spec("sparse_infill_density", "Infill density", "infill", KIND_PERCENT, lo=0, hi=100),
        _spec("sparse_infill_pattern", "Infill pattern", "infill", KIND_ENUM),
        # --- speed ---------------------------------------------------------
        _spec("outer_wall_speed", "Outer wall speed", "speed", KIND_FLOAT, lo=5, hi=500),
        _spec("inner_wall_speed", "Inner wall speed", "speed", KIND_FLOAT, lo=5, hi=500),
        _spec("top_surface_speed", "Top surface speed", "speed", KIND_FLOAT, lo=5, hi=500),
        _spec("sparse_infill_speed", "Sparse infill speed", "speed", KIND_FLOAT, lo=5, hi=500),
        _spec("initial_layer_speed", "First layer speed", "speed", KIND_FLOAT, lo=5, hi=200),
        _spec("bridge_speed", "Bridge speed", "speed", KIND_FLOAT, lo=5, hi=200),
        _spec("support_speed", "Support speed", "speed", KIND_FLOAT, lo=5, hi=300),
        _spec("support_interface_speed", "Support interface speed", "speed", KIND_FLOAT, lo=5, hi=200),
        # --- support -------------------------------------------------------
        _spec("enable_support", "Enable support", "support", KIND_BOOL),
        _spec(
            "support_type",
            "Support type",
            "support",
            KIND_ENUM,
            choices=("normal(auto)", "tree(auto)", "normal(manual)", "tree(manual)"),
        ),
        _spec("support_style", "Support style", "support", KIND_ENUM),
        _spec("support_threshold_angle", "Support threshold angle", "support", KIND_INT, lo=0, hi=90),
        _spec("support_on_build_plate_only", "Support on build plate only", "support", KIND_BOOL),
        _spec("support_critical_regions_only", "Support critical regions only", "support", KIND_BOOL),
        _spec("support_remove_small_overhang", "Remove small overhangs", "support", KIND_BOOL),
        _spec("support_top_z_distance", "Support top Z distance", "support", KIND_FLOAT, lo=0, hi=1),
        _spec("support_bottom_z_distance", "Support bottom Z distance", "support", KIND_FLOAT, lo=0, hi=1),
        _spec("support_object_xy_distance", "Support/object XY distance", "support", KIND_FLOAT, lo=0, hi=3),
        _spec("support_interface_top_layers", "Support top interface layers", "support", KIND_INT, lo=0, hi=10),
        _spec("support_interface_bottom_layers", "Support bottom interface layers", "support", KIND_INT, lo=-1, hi=10),
        _spec("support_interface_spacing", "Support top interface spacing", "support", KIND_FLOAT, lo=0, hi=5),
        _spec("support_interface_pattern", "Support interface pattern", "support", KIND_ENUM),
        _spec("support_base_pattern", "Support base pattern", "support", KIND_ENUM),
        _spec("support_base_pattern_spacing", "Support base pattern spacing", "support", KIND_FLOAT, lo=0.5, hi=10),
        _spec("tree_support_branch_angle", "Tree support branch angle", "support", KIND_INT, lo=10, hi=60),
        # --- bridges -------------------------------------------------------
        _spec("thick_bridges", "Thick bridges", "bridges", KIND_BOOL),
        _spec("bridge_flow", "Bridge flow", "bridges", KIND_FLOAT, lo=0.5, hi=1.5),
        _spec("bridge_no_support", "Don't support bridges", "bridges", KIND_BOOL),
        _spec("max_bridge_length", "Max bridge length", "bridges", KIND_FLOAT, lo=0, hi=200),
        # --- adhesion ------------------------------------------------------
        _spec("brim_width", "Brim width", "adhesion", KIND_FLOAT, lo=0, hi=30),
        _spec(
            "brim_type",
            "Brim type",
            "adhesion",
            KIND_ENUM,
            choices=("auto_brim", "outer_only", "inner_only", "outer_and_inner", "no_brim"),
        ),
        _spec("brim_object_gap", "Brim-object gap", "adhesion", KIND_FLOAT, lo=0, hi=2),
        # --- compensation --------------------------------------------------
        _spec("elefant_foot_compensation", "Elephant foot compensation", "compensation", KIND_FLOAT, lo=0, hi=1),
        _spec("xy_contour_compensation", "XY contour compensation", "compensation", KIND_FLOAT, lo=-1, hi=1),
        _spec("xy_hole_compensation", "XY hole compensation", "compensation", KIND_FLOAT, lo=-1, hi=1),
    ]
}


def label_for(key: str) -> str:
    spec = SETTINGS.get(key)
    return spec.label if spec else key


def is_verified(key: str) -> bool:
    spec = SETTINGS.get(key)
    return bool(spec and spec.verified)


def is_known(key: str) -> bool:
    """True if this project is allowed to change `key` at all."""
    return key in SETTINGS
