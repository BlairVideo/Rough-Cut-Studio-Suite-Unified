"""
lut.py -- parse, sample, and write 3D LUTs (.cube / .3dl).

A CubeLut is the one in-memory LUT representation used on both sides of
Colorize: ffmpeg_graph.py samples a creative LUT through `sample_lut`
while baking the full grade (see grade.py's module docstring), and
`to_preview_json` produces the flat, JSON-serializable form
colorize.js uploads as a WebGL texture (3D if the WebKit WebGL2 context
exposes TEXTURE_3D, tiled-2D as a fallback -- both cases just need the
same flat RGB lattice, so one export format serves either upload path).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

SUPPORTED_EXTENSIONS = (".cube", ".3dl")


class LutParseError(ValueError):
    pass


@dataclass
class CubeLut:
    size: int                       # lattice is size x size x size
    data: List[Tuple[float, float, float]]  # length size**3, R-fastest ordering
    title: str = ""
    domain_min: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    domain_max: Tuple[float, float, float] = (1.0, 1.0, 1.0)

    def index(self, ri: int, gi: int, bi: int) -> int:
        return ri + gi * self.size + bi * self.size * self.size


def identity_lut(size: int = 17) -> CubeLut:
    data = []
    for bi in range(size):
        for gi in range(size):
            for ri in range(size):
                data.append((ri / (size - 1), gi / (size - 1), bi / (size - 1)))
    return CubeLut(size=size, data=data, title="Identity")


def parse_lut_file(path: str) -> CubeLut:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".cube":
        return parse_cube(path)
    if ext == ".3dl":
        return parse_3dl(path)
    raise LutParseError(f"Unsupported LUT extension: {ext or '(none)'}")


def parse_cube(path: str) -> CubeLut:
    """Parses the Adobe/Resolve-common .cube format (3D LUTs only --
    1D LUT_1D_SIZE files are rejected with a clear error rather than
    silently misread as 3D)."""
    size: Optional[int] = None
    title = ""
    domain_min = (0.0, 0.0, 0.0)
    domain_max = (1.0, 1.0, 1.0)
    data: List[Tuple[float, float, float]] = []

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            upper = line.upper()
            if upper.startswith("TITLE"):
                title = line.split(None, 1)[1].strip().strip('"') if len(line.split(None, 1)) > 1 else ""
            elif upper.startswith("LUT_1D_SIZE"):
                raise LutParseError("1D .cube LUTs are not supported -- Colorize applies 3D creative LUTs only")
            elif upper.startswith("LUT_3D_SIZE"):
                size = int(line.split()[1])
            elif upper.startswith("DOMAIN_MIN"):
                parts = line.split()[1:4]
                domain_min = tuple(float(p) for p in parts)
            elif upper.startswith("DOMAIN_MAX"):
                parts = line.split()[1:4]
                domain_max = tuple(float(p) for p in parts)
            else:
                parts = line.split()
                if len(parts) != 3:
                    continue
                try:
                    data.append((float(parts[0]), float(parts[1]), float(parts[2])))
                except ValueError:
                    continue

    if size is None:
        raise LutParseError("Missing LUT_3D_SIZE in .cube file")
    expected = size ** 3
    if len(data) != expected:
        raise LutParseError(
            f"Expected {expected} data rows for LUT_3D_SIZE {size}, found {len(data)}")

    return CubeLut(size=size, data=data, title=title, domain_min=domain_min, domain_max=domain_max)


def parse_3dl(path: str) -> CubeLut:
    """Parses the common 3dl format: an optional 'Mesh <in> <out>' or
    bit-depth header line, then size**3 integer RGB rows. Values are
    normalized to [0,1] using the max value actually seen in the file
    (robust to 8/10/12/16-bit variants without needing an explicit
    bit-depth field)."""
    rows: List[Tuple[int, int, int]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 3:
                continue
            try:
                rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
            except ValueError:
                continue

    if not rows:
        raise LutParseError("No data rows found in .3dl file")

    size = round(len(rows) ** (1.0 / 3.0))
    if size ** 3 != len(rows):
        raise LutParseError(f"Row count {len(rows)} is not a perfect cube -- malformed .3dl")

    max_val = max(max(r, g, b) for r, g, b in rows) or 1
    data = [(r / max_val, g / max_val, b / max_val) for r, g, b in rows]
    return CubeLut(size=size, data=data, title=os.path.splitext(os.path.basename(path))[0])


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def sample_lut(lut: CubeLut, r: float, g: float, b: float) -> Tuple[float, float, float]:
    """Trilinear sample of the LUT at normalized [0,1] input (already
    assumed domain-normalized by the caller)."""
    n = lut.size
    if n == 1:
        return lut.data[0]
    fr = min(max(r, 0.0), 1.0) * (n - 1)
    fg = min(max(g, 0.0), 1.0) * (n - 1)
    fb = min(max(b, 0.0), 1.0) * (n - 1)
    r0, g0, b0 = int(fr), int(fg), int(fb)
    r1, g1, b1 = min(r0 + 1, n - 1), min(g0 + 1, n - 1), min(b0 + 1, n - 1)
    tr, tg, tb = fr - r0, fg - g0, fb - b0

    def at(ri, gi, bi):
        return lut.data[lut.index(ri, gi, bi)]

    c000, c100 = at(r0, g0, b0), at(r1, g0, b0)
    c010, c110 = at(r0, g1, b0), at(r1, g1, b0)
    c001, c101 = at(r0, g0, b1), at(r1, g0, b1)
    c011, c111 = at(r0, g1, b1), at(r1, g1, b1)

    def lerp3(a, b, t):
        return (_lerp(a[0], b[0], t), _lerp(a[1], b[1], t), _lerp(a[2], b[2], t))

    c00 = lerp3(c000, c100, tr)
    c10 = lerp3(c010, c110, tr)
    c01 = lerp3(c001, c101, tr)
    c11 = lerp3(c011, c111, tr)
    c0 = lerp3(c00, c10, tg)
    c1 = lerp3(c01, c11, tg)
    return lerp3(c0, c1, tb)


def to_preview_json(lut: CubeLut) -> dict:
    """Flat lattice for the frontend's WebGL texture upload -- a single
    flat list of size**3 * 3 floats in [0,1], R-fastest, matching
    CubeLut.index's ordering so colorize.js can upload it directly."""
    flat: List[float] = []
    for rgb in lut.data:
        flat.extend(rgb)
    return {"size": lut.size, "title": lut.title, "data": flat}


def write_cube(lut: CubeLut, path: str, title: Optional[str] = None) -> None:
    """Writes a standard .cube file -- used by ffmpeg_graph.py to bake
    the full grade pipeline into a LUT ffmpeg's own lut3d filter can
    apply at export."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'TITLE "{title or lut.title or "Colorize Grade"}"\n')
        f.write(f"LUT_3D_SIZE {lut.size}\n")
        f.write("DOMAIN_MIN 0.0 0.0 0.0\n")
        f.write("DOMAIN_MAX 1.0 1.0 1.0\n")
        for r, g, b in lut.data:
            f.write(f"{r:.6f} {g:.6f} {b:.6f}\n")
