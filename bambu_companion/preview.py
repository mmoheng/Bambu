"""Small PNG previews of models for the dashboard.

- A Bambu Studio 3MF that has a plate picture in it (every sliced
  output, every project saved from the window) — that picture is used
  as-is: it is what Bambu Studio itself shows.
- Anything else (an STL, a 3MF without a picture) is drawn here: an
  isometric, shaded view made by scattering points over every triangle
  and keeping the nearest one per pixel. Crude, but it needs nothing
  beyond numpy and the standard library, and it is plenty to recognise
  which model a job was about.
"""

from __future__ import annotations

import struct
import threading
import zipfile
import zlib
from pathlib import Path

import numpy as np

from .model_analyzer.mesh_io import load_mesh

SIZE = 320
_MAX_POINTS = 600_000
_THUMB_PARTS = ("Metadata/plate_1.png", "Auxiliaries/.thumbnails/thumbnail_middle.png", "Metadata/thumbnail.png")
_cache: dict[tuple[str, float, int], bytes] = {}
_cache_lock = threading.Lock()


def encode_png(rgb: np.ndarray) -> bytes:
    """(H, W, 3) uint8 -> PNG bytes, standard library only."""
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def _embedded_picture(path: Path) -> bytes | None:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for part in _THUMB_PARTS:
                if part in names:
                    data = zf.read(part)
                    if data.startswith(b"\x89PNG"):
                        return data
    except (zipfile.BadZipFile, OSError, KeyError):
        return None
    return None


def render_mesh(vertices: np.ndarray, faces: np.ndarray, size: int = SIZE,
                colour=(0.30, 0.55, 0.95)) -> np.ndarray:
    """Isometric shaded render, (size, size, 3) uint8 on a light background."""
    v = np.asarray(vertices, float)
    v = v - (v.min(0) + v.max(0)) / 2
    az, el = np.radians(45.0), np.radians(35.0)
    rz = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, np.cos(el), -np.sin(el)], [0, np.sin(el), np.cos(el)]])
    # look from the front-right, above: screen x = right, screen y = up, depth = towards viewer
    w = v @ rz.T
    w = np.c_[w[:, 0], w[:, 2], -w[:, 1]] @ rx.T
    t = w[faces]
    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    area = np.linalg.norm(n, axis=1) / 2
    keep = area > 1e-12
    t, n, area = t[keep], n[keep] / (2 * area[keep, None]), area[keep]
    ext = max(np.ptp(w[:, 0]), np.ptp(w[:, 1]), 1e-6)
    scale = (size * 0.86) / ext
    # enough points that neighbouring samples are under a pixel apart
    px_area = (1.0 / scale) ** 2
    counts = np.ceil(area / (px_area * 0.5)).astype(np.int64)
    total = counts.sum()
    if total > _MAX_POINTS:
        counts = np.maximum(1, np.floor(counts * (_MAX_POINTS / total)).astype(np.int64))
    idx = np.repeat(np.arange(len(t)), counts)
    rng = np.random.default_rng(0)
    r1, r2 = rng.random(len(idx)), rng.random(len(idx))
    flip = r1 + r2 > 1
    r1[flip], r2[flip] = 1 - r1[flip], 1 - r2[flip]
    pts = t[idx, 0] + (t[idx, 1] - t[idx, 0]) * r1[:, None] + (t[idx, 2] - t[idx, 0]) * r2[:, None]
    pts = np.vstack([pts, t.reshape(-1, 3)])                 # vertices too: keeps thin edges
    nid = np.concatenate([idx, np.repeat(np.arange(len(t)), 3)])
    light = np.array([0.35, 0.55, 0.75]); light /= np.linalg.norm(light)
    shade = np.abs(n @ light)[nid]                            # two-sided: works for any winding
    px = np.clip(((pts[:, 0]) * scale + size / 2).astype(int), 0, size - 1)
    py = np.clip((size / 2 - pts[:, 1] * scale).astype(int), 0, size - 1)
    flat = py * size + px
    depth = np.full(size * size, -np.inf)
    np.maximum.at(depth, flat, pts[:, 2])
    front = pts[:, 2] >= depth[flat] - 1e-9
    lum = np.zeros(size * size); lum[flat[front]] = shade[front]
    hit = np.isfinite(depth)
    img = np.empty((size * size, 3))
    img[:] = (0.95, 0.96, 0.98)
    base = np.array(colour)
    img[hit] = base * (0.35 + 0.65 * lum[hit, None]) + 0.08
    return (np.clip(img, 0, 1) * 255).astype(np.uint8).reshape(size, size, 3)


def preview_png(path: str | Path) -> bytes:
    """PNG preview of a model file; cached per file version."""
    p = Path(path)
    st = p.stat()
    key = (str(p.resolve()), st.st_mtime, st.st_size)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    data = _embedded_picture(p) if p.suffix.lower() == ".3mf" else None
    if data is None:
        mesh = load_mesh(p)
        data = encode_png(render_mesh(mesh.vertices, mesh.faces))
    with _cache_lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = data
    return data
