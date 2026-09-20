"""Implements the memory bank's "Temporary Profile Rule":

    1. Read the starting settings.
    2. Create a temporary job/profile copy.
    3. Apply only user-approved recommendations to that copy.
    4. Slice/export from the temporary copy.
    5. Leave normal saved Bambu presets unchanged.

This module only handles steps 1-3 (the pure data transformation — read a
settings dict, apply an approved subset of SettingChanges, return a new
dict). It never touches disk or Bambu Studio's real preset files; that's
the Bambu Studio Runner's job (bridge/studio_runner.py), which is what
actually writes this dict out to a temp process JSON and hands it to the
CLI. Keeping this step pure makes it trivially unit-testable and keeps
the "never overwrite the normal presets" guarantee obvious: this function
has no way to write anywhere.
"""

from __future__ import annotations

from ..schemas import SettingChange


class ApprovalError(ValueError):
    """Raised when the approved-changes list doesn't match what was proposed."""


def apply_changes(
    base_settings: dict,
    proposed_changes: list[SettingChange],
    approved_keys: set[str],
) -> dict:
    """Returns a NEW settings dict: a copy of `base_settings` with only the
    proposed changes whose `key` is in `approved_keys` applied.

    `approved_keys` should be exactly the set of setting keys the user
    said yes to (e.g. from the ChatGPT-side approval step) — anything
    proposed but not approved is left at its original value. Approving a
    key that wasn't actually proposed is treated as an error rather than
    silently ignored, since that likely means the caller is out of sync
    with what was actually shown to the user.
    """
    proposed_keys = {c.key for c in proposed_changes}
    unknown = approved_keys - proposed_keys
    if unknown:
        raise ApprovalError(
            f"Approved key(s) {sorted(unknown)} were not part of the proposed changes "
            f"{sorted(proposed_keys)}. Refusing to apply — this usually means the approval "
            "step is out of sync with what was actually shown to the user."
        )

    temp_settings = dict(base_settings)
    for change in proposed_changes:
        if change.key in approved_keys:
            temp_settings[change.key] = change.recommended_value
    return temp_settings


def diff_from_base(base_settings: dict, temp_settings: dict) -> dict:
    """Small helper for logging/job history: which keys actually differ
    between the original preset and the temp profile that was sliced."""
    return {
        key: (base_settings.get(key), temp_settings.get(key))
        for key in temp_settings
        if base_settings.get(key) != temp_settings.get(key)
    }
