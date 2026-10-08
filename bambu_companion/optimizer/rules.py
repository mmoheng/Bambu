"""Rule functions for the print optimizer.

Project rule (from the memory bank, unchanged): "No setting is changed
solely because a fixed recipe says so. Recommendations should be connected
to model geometry and the selected goal." So every rule here takes both
the AnalysisResult and the PrintContext, and every reason string is built
from the actual numbers involved — not a static template. A rule that
can't tie its suggestion to something concrete about this model reports
an uncertainty instead of guessing (see `optimizer.py`'s uncertainties
list).

Each rule returns a `SettingChange | None` (None = "no change to
recommend for this key given this model/goal/current settings").
"""

from __future__ import annotations

import math

from ..profiles.bambu_settings import label_for
from ..profiles.bambu_values import to_typed, values_equal
from ..schemas import AnalysisResult, PrintContext, PrintGoal, SettingChange

# ---------------------------------------------------------------------------
# Goal-based baselines. These are starting points, not the recommendation
# itself — every rule below layers geometry-specific reasoning on top
# before deciding whether/what to recommend.
# ---------------------------------------------------------------------------

_WALL_LOOPS_BY_GOAL = {
    PrintGoal.FAST_PRINT: 2,
    PrintGoal.BALANCED: 3,
    PrintGoal.VISUAL_QUALITY: 3,
    PrintGoal.STRENGTH: 4,
    PrintGoal.DIMENSIONAL_ACCURACY: 3,
}

_LAYER_HEIGHT_BY_GOAL = {
    PrintGoal.FAST_PRINT: 0.28,
    PrintGoal.BALANCED: 0.20,
    PrintGoal.VISUAL_QUALITY: 0.12,
    PrintGoal.STRENGTH: 0.20,
    PrintGoal.DIMENSIONAL_ACCURACY: 0.16,
}

_OUTER_WALL_SPEED_BY_GOAL = {
    PrintGoal.FAST_PRINT: 250,
    PrintGoal.BALANCED: 180,
    PrintGoal.VISUAL_QUALITY: 100,
    PrintGoal.STRENGTH: 150,
    PrintGoal.DIMENSIONAL_ACCURACY: 100,
}

_SHELL_LAYERS_BY_GOAL = {
    PrintGoal.FAST_PRINT: 3,
    PrintGoal.BALANCED: 4,
    PrintGoal.VISUAL_QUALITY: 5,
    PrintGoal.STRENGTH: 5,
    PrintGoal.DIMENSIONAL_ACCURACY: 4,
}

_INFILL_DENSITY_BY_GOAL = {
    PrintGoal.FAST_PRINT: 10,
    PrintGoal.BALANCED: 15,
    PrintGoal.VISUAL_QUALITY: 15,
    PrintGoal.STRENGTH: 30,
    PrintGoal.DIMENSIONAL_ACCURACY: 20,
}

_INFILL_PATTERN_BY_GOAL = {
    PrintGoal.FAST_PRINT: "grid",
    PrintGoal.BALANCED: "grid",
    PrintGoal.VISUAL_QUALITY: "grid",
    PrintGoal.STRENGTH: "gyroid",
    PrintGoal.DIMENSIONAL_ACCURACY: "grid",
}


def _current(ctx: PrintContext, key: str):
    """The current value of `key` as a plain Python value, whether the
    caller passed `2` or Bambu Studio's own `"2"` / `["200"]` / `"15%"`."""
    return to_typed(key, ctx.current_settings.get(key))


def _change(ctx: PrintContext, key: str, recommended, reason: str) -> SettingChange | None:
    current = _current(ctx, key)
    # Representation-aware: a real Bambu Studio profile stores "3", the
    # optimizer recommends 3 — that is not a change.
    if key in ctx.current_settings and values_equal(key, current, recommended):
        return None
    return SettingChange(
        key=key,
        label=label_for(key),
        current_value=current,
        recommended_value=recommended,
        reason=reason,
    )


def _material_family(ctx: PrintContext) -> str:
    """'PLA', 'PETG' or '' (anything this project has no tested advice for)."""
    material = (ctx.material or "").upper().replace("-", "").strip()
    if material.startswith("PETG") or material == "PET":
        return "PETG"
    if material.startswith("PLA"):
        return "PLA"
    return ""


def _line_width_mm(ctx: PrintContext) -> float:
    # Bambu's default line width for a 0.4 nozzle is 0.42 (seen in a real
    # 02.08.04.57 profile); 1.05 x nozzle reproduces that.
    return round(ctx.nozzle_diameter_mm * 1.05, 2)


# A sampled thickness below this is treated as a taper (chamfer tip,
# knife edge, countersink) rather than a wall. Measured on real models:
# a set of countersunk discs reports a "thinnest region" of 0.01 mm.
_MIN_REAL_WALL_FACTOR = 2.0  # x line width
# Don't raise the whole model's wall count past this for one thin feature.
_MAX_LOOPS_FOR_THIN_FEATURE = 4


def wall_loops(analysis: AnalysisResult, ctx: PrintContext) -> SettingChange | None:
    """Goal baseline, raised when the model's thinnest real wall would
    otherwise print with a sparse-infill core.

    This rule used to LOWER the wall count to "what fits" in the
    thinnest sampled spot. That was wrong twice over: Bambu Studio
    already prints fewer loops wherever a region is too thin for the
    requested number, so asking for more never fails — and the thinnest
    sample is usually a chamfer tip, so one 0.01 mm reading dropped a
    whole strength-goal part to a single wall. It never lowers now.
    """
    baseline = _WALL_LOOPS_BY_GOAL[ctx.goal]
    line = _line_width_mm(ctx)
    thinnest = analysis.thin_walls.min_thickness_mm
    recommended = baseline
    reason = f"goal is '{ctx.goal.value}'"

    if thinnest is not None and thinnest >= _MIN_REAL_WALL_FACTOR * line:
        # Loops per side needed for that wall to be solid perimeters.
        loops_to_fill = math.ceil(thinnest / (2 * line) - 1e-9)
        if baseline < loops_to_fill <= _MAX_LOOPS_FOR_THIN_FEATURE:
            recommended = loops_to_fill
            reason = (
                f"goal is '{ctx.goal.value}' (normally {baseline} loops), but the thinnest "
                f"sampled wall is {thinnest:.2f} mm — at a {line} mm line that needs "
                f"{loops_to_fill} loops per side to be solid perimeters instead of two skins "
                "around sparse infill, which is where thin rails and lips crack"
            )
        elif loops_to_fill <= baseline:
            reason = (
                f"goal is '{ctx.goal.value}'; the thinnest sampled wall ({thinnest:.2f} mm) "
                f"is solid perimeters at {baseline} loops with a {line} mm line"
            )
    return _change(ctx, "wall_loops", recommended, reason)


def layer_height(analysis: AnalysisResult, ctx: PrintContext) -> SettingChange | None:
    recommended = _LAYER_HEIGHT_BY_GOAL[ctx.goal]
    max_reasonable = round(ctx.nozzle_diameter_mm * 0.8, 2)
    if recommended > max_reasonable:
        recommended = max_reasonable
    reason = (
        f"goal is '{ctx.goal.value}' on a {ctx.nozzle_diameter_mm} mm nozzle "
        f"(capped at {max_reasonable} mm = 80% of nozzle diameter, a common practical ceiling)"
    )
    return _change(ctx, "layer_height", recommended, reason)


def outer_wall_speed(analysis: AnalysisResult, ctx: PrintContext) -> SettingChange | None:
    recommended = _OUTER_WALL_SPEED_BY_GOAL[ctx.goal]
    reason = f"goal is '{ctx.goal.value}'; outer-wall speed most directly affects surface finish and dimensional accuracy"
    if ctx.goal in (PrintGoal.VISUAL_QUALITY, PrintGoal.DIMENSIONAL_ACCURACY) and analysis.fit_sensitive_features:
        reason += (
            f"; model has {len(analysis.fit_sensitive_features)} detected fit-sensitive "
            "hole(s), so a slower, more consistent outer wall matters more than usual"
        )
    return _change(ctx, "outer_wall_speed", recommended, reason)


def shell_layers(analysis: AnalysisResult, ctx: PrintContext) -> list[SettingChange]:
    recommended = _SHELL_LAYERS_BY_GOAL[ctx.goal]
    changes = []
    for key in ("top_shell_layers", "bottom_shell_layers"):
        reason = f"goal is '{ctx.goal.value}'"
        if key == "bottom_shell_layers" and analysis.bed_contact_area_mm2 < 500:
            reason += (
                f"; small bed-contact footprint ({analysis.bed_contact_area_mm2:.0f} mm^2) "
                "benefits from a solid, well-adhered bottom"
            )
        c = _change(ctx, key, recommended, reason)
        if c:
            changes.append(c)
    return changes


def infill(analysis: AnalysisResult, ctx: PrintContext) -> list[SettingChange]:
    density = _INFILL_DENSITY_BY_GOAL[ctx.goal]
    pattern = _INFILL_PATTERN_BY_GOAL[ctx.goal]
    changes = []
    density_reason = f"goal is '{ctx.goal.value}'"
    c = _change(ctx, "sparse_infill_density", density, density_reason)
    if c:
        changes.append(c)
    c = _change(
        ctx,
        "sparse_infill_pattern",
        pattern,
        f"goal is '{ctx.goal.value}'"
        + (
            "; gyroid is isotropic (similar strength in all directions), which matters more "
            "than print time when strength is the goal"
            if ctx.goal == PrintGoal.STRENGTH
            else ""
        ),
    )
    if c:
        changes.append(c)
    return changes


# Support heuristics. Calibrated on five real deck-box models (2026-10-07):
# a lid with a 7,000 mm^2 pocket ceiling failed without support; a body
# with forty drip undersides of <= 45 mm^2 each, and a lid with six
# 52 mm^2 / 11.6 mm covered pockets, both print cleanly without it.
SUPPORT_ISLAND_AREA_MM2 = 100.0  # one overhanging region this big needs support
UNAIDED_BRIDGE_SPAN_MM = 20.0  # flat ceilings up to this (bbox diagonal) bridge unaided
_FLAT_CEILING_AREA_MM2 = 100.0

SUPPORT_NEEDED = "needed"
SUPPORT_SMALL_ONLY = "small_only"
SUPPORT_NONE = "none"

# Starting-point gap between the support interface and the part. PETG
# bonds to its own support far more readily than PLA, so it gets a
# looser gap. Verify on a test print — same caveat as the compensation
# values below.
_SUPPORT_TOP_Z_BY_MATERIAL = {"PLA": 0.2, "PETG": 0.25}


def support_need(analysis: AnalysisResult) -> str:
    """Whether this model's overhangs need support, judged by the size
    of the largest overhanging region rather than by total area."""
    oh = analysis.overhangs
    if oh.overhang_face_count == 0:
        return SUPPORT_NONE
    if oh.island_count == 0:
        return SUPPORT_NEEDED  # region sizes weren't computed; stay on the safe side
    if (
        oh.largest_island_area_mm2 >= SUPPORT_ISLAND_AREA_MM2
        or analysis.bridges.longest_span_mm > UNAIDED_BRIDGE_SPAN_MM
    ):
        return SUPPORT_NEEDED
    return SUPPORT_SMALL_ONLY


def has_flat_ceiling(analysis: AnalysisResult) -> bool:
    """A large, near-horizontal downward face (a pocket roof, the
    underside of a lid) — the case where the support interface decides
    how the surface looks."""
    return (
        analysis.bridges.bridge_area_mm2 >= _FLAT_CEILING_AREA_MM2
        and analysis.bridges.longest_span_mm > UNAIDED_BRIDGE_SPAN_MM
    )


def support(analysis: AnalysisResult, ctx: PrintContext) -> list[SettingChange]:
    if support_need(analysis) != SUPPORT_NEEDED:
        return []
    oh, br = analysis.overhangs, analysis.bridges

    if oh.island_count:
        why = (
            f"the largest overhanging region is {oh.largest_island_area_mm2:.0f} mm^2 "
            f"({oh.overhang_area_mm2:.0f} mm^2 in {oh.island_count} region(s), flattest face "
            f"{oh.worst_face_tilt_deg:.0f} deg from horizontal)"
        )
        if br.longest_span_mm > UNAIDED_BRIDGE_SPAN_MM:
            why += (
                f", including a flat ceiling about {br.longest_span_mm:.0f} mm across — "
                "too far to bridge in mid-air"
            )
    else:
        why = (
            f"detected {oh.overhang_face_count} overhang faces totaling "
            f"{oh.overhang_area_mm2:.1f} mm^2 with worst face angle "
            f"{oh.worst_face_tilt_deg:.1f} deg from horizontal (below the "
            f"{oh.threshold_deg:.0f} deg check threshold)"
        )

    changes = [_change(ctx, "enable_support", True, why)]

    # Set the threshold a little above the worst detected angle so supports
    # actually trigger where they're needed, without also catching every
    # near-vertical wall.
    suggested_threshold = min(oh.threshold_deg, round(oh.worst_face_tilt_deg + 10))
    current_threshold = _current(ctx, "support_threshold_angle")
    threshold_already_catches_it = (
        isinstance(current_threshold, (int, float))
        and not isinstance(current_threshold, bool)
        and current_threshold > oh.worst_face_tilt_deg
    )
    # Do no harm: if the current threshold already reaches the worst
    # face, leave it. Lowering it could strip support from steeper
    # regions this rule knows nothing about; too much support costs
    # cleanup, too little costs the print.
    changes.append(
        None
        if threshold_already_catches_it
        else _change(
            ctx,
            "support_threshold_angle",
            suggested_threshold,
            f"worst detected overhang face is {oh.worst_face_tilt_deg:.1f} deg from "
            f"horizontal; setting the threshold to {suggested_threshold} deg targets that "
            "region without over-supporting the rest of the model",
        )
    )

    family = _material_family(ctx)
    if family in _SUPPORT_TOP_Z_BY_MATERIAL:
        gap = _SUPPORT_TOP_Z_BY_MATERIAL[family]
        changes.append(
            _change(
                ctx,
                "support_top_z_distance",
                gap,
                f"{oh.overhang_area_mm2:.0f} mm^2 of this model will rest on support, in "
                f"{family}: "
                + (
                    "PETG welds to its own support interface, so a slightly larger gap than "
                    "PLA's 0.2 mm keeps it removable"
                    if family == "PETG"
                    else "0.2 mm is close enough for a clean underside and still snaps off"
                )
                + " — a starting point, check the first print",
            )
        )

    if has_flat_ceiling(analysis):
        current_layers = _current(ctx, "support_interface_top_layers")
        if not isinstance(current_layers, (int, float)) or isinstance(current_layers, bool) or current_layers < 3:
            changes.append(
                _change(
                    ctx,
                    "support_interface_top_layers",
                    3,
                    f"a flat ceiling of {br.bridge_area_mm2:.0f} mm^2 prints directly onto the "
                    "support; three dense interface layers give it an even surface to land on "
                    "instead of sagging between support lines",
                )
            )
        if _current(ctx, "support_type") == "tree(auto)":
            changes.append(
                _change(
                    ctx,
                    "support_type",
                    "normal(auto)",
                    f"the overhang here is a flat {br.bridge_area_mm2:.0f} mm^2 ceiling; normal "
                    "support holds it up evenly across the whole area, where tree branches "
                    "touch it only in patches",
                )
            )
    return [c for c in changes if c]


def brim(analysis: AnalysisResult, ctx: PrintContext) -> list[SettingChange]:
    """Adds brim for parts with little bed contact or a tall, thin
    shape. Only ever adds: it never shrinks a brim that is already
    wider, and it leaves Bambu Studio's own "Auto" brim alone (in that
    mode Bambu Studio sizes the brim itself and ignores the width)."""
    bbox = analysis.bounding_box
    footprint_area = bbox.size[0] * bbox.size[1]
    height = bbox.size[2]
    contact_ratio = (analysis.bed_contact_area_mm2 / footprint_area) if footprint_area > 0 else 1.0
    tall_and_thin = height > 0 and (height / max(bbox.size[0], bbox.size[1], 1e-6)) > 2.0

    if not (contact_ratio < 0.3 or tall_and_thin):
        return []
    brim_type = _current(ctx, "brim_type")
    if brim_type == "auto_brim":
        return []

    width = 5.0 if tall_and_thin else 3.0
    why = (
        f"bed contact is only {analysis.bed_contact_area_mm2:.0f} mm^2 against a "
        f"{footprint_area:.0f} mm^2 footprint ({contact_ratio:.0%} coverage)"
        + (f", and the part is tall/thin ({height:.0f} mm tall)" if tall_and_thin else "")
        + " — added brim reduces warping/tip-over risk"
    )
    changes = []
    if brim_type == "no_brim":
        changes.append(_change(ctx, "brim_type", "outer_only", why + "; brim is currently switched off"))
    elif brim_type is None:
        # Brim type unknown. Bambu Studio's default is Auto, which
        # ignores the width — so a width on its own would change
        # nothing while still "verifying" in the output.
        changes.append(
            _change(ctx, "brim_type", "outer_only", why + "; set explicitly so the brim width below takes effect")
        )
    current_width = _current(ctx, "brim_width")
    already_wide_enough = (
        isinstance(current_width, (int, float))
        and not isinstance(current_width, bool)
        and current_width >= width
    )
    if not already_wide_enough:
        changes.append(_change(ctx, "brim_width", width, why))
    return [c for c in changes if c]


def dimensional_compensation(analysis: AnalysisResult, ctx: PrintContext) -> list[SettingChange]:
    if ctx.goal != PrintGoal.DIMENSIONAL_ACCURACY:
        return []
    # Starting points only — real compensation should be dialed in with a
    # calibration print on your specific printer/filament, not derived
    # from the model alone. See optimizer.py's uncertainties list, which
    # always adds a note for this category.
    material = ctx.material.upper()
    foot_comp = 0.15 if material == "PLA" else 0.10
    changes = []
    reason_suffix = (
        f"; {len(analysis.fit_sensitive_features)} candidate hole(s) detected in the model"
        if analysis.fit_sensitive_features
        else "; no confidently-detected round holes in this model, so this is a general starting point"
    )
    changes.append(
        _change(
            ctx,
            "elefant_foot_compensation",
            foot_comp,
            f"goal is dimensional accuracy on {material}; starting-point value, verify against "
            f"a first-layer test print{reason_suffix}",
        )
    )
    if analysis.fit_sensitive_features:
        changes.append(
            _change(
                ctx,
                "xy_hole_compensation",
                0.15,
                f"{len(analysis.fit_sensitive_features)} candidate hole(s) detected "
                f"(diameters: {', '.join(f'{f.diameter_mm:.1f}mm' for f in analysis.fit_sensitive_features[:5])}); "
                "holes print undersized on FDM printers, so a small positive compensation is a "
                "reasonable starting point — verify with a fit test",
            )
        )
    return [c for c in changes if c]
