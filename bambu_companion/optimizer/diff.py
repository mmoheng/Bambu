"""Formats an OptimizationResult as the "Current -> Recommended" table +
reasons shown in the memory bank's Approval Workflow example, and as a
plain dict for anything (a CLI, a future ChatGPT tool response, a test)
that wants structured data instead of text.
"""

from __future__ import annotations

from ..schemas import OptimizationResult


def format_diff_table(result: OptimizationResult) -> str:
    if not result.changes:
        lines = [f"No changes recommended for goal '{result.goal.value}'."]
    else:
        lines = [f"Goal: {result.goal.value}", "", "Current -> Recommended", ""]
        for c in result.changes:
            lines.append(f"{c.label}: {c.current_value} -> {c.recommended_value}")
        lines.append("")
        lines.append("Reasons:")
        for c in result.changes:
            lines.append(f"- {c.label}: {c.reason}")

    if result.uncertainties:
        lines.append("")
        lines.append("Flagged for your review (not auto-applied):")
        for note in result.uncertainties:
            lines.append(f"- {note}")

    return "\n".join(lines)


def to_dict(result: OptimizationResult) -> dict:
    return {
        "goal": result.goal.value,
        "changes": [
            {
                "key": c.key,
                "label": c.label,
                "current_value": c.current_value,
                "recommended_value": c.recommended_value,
                "reason": c.reason,
            }
            for c in result.changes
        ],
        "uncertainties": list(result.uncertainties),
    }
