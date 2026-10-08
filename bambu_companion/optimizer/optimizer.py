"""Print Optimizer: turns an AnalysisResult + PrintContext into a
SettingChange diff, following the project's rule that every change must
be tied to model geometry and the selected goal (see rules.py).

Anything the rules can't confidently turn into a specific setting value
goes into `uncertainties` instead of being silently invented — this
mirrors the memory bank's Failure Behavior: "Optimizer uncertainty: Flag
uncertainty. Ask for review instead of inventing a value."
"""

from __future__ import annotations

from ..profiles.bambu_settings import is_verified
from ..profiles.bambu_values import to_typed
from ..schemas import AnalysisResult, OptimizationResult, PrintContext, PrintGoal
from . import rules


def optimize(analysis: AnalysisResult, ctx: PrintContext) -> OptimizationResult:
    changes = []
    changes.extend(_maybe(rules.wall_loops(analysis, ctx)))
    changes.extend(_maybe(rules.layer_height(analysis, ctx)))
    changes.extend(_maybe(rules.outer_wall_speed(analysis, ctx)))
    changes.extend(rules.shell_layers(analysis, ctx))
    changes.extend(rules.infill(analysis, ctx))
    changes.extend(rules.support(analysis, ctx))
    changes.extend(rules.brim(analysis, ctx))
    changes.extend(rules.dimensional_compensation(analysis, ctx))

    uncertainties = _uncertainties(analysis, ctx, changes)

    return OptimizationResult(goal=ctx.goal, changes=changes, uncertainties=uncertainties)


def _maybe(change) -> list:
    return [change] if change is not None else []


def _uncertainties(analysis: AnalysisResult, ctx: PrintContext, changes: list) -> list[str]:
    notes: list[str] = []
    oh, br, tw = analysis.overhangs, analysis.bridges, analysis.thin_walls
    need = rules.support_need(analysis)
    current = {k: to_typed(k, v) for k, v in ctx.current_settings.items()}

    if need == rules.SUPPORT_SMALL_ONLY:
        note = (
            f"{oh.island_count} small overhanging region(s) found (largest "
            f"{oh.largest_island_area_mm2:.0f} mm^2, {oh.overhang_area_mm2:.0f} mm^2 in total"
            + (
                f", flat spans up to {br.longest_span_mm:.0f} mm"
                if br.bridge_face_count
                else ""
            )
            + "). Regions this size normally print without support, so none is recommended"
        )
        if current.get("enable_support") is True:
            note += (
                "; support is currently ON, which would leave a mark under each of them — "
                "consider turning it off for this print"
            )
        notes.append(note + ". Check the sliced preview if any of them is a visible face.")

    if need == rules.SUPPORT_NEEDED:
        if current.get("support_on_build_plate_only") is True:
            notes.append(
                "'Support on build plate only' is ON. Any overhanging region that sits above "
                "another part of the model (not above the bare plate) will get no support. "
                "This tool can't tell which case applies — check the sliced preview."
            )
        if not rules._material_family(ctx):
            notes.append(
                f"No tested support-gap value for material '{ctx.material}', so the support "
                "top Z distance was left alone — set it from the filament maker's guidance."
            )
        better = _better_orientation(analysis)
        if better:
            notes.append(better)

    if tw.checked_samples and tw.thin_sample_count / tw.checked_samples >= 0.05:
        notes.append(
            f"{tw.thin_sample_count} of {tw.checked_samples} sampled surface points are on "
            f"material thinner than two wall lines (thinnest {tw.min_thickness_mm:.2f} mm). "
            "That is either tapered edges (chamfers, countersinks — harmless) or genuinely "
            "thin walls, which this sampling can't tell apart. If the sliced preview shows "
            "gaps, switch Wall generator to Arachne or turn on Detect thin wall."
        )

    if ctx.goal == PrintGoal.VISUAL_QUALITY:
        notes.append(
            "Seam position decides where the seam line shows, and the right choice depends "
            "on which side of this model faces the viewer — something the geometry alone "
            "doesn't say. Set 'Seam position' (aligned / back / nearest / random) yourself."
        )

    if not analysis.is_watertight:
        notes.append(
            "The model isn't watertight (open edges or non-manifold geometry present, see "
            "warnings). Recommendations above assume valid geometry; consider mesh repair "
            "before relying on the thin-wall/hole/overhang numbers."
        )

    if analysis.fit_sensitive_features and ctx.goal != PrintGoal.DIMENSIONAL_ACCURACY:
        notes.append(
            f"{len(analysis.fit_sensitive_features)} candidate hole(s) detected, but the "
            f"selected goal is '{ctx.goal.value}', not dimensional accuracy — no XY hole "
            "compensation was recommended. Re-run with goal=dimensional_accuracy if fit matters here."
        )

    unverified_keys = sorted({c.key for c in changes if not is_verified(c.key)})
    if unverified_keys:
        notes.append(
            "These recommended keys are standard Bambu Studio settings but weren't "
            f"individually re-confirmed against your installed version this session: "
            f"{', '.join(unverified_keys)}. Spot-check them in your process JSON before "
            "applying."
        )

    return notes


def _better_orientation(analysis: AnalysisResult) -> str | None:
    """Mentions a resting orientation that would need far less support,
    if the analyzer found one. A note, never an automatic change: turning
    a part over also changes which face gets the bed texture and which
    way the layer lines run."""
    current_area = analysis.overhangs.overhang_area_mm2
    if current_area < 200 or not analysis.orientation_candidates:
        return None
    best = min(analysis.orientation_candidates, key=lambda c: c.overhang_area_mm2)
    if best.overhang_area_mm2 > 0.5 * current_area:
        return None
    return (
        f"As oriented, {current_area:.0f} mm^2 overhangs. A different resting face would cut "
        f"that to {best.overhang_area_mm2:.0f} mm^2 with {best.bed_contact_area_mm2:.0f} mm^2 "
        f"on the bed: {best.description}. Worth a look before printing with support — but "
        "it also changes which face shows the bed texture and the direction of the layer lines."
    )
