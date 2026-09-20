"""Command-line entry point for exercising the analyzer + optimizer
without the bridge/server/ChatGPT layer — useful for local testing on
real STL/3MF files while the rest of the stack is still being built.

Usage:
    python -m bambu_companion.cli path/to/model.stl \\
        --goal dimensional_accuracy --material PETG \\
        --current wall_loops=2 --current sparse_infill_density=15

This does NOT talk to a printer or Bambu Studio — it only runs the
model analyzer and print optimizer and prints the resulting diff, the
same table format described in the memory bank's Approval Workflow.
"""

from __future__ import annotations

import argparse
import json
import sys

from .model_analyzer import analyze
from .optimizer import format_diff_table, optimize, to_dict
from .schemas import PrintContext, PrintGoal


def _parse_current_settings(pairs: list[str]) -> dict:
    settings = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--current expects key=value, got: {pair!r}")
        key, raw_value = pair.split("=", 1)
        settings[key] = _coerce(raw_value)
    return settings


def _coerce(raw: str):
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            continue
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_path", help="Path to an .stl or .3mf file")
    parser.add_argument(
        "--goal",
        choices=[g.value for g in PrintGoal],
        default=PrintGoal.BALANCED.value,
    )
    parser.add_argument("--material", default="PLA")
    parser.add_argument("--nozzle", type=float, default=0.4, dest="nozzle_diameter_mm")
    parser.add_argument("--printer", default="Bambu Lab A1")
    parser.add_argument("--ams-slot", default=None)
    parser.add_argument(
        "--current",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Current setting value; repeatable. Values are parsed as int/float/bool/str.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of the text table."
    )
    args = parser.parse_args(argv)

    analysis = analyze(args.model_path, nozzle_diameter_mm=args.nozzle_diameter_mm)
    ctx = PrintContext(
        printer=args.printer,
        nozzle_diameter_mm=args.nozzle_diameter_mm,
        material=args.material,
        goal=PrintGoal(args.goal),
        current_settings=_parse_current_settings(args.current),
        ams_slot=args.ams_slot,
    )
    result = optimize(analysis, ctx)

    if args.json:
        print(json.dumps(to_dict(result), indent=2))
    else:
        if not analysis.bed_fit.fits:
            print("WARNING: model does not fit the A1 build volume.", file=sys.stderr)
            for note in analysis.bed_fit.notes:
                print(f"  - {note}", file=sys.stderr)
        print(format_diff_table(result))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
