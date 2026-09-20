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

    if analysis.bridges.bridge_face_count > 0:
        notes.append(
            f"{analysis.bridges.bridge_face_count} bridge faces detected "
            f"({analysis.bridges.bridge_area_mm2:.0f} mm^2, longest span "
            f"~{analysis.bridges.longest_span_mm:.0f} mm). Bridge-specific cooling/speed "
            "keys weren't in this project's verified setting list, so no automatic change is "
            "recommended — review bridge cooling/fan settings manually for this model."
        )

    if ctx.goal == PrintGoal.VISUAL_QUALITY:
        notes.append(
            "Seam placement affects visible quality but this project hasn't verified the exact "
            "Bambu Studio key/values for seam position against the current install — review the "
            "seam setting manually rather than trusting an unverified key here."
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
