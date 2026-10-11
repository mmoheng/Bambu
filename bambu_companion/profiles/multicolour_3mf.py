"""Builds a multi-colour Bambu Studio 3MF from separate mesh files.

The usual way to print a part in two colours is to export it as two
meshes that share one coordinate space — a body and an inlay (numbers,
a logo) — and give each its own filament. Bambu Studio does that with
one object made of several *parts*, each part assigned an extruder
(filament slot). This module writes exactly that structure, in the
layout Bambu Studio 02.08 itself writes (checked against Matt's own
saved projects on 2026-10-10):

- `3D/3dmodel.model`: one `<object>` per printed object, made of
  `<component p:path="/3D/Objects/object_N.model" objectid=…>` entries,
  and one `<build><item>` per copy;
- `3D/Objects/object_N.model`: the part meshes;
- `Metadata/model_settings.config`: the object / part names and each
  part's `extruder`, plus the plate's `model_instance` list.

Bambu Studio's command line needs a complete `project_settings.config`
in the file (see `build_multicolour_3mf`); the service gets a genuine
one by slicing a test cube with the chosen presets first and embedding
the config Bambu Studio itself wrote. The parts keep their relative positions; each
object is moved so it sits on the plate (lowest point at z = 0) and is
centred, and copies are laid out in a grid on a 256 mm bed.

Pure standard library + numpy; no Bambu Studio needed.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.sax.saxutils import quoteattr

import numpy as np

from ..model_analyzer.mesh_io import MeshLoadError, load_mesh

MAX_SLOTS = 16
MAX_COPIES = 100
BED_MM = 256.0
GAP_MM = 4.0

_NS = (
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p"'
)
_IDENTITY = "1 0 0 0 1 0 0 0 1 0 0 0"
_IDENTITY_4X4 = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"


class MulticolourError(ValueError):
    """A request that can't be turned into a multi-colour file, with a
    message a person can act on."""


@dataclass
class _Part:
    name: str
    filament: int
    vertices: np.ndarray
    faces: np.ndarray


@dataclass
class _Object:
    name: str
    copies: int
    parts: list[_Part]
    size: np.ndarray


def _parse(objects: Sequence[Mapping[str, Any]]) -> list[_Object]:
    if not isinstance(objects, (list, tuple)) or not objects:
        raise MulticolourError("Give at least one object, each with a list of parts.")
    out = []
    for i, obj in enumerate(objects, start=1):
        if not isinstance(obj, Mapping):
            raise MulticolourError(f"Object {i} must be an object with 'parts'.")
        parts_in = obj.get("parts")
        if not isinstance(parts_in, (list, tuple)) or not parts_in:
            raise MulticolourError(f"Object {i} has no parts.")
        copies = obj.get("copies", 1)
        if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= MAX_COPIES:
            raise MulticolourError(f"Object {i}: copies must be a whole number from 1 to {MAX_COPIES}.")
        parts = []
        for j, part in enumerate(parts_in, start=1):
            if not isinstance(part, Mapping) or not part.get("path"):
                raise MulticolourError(f"Object {i}, part {j}: give the mesh file as 'path'.")
            slot = part.get("filament", 1)
            if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= MAX_SLOTS:
                raise MulticolourError(f"Object {i}, part {j}: filament must be a slot number from 1 to {MAX_SLOTS}.")
            path = Path(part["path"])
            if not path.is_file():
                raise MulticolourError(f"Object {i}, part {j}: file not found: {path}")
            try:
                mesh = load_mesh(path)
            except MeshLoadError as exc:
                raise MulticolourError(f"Object {i}, part {j}: {exc}") from exc
            parts.append(_Part(str(part.get("name") or path.stem), slot,
                               np.asarray(mesh.vertices, float), np.asarray(mesh.faces, np.int64)))
        allv = np.vstack([p.vertices for p in parts])
        lo, hi = allv.min(0), allv.max(0)
        shift = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
        for p in parts:                       # same shift for every part: they stay aligned
            p.vertices = p.vertices - shift
        name = str(obj.get("name") or parts[0].name)
        out.append(_Object(name, copies, parts, hi - lo))
    return out


def _layout(objs: list[_Object]) -> tuple[list[tuple[int, float, float]], bool]:
    """Row-by-row placement of every copy, centred on the bed. Returns
    [(object index, x, y)] and whether it all fits on one plate (the
    slicer re-arranges anyway; this is a sensible start and a warning)."""
    items = [(k, o.size) for k, o in enumerate(objs) for _ in range(o.copies)]
    rows, row, x, row_h = [], [], 0.0, 0.0
    for k, size in items:
        w, h = size[0], size[1]
        if row and x + w > BED_MM:
            rows.append((row, row_h)); row, x, row_h = [], 0.0, 0.0
        row.append((k, x + w / 2, w)); x += w + GAP_MM; row_h = max(row_h, h)
    if row:
        rows.append((row, row_h))
    total_h = sum(h for _, h in rows) + GAP_MM * (len(rows) - 1)
    widest = max(sum(w for _, _, w in r) + GAP_MM * (len(r) - 1) for r, _ in rows)
    fits = bool(total_h <= BED_MM and widest <= BED_MM)
    placed, y = [], BED_MM / 2 + total_h / 2
    for r, h in rows:
        width = sum(w for _, _, w in r) + GAP_MM * (len(r) - 1)
        x0 = BED_MM / 2 - width / 2
        for k, cx, _ in r:
            placed.append((k, x0 + cx, y - h / 2))
        y -= h + GAP_MM
    return placed, fits


def _mesh_xml(oid: int, v: np.ndarray, f: np.ndarray) -> str:
    buf = io.StringIO()
    buf.write(f'  <object id="{oid}" p:UUID="{uuid.uuid4()}" type="model">\n   <mesh>\n    <vertices>\n')
    buf.writelines(f'     <vertex x="{a:.5f}" y="{b:.5f}" z="{c:.5f}"/>\n' for a, b, c in v)
    buf.write("    </vertices>\n    <triangles>\n")
    buf.writelines(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n' for a, b, c in f)
    buf.write("    </triangles>\n   </mesh>\n  </object>\n")
    return buf.getvalue()


def build_multicolour_3mf(
    objects: Sequence[Mapping[str, Any]],
    output_path: str | Path,
    *,
    project_settings: bytes | str | None = None,
) -> dict[str, Any]:
    """Writes a NEW 3MF (never overwrites). `objects` is a list of
    {"name"?, "copies"?: int, "parts": [{"path": mesh file, "filament": slot, "name"?}]}.
    `project_settings` is a complete `project_settings.config` to embed.
    Bambu Studio 02.08's command line CRASHED (exit 0xC0000005) on this
    file without one, and on a stub with a handful of keys, even with
    every setting passed on the command line (2026-10-10); with a
    complete config it sliced. Returns a summary: objects, copies,
    filament slots used, whether the copies fit on one plate."""
    out = Path(output_path)
    if out.suffix.lower() != ".3mf":
        raise MulticolourError("The output file must end in .3mf.")
    if out.exists():
        raise MulticolourError(f"{out} already exists. Refusing to overwrite it.")
    objs = _parse(objects)
    placed, fits = _layout(objs)

    files: dict[str, str] = {}
    resources, items, cfg, instances, rels = [], [], [], [], []
    next_id = 1
    obj_ids = []
    for k, o in enumerate(objs, start=1):
        sub = f"3D/Objects/object_{k}.model"
        part_ids, meshes = [], []
        for p in o.parts:
            part_ids.append(next_id)
            meshes.append(_mesh_xml(next_id, p.vertices, p.faces))
            next_id += 1
        oid = next_id; next_id += 1
        obj_ids.append(oid)
        files[sub] = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {_NS}>\n'
            ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n'
            + "".join(meshes) + " </resources>\n <build/>\n</model>\n"
        )
        rels.append(sub)
        comps = "".join(
            f'    <component p:path="/{sub}" objectid="{pid}" p:UUID="{uuid.uuid4()}" transform="{_IDENTITY}"/>\n'
            for pid in part_ids
        )
        resources.append(f'  <object id="{oid}" p:UUID="{uuid.uuid4()}" type="model">\n   <components>\n'
                         f"{comps}   </components>\n  </object>\n")
        part_cfg = "".join(
            f'    <part id="{pid}" subtype="normal_part">\n'
            f'      <metadata key="name" value={quoteattr(p.name)}/>\n'
            f'      <metadata key="matrix" value="{_IDENTITY_4X4}"/>\n'
            f'      <metadata key="extruder" value="{p.filament}"/>\n    </part>\n'
            for pid, p in zip(part_ids, o.parts)
        )
        cfg.append(f'  <object id="{oid}">\n    <metadata key="name" value={quoteattr(o.name)}/>\n'
                   f'    <metadata key="extruder" value="{o.parts[0].filament}"/>\n{part_cfg}  </object>\n')

    slots = max(p.filament for o in objs for p in o.parts)
    counts = [0] * len(objs)
    for k, x, y in placed:
        items.append(f'  <item objectid="{obj_ids[k]}" p:UUID="{uuid.uuid4()}" '
                     f'transform="1 0 0 0 1 0 0 0 1 {x:.4f} {y:.4f} 0" printable="1"/>\n')
        instances.append(f'    <model_instance>\n      <metadata key="object_id" value="{obj_ids[k]}"/>\n'
                         f'      <metadata key="instance_id" value="{counts[k]}"/>\n    </model_instance>\n')
        counts[k] += 1

    files["3D/3dmodel.model"] = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {_NS}>\n'
        ' <metadata name="Application">BambuStudio-02.08.04.57</metadata>\n'
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
        " <resources>\n" + "".join(resources) + " </resources>\n"
        f' <build p:UUID="{uuid.uuid4()}">\n' + "".join(items) + " </build>\n</model>\n"
    )
    files["3D/_rels/3dmodel.model.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        + "".join(f' <Relationship Target="/{s}" Id="rel-{i}" '
                  'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
                  for i, s in enumerate(rels, start=1))
        + "</Relationships>\n"
    )
    files["Metadata/model_settings.config"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n' + "".join(cfg)
        + '  <plate>\n    <metadata key="plater_id" value="1"/>\n    <metadata key="plater_name" value=""/>\n'
        '    <metadata key="locked" value="false"/>\n'
        '    <metadata key="filament_map_mode" value="Auto For Flush"/>\n'
        f'    <metadata key="filament_maps" value="{" ".join(["1"] * slots)}"/>\n'
        + "".join(instances) + "  </plate>\n</config>\n"
    )
    files["[Content_Types].xml"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        ' <Default Extension="png" ContentType="image/png"/>\n'
        ' <Default Extension="gcode" ContentType="text/x.gcode"/>\n</Types>\n'
    )
    files["_rels/.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n</Relationships>\n'
    )

    order = ["[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model", "3D/_rels/3dmodel.model.rels",
             *rels, "Metadata/model_settings.config"]
    if project_settings is not None:
        files["Metadata/project_settings.config"] = (
            project_settings.decode("utf-8") if isinstance(project_settings, bytes) else project_settings
        )
        order.append("Metadata/project_settings.config")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(out, "x", zipfile.ZIP_DEFLATED) as zf:   # "x": never replace a file
            for name in order:
                zf.writestr(name, files[name])
    except FileExistsError as exc:
        raise MulticolourError(f"{out} already exists. Refusing to overwrite it.") from exc
    except BaseException:
        out.unlink(missing_ok=True)
        raise

    return {
        "output_path": str(out),
        "objects": [
            {
                "name": o.name,
                "copies": o.copies,
                "size_mm": [round(float(v), 2) for v in o.size],
                "parts": [{"name": p.name, "filament": p.filament, "triangles": int(len(p.faces))} for p in o.parts],
            }
            for o in objs
        ],
        "filament_slots": slots,
        "fits_one_plate": fits,
    }
