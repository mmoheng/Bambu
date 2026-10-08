"""Synthetic Bambu Studio files for tests: a project 3MF, a sliced 3MF,
a preset tree, and a stand-in for the Bambu Studio command line.

Shapes (part names, key names, value formats, XML layout) follow real
files written by Bambu Studio 02.08.04.57 for a Bambu Lab A1 — see the
docstrings in profiles/project_3mf.py and profiles/bambu_settings.py.
The geometry and every identifier are made up.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

# A trimmed copy of the key/value FORMATS in a real project_settings.config:
# strings everywhere, "15%" for percentages, "1"/"0" for booleans, and
# one-element lists for the per-extruder speed keys.
REAL_STYLE_SETTINGS: dict[str, Any] = {
    "name": "project_settings",
    "from": "project",
    "version": "02.08.04.57",
    "printer_settings_id": "Bambu Lab A1 0.4 nozzle",
    "printer_model": "Bambu Lab A1",
    "printer_variant": "0.4",
    "nozzle_diameter": ["0.4"],
    "curr_bed_type": "Textured PEI Plate",
    "print_settings_id": "0.28mm Extra Draft @BBL A1",
    "filament_settings_id": ["Bambu PLA Matte @BBL A1"],
    "filament_type": ["PLA"],
    "different_settings_to_system": ["enable_support;support_type", "", ""],
    "layer_height": "0.28",
    "initial_layer_print_height": "0.2",
    "wall_loops": "2",
    "top_shell_layers": "4",
    "bottom_shell_layers": "3",
    "sparse_infill_density": "15%",
    "sparse_infill_pattern": "grid",
    "outer_wall_speed": ["200"],
    "inner_wall_speed": ["200"],
    "enable_support": "1",
    "support_type": "normal(auto)",
    "support_threshold_angle": "30",
    "support_on_build_plate_only": "1",
    "support_top_z_distance": "0.2",
    "support_interface_top_layers": "3",
    "brim_type": "auto_brim",
    "brim_width": "5",
    "elefant_foot_compensation": "0.075",
    "xy_hole_compensation": "0",
    "machine_start_gcode": "M104 S0 ; never touched by this project",
    "some_key_this_project_never_heard_of": "keep-me",
}

_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_PNS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"

_CUBE_VERTS = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0), (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
_CUBE_TRIS = [
    (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
    (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
]  # fmt: skip


def _mesh_xml() -> str:
    verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in _CUBE_VERTS)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in _CUBE_TRIS)
    return f"<mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh>"


def component_model_parts(translate=(100.0, 120.0, 0.0)) -> dict[str, str]:
    """The two-part layout Bambu Studio writes: a root model that only
    references `/3D/Objects/object_1.model`, where the triangles live."""
    tx, ty, tz = translate
    root = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<model unit="millimeter" xmlns="{_NS}" xmlns:p="{_PNS}" requiredextensions="p">'
        f'<metadata name="Application">BambuStudio-02.08.04.57</metadata>'
        f"<resources>"
        f'<object id="2" type="model"><components>'
        f'<component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        f"</components></object>"
        f"</resources>"
        f'<build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 {tx} {ty} {tz}" printable="1"/></build>'
        f"</model>"
    )
    child = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<model unit="millimeter" xmlns="{_NS}" xmlns:p="{_PNS}">'
        f'<resources><object id="1" type="model">{_mesh_xml()}</object></resources>'
        f"</model>"
    )
    return {"3D/3dmodel.model": root, "3D/Objects/object_1.model": child}


def slice_info_xml(*, prediction_s: int = 8354, weight_g: float = 91.71, support_used: bool = True) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        '  <header><header_item key="X-BBL-Client-Version" value="02.08.04.57"/></header>\n'
        "  <plate>\n"
        '    <metadata key="index" value="1"/>\n'
        f'    <metadata key="prediction" value="{prediction_s}"/>\n'
        f'    <metadata key="weight" value="{weight_g}"/>\n'
        f'    <metadata key="support_used" value="{"true" if support_used else "false"}"/>\n'
        '    <object identify_id="138" name="cube.stl" skipped="false" />\n'
        '    <filament id="1" type="PLA" color="#F7D959" used_m="28.89" '
        f'used_g="{weight_g}" used_for_object="true" used_for_support="true"/>\n'
        '    <warning msg="not_support_traditional_timelapse" level="2" error_code ="10018003"  />\n'
        "  </plate>\n</config>\n"
    )


_EMPTY_SLICE_INFO = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
    '  <header><header_item key="X-BBL-Client-Version" value="02.08.04.57"/></header>\n</config>\n'
)

_GCODE_HEAD = (
    "; HEADER_BLOCK_START\n; BambuStudio 02.08.04.57\n"
    "; model printing time: 2h 13m 0s; total estimated time: 2h 19m 14s\n"
    "; total layer number: 346\n; max_z_height: 88.96\n; HEADER_BLOCK_END\nG28\n"
)


def write_project_3mf(
    path: Path,
    *,
    settings: dict[str, Any] | None = REAL_STYLE_SETTINGS,
    sliced: bool = False,
    extra_settings: dict[str, Any] | None = None,
) -> Path:
    """Writes a project 3MF. `settings=None` makes a geometry-only 3MF
    (no project_settings.config); `sliced=True` adds a slice result and
    plate G-code, like File > Export > Export plate sliced file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for name, xml in component_model_parts().items():
            zf.writestr(name, xml)
        zf.writestr("Metadata/plate_1.png", b"\x89PNG not really an image")
        if settings is not None:
            merged = {**settings, **(extra_settings or {})}
            zf.writestr("Metadata/project_settings.config", json.dumps(merged, indent=4))
            zf.writestr("Metadata/slice_info.config", slice_info_xml() if sliced else _EMPTY_SLICE_INFO)
        if sliced:
            zf.writestr("Metadata/plate_1.gcode", _GCODE_HEAD)
    return path


def write_two_plate_project(path: Path) -> Path:
    """A project with one 10 mm cube on each of two plates. Bambu Studio
    lays plates out side by side in one coordinate space, so the second
    cube sits far to the right of the first (300 mm here)."""
    parts = component_model_parts(translate=(100.0, 120.0, 0.0))
    root = parts["3D/3dmodel.model"]
    root = root.replace(
        "</resources>",
        '<object id="4" type="model"><components>'
        '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        "</components></object></resources>",
    ).replace(
        "</build>",
        '<item objectid="4" transform="1 0 0 0 1 0 0 0 1 400 120 0" printable="1"/></build>',
    )
    parts["3D/3dmodel.model"] = root
    model_settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        '  <object id="2"><metadata key="name" value="left"/></object>\n'
        '  <object id="4"><metadata key="name" value="right"/></object>\n'
        '  <plate><metadata key="plater_id" value="1"/>'
        '<model_instance><metadata key="object_id" value="2"/><metadata key="instance_id" value="0"/></model_instance></plate>\n'
        '  <plate><metadata key="plater_id" value="2"/>'
        '<model_instance><metadata key="object_id" value="4"/><metadata key="instance_id" value="0"/></model_instance></plate>\n'
        "</config>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, xml in parts.items():
            zf.writestr(name, xml)
        zf.writestr("Metadata/project_settings.config", json.dumps(REAL_STYLE_SETTINGS, indent=4))
        zf.writestr("Metadata/model_settings.config", model_settings)
        zf.writestr("Metadata/slice_info.config", _EMPTY_SLICE_INFO)
    return path


def write_preset_tree(root: Path) -> Path:
    """A miniature `resources/profiles/BBL` with the same inheritance
    shape as Bambu's: common base -> family base -> selectable preset."""
    files = {
        "machine/fdm_machine_common.json": {
            "type": "machine", "name": "fdm_machine_common", "from": "system",
            "instantiation": "false", "printable_height": "250", "nozzle_diameter": ["0.4"],
            "machine_start_gcode": "G28",
        },
        "machine/Bambu Lab A1 0.4 nozzle.json": {
            "type": "machine", "name": "Bambu Lab A1 0.4 nozzle", "from": "system",
            "inherits": "fdm_machine_common", "instantiation": "true", "setting_id": "GM030",
            "printer_model": "Bambu Lab A1", "printable_height": "256",
            "printable_area": ["0x0", "256x0", "256x256", "0x256"],
        },
        "process/fdm_process_common.json": {
            "type": "process", "name": "fdm_process_common", "from": "system",
            "instantiation": "false", "wall_loops": "2", "sparse_infill_density": "15%",
            "layer_height": "0.2", "outer_wall_speed": ["120"], "enable_support": "0",
        },
        "process/fdm_process_single_0.20.json": {
            "type": "process", "name": "fdm_process_single_0.20", "from": "system",
            "inherits": "fdm_process_common", "instantiation": "false",
            "layer_height": "0.2", "top_shell_layers": "5",
        },
        "process/0.20mm Standard @BBL A1.json": {
            "type": "process", "name": "0.20mm Standard @BBL A1", "from": "system",
            "inherits": "fdm_process_single_0.20", "instantiation": "true", "setting_id": "GP004",
            "outer_wall_speed": ["200"],
        },
        "filament/fdm_filament_common.json": {
            "type": "filament", "name": "fdm_filament_common", "from": "system",
            "instantiation": "false", "nozzle_temperature": ["200"], "filament_type": ["PLA"],
        },
        "filament/Bambu PLA Basic @base.json": {
            "type": "filament", "name": "Bambu PLA Basic @base", "from": "system",
            "inherits": "fdm_filament_common", "instantiation": "false", "filament_id": "GFA00",
            "nozzle_temperature": ["220"],
        },
        "filament/Bambu PLA Basic @BBL A1.json": {
            "type": "filament", "name": "Bambu PLA Basic @BBL A1", "from": "system",
            "inherits": "Bambu PLA Basic @base", "instantiation": "true", "setting_id": "GFSA00",
        },
    }  # fmt: skip
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return root


class FakeStudio:
    """Stands in for `studio_runner.run_cli` (same call signature and
    return shape) so the runner's plumbing can be tested without Bambu
    Studio. It "slices" by reading the arguments the way the real CLI
    documents them and writing a sliced 3MF whose embedded settings are
    whatever it was given — or, with `ignore_settings=True`, Bambu's
    defaults, to reproduce the reported "silently fell back" behaviour.
    """

    def __init__(
        self,
        *,
        returncode: int = 0,
        write_output: bool = True,
        ignore_settings: bool = False,
        stderr: str = "",
        unsliced_output: bool = False,
        garbage_output: bool = False,
    ):
        self.returncode = returncode
        self.write_output = write_output
        self.ignore_settings = ignore_settings
        self.unsliced_output = unsliced_output  # exit 0, valid 3MF, but no slice result in it
        self.garbage_output = garbage_output  # exit 0, output file is not a 3MF at all
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, executable, args, *, timeout_s=900.0, cwd=None) -> dict[str, Any]:
        args = [str(a) for a in args]
        self.calls.append(args)
        run = {
            "command": [str(executable), *args],
            "returncode": self.returncode,
            "duration_s": 0.1,
            "stdout_tail": "",
            "stderr_tail": self.stderr,
        }
        if self.returncode != 0 or not self.write_output:
            return run

        def value(flag: str) -> str | None:
            return args[args.index(flag) + 1] if flag in args else None

        model = Path(args[-1])
        name = value("--export-3mf")
        out = Path(value("--outputdir")) / name if value("--outputdir") else Path(name)

        settings: dict[str, Any] = dict(REAL_STYLE_SETTINGS)
        if not self.ignore_settings:
            if model.suffix.lower() == ".3mf":
                with zipfile.ZipFile(model) as zf:
                    settings = json.loads(zf.read("Metadata/project_settings.config"))
            for f in (value("--load-settings") or "").split(";"):
                if f:
                    settings.update(json.loads(Path(f).read_text(encoding="utf-8")))
        if self.garbage_output:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"")
        else:
            write_project_3mf(out, settings=settings, sliced=not self.unsliced_output)
        return run
