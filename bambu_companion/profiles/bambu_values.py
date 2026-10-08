"""Converts between Bambu Studio's stored setting values and the plain
Python values the optimizer reasons with.

Why this exists: a real Bambu Studio config (a process JSON, or the
`Metadata/project_settings.config` inside a project 3MF) stores
everything as strings — `"wall_loops": "2"`, `"sparse_infill_density":
"15%"`, `"enable_support": "1"` — and, since Bambu Studio 2.x, some
keys as one-element lists: `"outer_wall_speed": ["200"]` (confirmed in
a real 02.08.04.57 file). The optimizer works with `2`, `15`, `True`,
`200`. Before this module the two were mixed freely, which meant:

- loading a real profile made `"2" != 2`, so every setting looked like
  it needed changing even when it already matched, and
- an exported "full profile" got bare numbers overlaid onto a file of
  strings, which Bambu Studio does not reliably accept.

Everything here is pure and has no I/O.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping

from .bambu_settings import (
    KIND_BOOL,
    KIND_ENUM,
    KIND_FLOAT,
    KIND_INT,
    KIND_PERCENT,
    SETTINGS,
)


class SettingValueError(ValueError):
    """A value can't be used for the given setting (unknown key, wrong
    type, out of the registry's sane range, or not one of its choices)."""


_OPTION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_() \-]{0,39}$")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _scalar(raw: Any) -> Any:
    """Bambu 2.x per-extruder lists -> their first element."""
    if isinstance(raw, (list, tuple)):
        return raw[0] if raw else None
    return raw


def to_typed(key: str, raw: Any) -> Any:
    """Bambu-stored value (or an already-typed one) -> plain Python
    value. Unknown keys and unparseable values are returned unchanged
    rather than raising: this is used to *read* whatever is in a file."""
    spec = SETTINGS.get(key)
    value = _scalar(raw)
    if spec is None or value is None:
        return value
    try:
        if spec.kind == KIND_BOOL:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            text = str(value).strip().lower()
            if text in _TRUE:
                return True
            if text in _FALSE:
                return False
            return value
        if spec.kind == KIND_INT:
            if isinstance(value, bool):
                return int(value)
            number = float(str(value).strip())
            return int(number) if number == int(number) else number
        if spec.kind in (KIND_FLOAT, KIND_PERCENT):
            if isinstance(value, bool):
                return float(value)
            text = str(value).strip()
            if text.endswith("%"):
                text = text[:-1]
            number = float(text)
            # 200.0 -> 200, 15.0 -> 15; 0.2 stays 0.2. Keeps tables and
            # JSON readable and matches how people write these values.
            return int(number) if number == int(number) else number
    except (TypeError, ValueError, OverflowError):
        return value
    return str(value) if not isinstance(value, str) else value


def _format_number(number: float) -> str:
    if float(number) == int(number):
        return str(int(number))
    return f"{float(number):.6f}".rstrip("0").rstrip(".")


def to_bambu_string(key: str, value: Any) -> str:
    """Plain Python value -> the string Bambu Studio stores for `key`."""
    spec = SETTINGS.get(key)
    typed = to_typed(key, value)
    kind = spec.kind if spec else KIND_ENUM
    if kind == KIND_BOOL:
        if not isinstance(typed, bool):
            raise SettingValueError(f"{key}: expected true/false, got {value!r}")
        return "1" if typed else "0"
    if kind in (KIND_INT, KIND_FLOAT, KIND_PERCENT):
        if isinstance(typed, bool) or not isinstance(typed, (int, float)):
            raise SettingValueError(f"{key}: expected a number, got {value!r}")
        if not math.isfinite(typed):
            raise SettingValueError(f"{key}: expected a finite number, got {value!r}")
        text = _format_number(typed)
        return f"{text}%" if kind == KIND_PERCENT else text
    return str(typed)


def validate(key: str, value: Any) -> Any:
    """Checks `value` is acceptable for `key` and returns it typed.
    Raises SettingValueError for anything this project should not write:
    a key outside the registry, the wrong type, a number outside the
    registry's range, or a string that isn't one of the listed choices.
    """
    spec = SETTINGS.get(key)
    if spec is None:
        raise SettingValueError(
            f"'{key}' is not a setting Bambu Companion is allowed to change "
            "(see profiles/bambu_settings.py for the list)."
        )
    if isinstance(value, (dict, set, bytes)) or (isinstance(value, (list, tuple)) and len(value) != 1):
        raise SettingValueError(f"{key}: expected a single value, got {type(value).__name__}")
    if spec.kind in (KIND_INT, KIND_FLOAT, KIND_PERCENT) and isinstance(_scalar(value), bool):
        raise SettingValueError(f"{key}: expected a number, got {value!r}")
    typed = to_typed(key, value)
    to_bambu_string(key, typed)  # raises on a wrong type
    if spec.kind in (KIND_INT, KIND_FLOAT, KIND_PERCENT):
        if spec.kind == KIND_INT and typed != int(typed):
            raise SettingValueError(f"{key}: expected a whole number, got {value!r}")
        if spec.minimum is not None and typed < spec.minimum:
            raise SettingValueError(f"{key}: {typed} is below the allowed minimum {spec.minimum}")
        if spec.maximum is not None and typed > spec.maximum:
            raise SettingValueError(f"{key}: {typed} is above the allowed maximum {spec.maximum}")
    if spec.kind == KIND_ENUM:
        if not isinstance(_scalar(value), str):
            raise SettingValueError(f"{key}: expected one of Bambu Studio's option names, got {value!r}")
        if spec.choices and typed not in spec.choices:
            raise SettingValueError(f"{key}: {typed!r} is not one of {list(spec.choices)}")
        if not spec.choices and not _OPTION_NAME_RE.match(typed):
            # The full option list for this key isn't pinned in the
            # registry (it grows between Bambu Studio releases), so at
            # least insist on something shaped like one of its option
            # names — "grid", "tree(auto)", "monotonicline".
            raise SettingValueError(f"{key}: {typed!r} does not look like a Bambu Studio option name")
    return typed


def values_equal(key: str, a: Any, b: Any) -> bool:
    """True if two values mean the same thing for `key`, whichever
    representation each is in (`"15%"` == `15`, `["200"]` == `200`,
    `"1"` == `True`)."""
    ta, tb = to_typed(key, a), to_typed(key, b)
    if isinstance(ta, bool) or isinstance(tb, bool):
        return ta is tb if isinstance(ta, bool) and isinstance(tb, bool) else ta == tb
    if isinstance(ta, (int, float)) and isinstance(tb, (int, float)):
        return abs(float(ta) - float(tb)) < 1e-9
    return ta == tb


def looks_like_bambu_config(config: Mapping[str, Any]) -> bool:
    """True if `config` stores registry keys the way Bambu Studio does
    (strings / lists of strings) rather than as plain numbers."""
    seen = [config[k] for k in SETTINGS if k in config]
    if not seen:
        return False
    stringy = sum(1 for v in seen if isinstance(v, (str, list)))
    return stringy * 2 > len(seen)


def format_like(key: str, value: Any, existing: Any, *, bambu_style: bool) -> Any:
    """Formats `value` in the same shape `existing` already has in the
    file being written: a list stays a list of the same length, a string
    stays a string, a plain number stays a number. A key that isn't in
    the file yet follows `bambu_style`."""
    if isinstance(existing, (list, tuple)):
        text = to_bambu_string(key, value)
        return [text] * max(len(existing), 1)
    if isinstance(existing, str) or (existing is None and bambu_style):
        return to_bambu_string(key, value)
    return to_typed(key, value)


def overlay(base: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    """Returns a NEW config: `base` with `changes` (key -> typed value)
    written in whatever representation `base` uses. `base` is never
    mutated and no other key is touched."""
    bambu_style = looks_like_bambu_config(base)
    merged = dict(base)
    for key, value in changes.items():
        merged[key] = format_like(key, value, base.get(key), bambu_style=bambu_style)
    return merged


def typed_settings(config: Mapping[str, Any], keys: Iterable[str] | None = None) -> dict[str, Any]:
    """Pulls the registry's keys (or `keys`) out of a config as typed
    values, skipping any the config doesn't have."""
    wanted = list(keys) if keys is not None else list(SETTINGS)
    return {k: to_typed(k, config[k]) for k in wanted if k in config}
