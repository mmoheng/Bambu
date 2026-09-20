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

from ..profiles.bambu_settings import label_for
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


def _change(ctx: PrintContext, key: str, recommended, reason: str) -> SettingChange | None:
    current = ctx.current_settings.get(key)
    if current == recommended:
        return None
    return SettingChange(
        key=key,
        label=label_for(key),
        current_value=current,
        recommended_value=recommended,
        reason=reason,
    )


def wall_loops(analysis: AnalysisResult, ctx: PrintContext) -> SettingChange | None:
    baseline = _WALL_LOOPS_BY_GOAL[ctx.goal]
    tw = analysis.thin_walls
    reason = f"goal is '{ctx.goal.value}'"
    recommended = baseline
    if tw.min_thickness_mm is not None:
        # If the thinnest sampled region can't fit `baseline` loops at
        # this nozzle, drop to what the geometry can actually support
        # rather than recommending a wall count the part can't hold.
        max_loops_that_fit = max(int(tw.min_thickness_mm // ctx.nozzle_diameter_mm), 1)
        if max_loops_that_fit < baseline:
            recommended = max_loops_that_fit
            reason = (
                f"goal is '{ctx.goal.value}' (would normally suggest {baseline} loops), but "
                f"the thinnest sampled wall region is {tw.min_thickness_mm:.2f} mm — at your "
                f"{ctx.nozzle_diameter_mm} mm nozzle that only fits {max_loops_that_fit} loop(s) "
                "without the perimeters overlapping/failing to close"
            )
        else:
            reason = (
                f"goal is '{ctx.goal.value}'; thinnest sampled wall region "
                f"({tw.min_thickness_mm:.2f} mm) comfortably fits {baseline} loops at your "
                f"{ctx.nozzle_diameter_mm} mm nozzle"
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
    if ctx.goal == PrintGoal.STRENGTH and analysis.thin_walls.thin_sample_count > 0:
        density_reason += (
            f"; {analysis.thin_walls.thin_sample_count}/{analysis.thin_walls.checked_samples} "
            "sampled regions came back thin, so infill is doing more of the structural work here"
        )
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


def support(analysis: AnalysisResult, ctx: PrintContext) -> list[SettingChange]:
    oh = analysis.overhangs
    if oh.overhang_face_count == 0:
        return []
    changes = []
    changes.append(
        _change(
            ctx,
            "enable_support",
            True,
            f"detected {oh.overhang_face_count} overhang faces totaling "
            f"{oh.overhang_area_mm2:.1f} mm^2 with worst face angle "
            f"{oh.worst_face_tilt_deg:.1f} deg from horizontal (below the "
            f"{oh.threshold_deg:.0f} deg check threshold)",
        )
    )
    # Set the threshold a little above the worst detected angle so supports
    # actually trigger where they're needed, without also catching every
    # near-vertical wall.
    suggested_threshold = min(oh.threshold_deg, round(oh.worst_face_tilt_deg + 10))
    changes.append(
        _change(
            ctx,
            "support_threshold_angle",
            suggested_threshold,
            f"worst detected overhang face is {oh.worst_face_tilt_deg:.1f} deg from "
            f"horizontal; setting the threshold to {suggested_threshold} deg targets that "
            "region without over-supporting the rest of the model",
        )
    )
    return [c for c in changes if c]


def brim(analysis: AnalysisResult, ctx: PrintContext) -> list[SettingChange]:
    bbox = analysis.bounding_box
    footprint_area = bbox.size[0] * bbox.size[1]
    height = bbox.size[2]
    contact_ratio = (analysis.bed_contact_area_mm2 / footprint_area) if footprint_area > 0 else 1.0
    tall_and_thin = height > 0 and (height / max(bbox.size[0], bbox.size[1], 1e-6)) > 2.0

    changes = []
    if contact_ratio < 0.3 or tall_and_thin:
        width = 5.0 if tall_and_thin else 3.0
        changes.append(
            _change(
                ctx,
                "brim_width",
                width,
                f"bed contact is only {analysis.bed_contact_area_mm2:.0f} mm^2 against a "
                f"{footprint_area:.0f} mm^2 footprint ({contact_ratio:.0%} coverage)"
                + (f", and the part is tall/thin ({height:.0f} mm tall)" if tall_and_thin else "")
                + " — added brim reduces warping/tip-over risk",
            )
        )
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
