"""Load an ODB++ job folder into a Board.

Altium and Xpedition both export this. Coordinates in the features file are
inches or millimeters (`U INCH` / `U MM`, inches when the unit line is absent).
Symbol sizes are thousandths of that unit unless the number is a small decimal
written already in file units (`r0.6` in a millimeter file is 0.6 mm).

Geometry is built Y-up, then Y is negated once so the existing Y-down view
matches the CAD window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from shapely.affinity import rotate as shp_rotate
from shapely.affinity import scale as shp_scale
from shapely.affinity import translate as shp_translate
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .board import Board, CopperPoly, Layer, Pad, StackLayer, Via
from .exchange import finish_stackup, flip_y, remember_net
from .geom import (
    arc_points_ends,
    as_polygon,
    buffer_centerline,
    chain_loops,
    circle_points,
    circle_world,
    pad_polygon,
    polygon_parts,
    shoelace,
    transform_point,
)

_COPPER_TYPES = {"SIGNAL", "POWER_GROUND", "MIXED"}
_MASK_TYPES = {"SOLDER_MASK", "MASK"}
_SKIP_TYPES = {"SILK_SCREEN", "SOLDER_PASTE", "ROUT", "DOCUMENT", "COMPONENT", "SILK"}
_PREFIXES = ("donut_r", "donut_s", "rrect", "rect", "oval", "hole", "oct", "di", "r", "s")
_NUMBER = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
_EPS_KEYS = ("dielectric_constant", "dielectricconstant", "d_constant", "er", "eps")
_LOSS_KEYS = ("loss_tangent", "dissipation", "tan_delta", "loss")
_THICK_KEYS = (
    "layer_thickness",
    "dielectric_thickness",
    "copper_thickness",
    "thickness",
    "board_thickness",
)


def symbol_mm(value: float, file_unit: str, suffix: str | None = None) -> float:
    """Convert one ODB++ symbol number to millimeters.

    `I` is thousandths of an inch and `M` is microns. Those suffixes win over
    the features-file unit. With no suffix, `r200` in a millimeter file is
    0.2 mm, and a small decimal such as `r0.6` is already in file units.
    """
    forced = (suffix or "").upper()[:1]
    if forced == "M":
        return value / 1000.0
    if forced == "I":
        return value / 1000.0 * 25.4
    inch = file_unit.upper().startswith("IN")
    scale = 25.4 if inch else 1.0
    thousandths = value / 1000.0 * scale
    if abs(thousandths) < 0.01 and abs(value) >= 0.05:
        return value * scale
    return thousandths


def _unit_name(token: str) -> str:
    text = token.strip().upper()
    if text in {"MM", "M", "METRIC", "MILLIMETER", "MILLIMETERS"}:
        return "MM"
    return "INCH"


@dataclass
class Symbol:
    kind: str
    raw: str
    dims: list[float]
    rounded: bool = False


@dataclass
class Feat:
    index: int
    record: str
    polarity: str
    net: str = ""
    geom: BaseGeometry | None = None
    feature_mm: float | None = None
    center: tuple[float, float] | None = None
    points: list[tuple[float, float]] | None = None
    holes: list[list[tuple[float, float]]] = field(default_factory=list)
    centerline: list[tuple[float, float]] | None = None
    diameter: float | None = None
    attrs: dict[str, str] = field(default_factory=dict)


def _find_child(path: Path, name: str) -> Path | None:
    if not path.is_dir():
        return None
    target = name.lower()
    for child in path.iterdir():
        if child.name.lower() == target:
            return child
    return None


def _is_job(path: Path) -> bool:
    matrix = _find_child(path, "matrix")
    return matrix is not None and _find_child(matrix, "matrix") is not None


def find_job_root(path: Path) -> Path | None:
    if _is_job(path):
        return path
    if not path.is_dir():
        return None
    for directory, dirnames, _filenames in _walk(path):
        for name in dirnames:
            if name.lower() != "matrix":
                continue
            root = Path(directory)
            if _is_job(root):
                return root
    return None


def _walk(path: Path):
    # os.walk follows symlinks we do not want; a plain stack stays inside the extract.
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        dirnames = [child.name for child in children if child.is_dir()]
        yield current, dirnames, [child.name for child in children if child.is_file()]
        stack.extend(child for child in children if child.is_dir())


def _read_text(path: Path) -> str | None:
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
    gz = path.with_name(path.name + ".gz")
    parent = path.parent
    if parent.is_dir():
        found = _find_child(parent, path.name)
        if found is not None and found.is_file():
            return found.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
        gz = _find_child(parent, path.name + ".gz")
    if gz is not None and gz.is_file():
        import gzip

        with gzip.open(gz, "rt", encoding="utf-8", errors="replace") as handle:
            return handle.read().lstrip("\ufeff")
    return None


def _lines(text: str) -> list[str]:
    rows = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#@") or stripped.startswith("#&"):
            stripped = stripped[1:]
        elif stripped.startswith("#"):
            continue
        else:
            stripped = stripped.split("#", 1)[0].strip()
        if stripped:
            rows.append(stripped)
    return rows


def _split_attr(line: str) -> tuple[str, dict[str, str]]:
    if ";" not in line:
        return line, {}
    body, tail = line.split(";", 1)
    attrs: dict[str, str] = {}
    for part in tail.split(","):
        part = part.strip().lstrip(";")
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            attrs[key.strip()] = value.strip()
        else:
            attrs[part] = ""
    return body.strip(), attrs


def _is_int(token: str) -> bool:
    try:
        int(token)
    except (TypeError, ValueError):
        return False
    return True


def _resolve_attrs(raw: dict[str, str], names: dict[int, str], strings: dict[int, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for key, value in raw.items():
        name = names[int(key)] if _is_int(key) and int(key) in names else key
        if _is_int(value) and int(value) in strings:
            text = strings[int(value)]
        else:
            text = value
        resolved[name] = text
    return resolved


def _net_from_attrs(attrs: dict[str, str]) -> str | None:
    for key, value in attrs.items():
        if "net" not in key.lower():
            continue
        text = value.strip().strip('"')
        if text in {"$NONE$", ""}:
            return ""
        return text
    return None


def _is_npth(attrs: dict[str, str]) -> bool:
    for key, value in attrs.items():
        blob = f"{key} {value}".lower().replace("-", "_")
        if any(token in blob for token in ("nplt", "unplated", "non_plated", "nonplated", "npth")):
            return True
        if value.strip().lower() in {"non", "np"}:
            return True
    return False


def _parse_orient(tokens: list[str]) -> tuple[float, bool]:
    if not tokens:
        return 0.0, False
    try:
        code = int(float(tokens[0]))
    except ValueError:
        return 0.0, False
    if 0 <= code <= 3:
        return code * 90.0, False
    if 4 <= code <= 7:
        return (code - 4) * 90.0, True
    if code == 8 and len(tokens) > 1:
        return float(tokens[1]), False
    if code == 9 and len(tokens) > 1:
        return float(tokens[1]), True
    return 0.0, False


def _is_cw(token: str) -> bool:
    return token.strip().upper() in {"Y", "YES", "CW", "TRUE", "1"}


def _classify_symbol(token: str, file_unit: str, suffix: str | None) -> Symbol:
    low = token.lower()
    prefix = ""
    rest = token
    for candidate in _PREFIXES:
        if not low.startswith(candidate):
            continue
        nxt = low[len(candidate) : len(candidate) + 1]
        if nxt and nxt not in "0123456789.+-":
            continue
        prefix = candidate
        rest = token[len(candidate) :]
        break
    if not prefix:
        return Symbol("custom", token, [])
    nums = [float(match) for match in _NUMBER.findall(rest)]
    dims = [symbol_mm(number, file_unit, suffix) for number in nums]
    rounded = prefix == "rrect" or "xr" in rest.lower()
    if "xc" in rest.lower() and not rounded:
        dims = dims[:2]
    kind = "rect" if prefix == "rrect" else prefix
    return Symbol(kind, token, dims, rounded=rounded)


def _with_resize(sym: Symbol, resize_mm: float) -> Symbol:
    if abs(resize_mm) < 1e-15 or not sym.dims:
        return sym
    dims = [value + resize_mm for value in sym.dims]
    if sym.kind in {"donut_r", "donut_s"} and len(sym.dims) >= 2:
        dims[1] = sym.dims[1] - resize_mm
    return Symbol(sym.kind, sym.raw, dims, sym.rounded)


def _place(local: list[tuple[float, float]], center, rotation: float, mirror: bool) -> list[tuple[float, float]]:
    return [transform_point(x, y, center, rotation, mirror) for x, y in local]


def _ring(local: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(local) >= 2 and abs(local[0][0] - local[-1][0]) < 1e-9 and abs(local[0][1] - local[-1][1]) < 1e-9:
        return local[:-1]
    return local


def _symbol_points(sym: Symbol, center, rotation: float, mirror: bool) -> tuple[list, list[list], float] | None:
    dims = sym.dims
    if not dims or dims[0] <= 0:
        return None
    feature = min(abs(value) for value in dims[:2]) if dims else None
    if sym.kind in {"r", "hole"}:
        points = pad_polygon("circle", (dims[0], dims[0]), center, rotation, mirror)
        return points, [], dims[0]
    if sym.kind == "s":
        points = pad_polygon("rect", (dims[0], dims[0]), center, rotation, mirror)
        return points, [], dims[0]
    if sym.kind == "rect" and len(dims) >= 2 and dims[1] > 0:
        if sym.rounded and len(dims) >= 3 and min(dims[0], dims[1]) > 0:
            ratio = max(0.0, min(0.5, dims[2] / min(dims[0], dims[1])))
            points = pad_polygon("roundrect", (dims[0], dims[1]), center, rotation, mirror, ratio)
        else:
            points = pad_polygon("rect", (dims[0], dims[1]), center, rotation, mirror)
        return points, [], min(dims[0], dims[1])
    if sym.kind == "oval" and len(dims) >= 2 and dims[1] > 0:
        points = pad_polygon("oval", (dims[0], dims[1]), center, rotation, mirror)
        return points, [], min(dims[0], dims[1])
    if sym.kind == "di":
        width = dims[0]
        height = dims[1] if len(dims) > 1 else dims[0]
        local = [(0.0, height / 2), (width / 2, 0.0), (0.0, -height / 2), (-width / 2, 0.0)]
        return _place(local, center, rotation, mirror), [], min(width, height)
    if sym.kind == "oct":
        width = dims[0]
        height = dims[1] if len(dims) > 1 else dims[0]
        cut = dims[2] if len(dims) > 2 else min(width, height) * 0.293
        cut = min(max(cut, 0.0), width / 2, height / 2)
        hx, hy = width / 2, height / 2
        local = [
            (hx - cut, hy),
            (hx, hy - cut),
            (hx, -hy + cut),
            (hx - cut, -hy),
            (-hx + cut, -hy),
            (-hx, -hy + cut),
            (-hx, hy - cut),
            (-hx + cut, hy),
        ]
        return _place(local, center, rotation, mirror), [], min(width, height)
    if sym.kind == "donut_r" and len(dims) >= 2 and dims[0] > dims[1] > 0:
        outer = [(dims[0] / 2 * x, dims[0] / 2 * y) for x, y in circle_points()]
        inner = [(dims[1] / 2 * x, dims[1] / 2 * y) for x, y in circle_points()]
        return _place(outer, center, rotation, mirror), [_place(inner, center, rotation, mirror)], dims[1]
    if sym.kind == "donut_s" and len(dims) >= 2 and dims[0] > dims[1] > 0:
        outer = pad_polygon("rect", (dims[0], dims[0]), center, rotation, mirror)
        inner = pad_polygon("rect", (dims[1], dims[1]), center, rotation, mirror)
        return outer, [inner], dims[1]
    return None if feature is None else None


def _stroke_width(sym: Symbol) -> tuple[float, str] | None:
    if sym.kind == "r" and sym.dims and sym.dims[0] > 0:
        return sym.dims[0], "round"
    if sym.kind == "s" and sym.dims and sym.dims[0] > 0:
        return sym.dims[0], "square"
    if sym.kind in {"rect", "oval"} and len(sym.dims) >= 2 and min(sym.dims[0], sym.dims[1]) > 0:
        return min(sym.dims[0], sym.dims[1]), "round"
    return None


def _geom_from_points(points, holes=None) -> BaseGeometry | None:
    poly = as_polygon(points, holes)
    return poly


def _clean(geom: BaseGeometry | None) -> BaseGeometry | None:
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.is_empty:
        return None
    return geom


def _read_surface(rows: list[str], start: int, scale: float = 1.0) -> tuple[BaseGeometry | None, list, list, int]:
    islands: list[tuple[list, list]] = []
    holes: list[list] = []
    ring: list[tuple[float, float]] = []
    hole_mode = False

    def store() -> None:
        nonlocal ring
        if len(ring) >= 3:
            closed = _ring(ring)
            if hole_mode and islands:
                islands[-1][1].append(closed)
            elif hole_mode:
                holes.append(closed)
            else:
                islands.append((closed, []))
        ring = []

    index = start
    while index < len(rows):
        parts = rows[index].split()
        index += 1
        if not parts:
            continue
        op = parts[0].upper()
        if op == "OB" and len(parts) >= 3:
            if ring:
                store()
            ring = [(float(parts[1]) * scale, float(parts[2]) * scale)]
            hole_mode = len(parts) > 3 and parts[3].upper().startswith("H")
        elif op == "OS" and len(parts) >= 3 and ring is not None:
            ring.append((float(parts[1]) * scale, float(parts[2]) * scale))
        elif op == "OC" and len(parts) >= 5 and ring:
            end = (float(parts[1]) * scale, float(parts[2]) * scale)
            center = (float(parts[3]) * scale, float(parts[4]) * scale)
            clockwise = _is_cw(parts[5]) if len(parts) > 5 else False
            arc = arc_points_ends(ring[-1], end, center, clockwise)
            ring.extend(arc[1:])
        elif op == "OE":
            store()
        elif op == "SE":
            if ring:
                store()
            break
        else:
            index -= 1
            break
    geoms = []
    largest = None
    largest_area = -1.0
    for outer, inner in islands:
        geom = _geom_from_points(outer, inner)
        if geom is None:
            continue
        geoms.append(geom)
        area = abs(shoelace(outer))
        if area > largest_area:
            largest_area = area
            largest = (outer, inner)
    geom = _clean(unary_union(geoms)) if geoms else None
    if largest is None:
        return geom, [], [], index
    outer, inner = largest
    if holes:
        inner = [*inner, *holes]
        geom = _clean(_geom_from_points(outer, inner))
    return geom, outer, inner, index


class _FileParser:
    def __init__(self, job: Path | None, allow_custom: bool):
        self.job = job
        self.allow_custom = allow_custom
        self.unit = "INCH"
        self.symbols: dict[int, Symbol] = {}
        self.names: dict[int, str] = {}
        self.strings: dict[int, str] = {}
        self.unknown = 0
        self._custom: dict[str, BaseGeometry | None] = {}

    def parse(self, text: str) -> list[Feat]:
        rows = _lines(text)
        for line in rows:
            parts = line.split()
            if not parts:
                continue
            head = parts[0].upper()
            if head in {"L", "P", "A", "T", "B", "S"}:
                break
            if head in {"U", "UNITS"} or head.startswith("UNITS"):
                token = parts[1] if head == "U" and len(parts) > 1 else line.split("=", 1)[-1]
                self.unit = _unit_name(token.split()[0])
        features: list[Feat] = []
        index = 0
        cursor = 0
        while cursor < len(rows):
            line = rows[cursor]
            body, raw_attrs = _split_attr(line)
            parts = body.split()
            if not parts:
                cursor += 1
                continue
            head = parts[0]
            if head.upper() in {"U", "UNITS"}:
                token = parts[1] if len(parts) > 1 and head.upper() == "U" else body.split("=", 1)[-1]
                self.unit = _unit_name(token)
                cursor += 1
                continue
            if head.startswith("$") and len(parts) >= 2:
                number = int(head[1:] or parts[1])
                token = parts[1] if head != "$" else parts[1]
                if head == "$":
                    number = int(parts[1])
                    token = parts[2] if len(parts) > 2 else ""
                    suffix = parts[3] if len(parts) > 3 else None
                else:
                    # `$0 r600` or `$0 r600 M`
                    suffix = parts[2] if len(parts) > 2 else None
                if suffix is not None and suffix.upper()[:1] not in {"I", "M"}:
                    suffix = None
                self.symbols[number] = _classify_symbol(token, self.unit, suffix)
                cursor += 1
                continue
            if head.startswith("@") or head.startswith("&"):
                self._header_table(head, body)
                cursor += 1
                continue
            kind = head.upper()
            if kind not in {"L", "P", "A", "T", "B", "S"}:
                cursor += 1
                continue
            attrs = _resolve_attrs(raw_attrs, self.names, self.strings)
            feature_index = index
            index += 1
            if kind in {"T", "B"}:
                cursor += 1
                continue
            if kind == "S":
                polarity = parts[1].upper()[:1] if len(parts) > 1 else "P"
                scale = 25.4 if self.unit == "INCH" else 1.0
                geom, outer, holes, cursor = _read_surface(rows, cursor + 1, scale)
                features.append(
                    Feat(
                        index=feature_index,
                        record="surface",
                        polarity=polarity if polarity in {"P", "N"} else "P",
                        geom=geom,
                        points=outer,
                        holes=holes,
                        attrs=attrs,
                    )
                )
                continue
            cursor += 1
            feat = self._vector(kind, parts[1:], feature_index, attrs)
            if feat is not None:
                features.append(feat)
        return features

    def _header_table(self, head: str, body: str) -> None:
        text = body[1:].strip()
        if "=" in text and head[0] in text:
            number, value = text.split("=", 1) if text[0].isdigit() or text.startswith("-") else (None, None)
        else:
            number = None
        if text[:1].isdigit() or (text.startswith("-") and "=" in text):
            if "=" in text:
                number_text, value = text.split("=", 1)
            else:
                bits = text.split(None, 1)
                if len(bits) < 2:
                    return
                number_text, value = bits
            if not _is_int(number_text.strip()):
                return
            target = self.names if head.startswith("@") else self.strings
            target[int(number_text)] = value.strip()

    def _symbol(self, number: int, resize: float) -> Symbol | None:
        sym = self.symbols.get(number)
        if sym is None:
            self.unknown += 1
            return None
        return _with_resize(sym, symbol_mm(resize, self.unit) if resize else 0.0)

    def _vector(self, kind: str, tokens: list[str], feature_index: int, attrs: dict[str, str]) -> Feat | None:
        try:
            if kind == "P":
                return self._pad(tokens, feature_index, attrs)
            if kind == "L":
                return self._line(tokens, feature_index, attrs)
            if kind == "A":
                return self._arc(tokens, feature_index, attrs)
        except (IndexError, ValueError):
            self.unknown += 1
            return None
        return None

    def _pad(self, tokens: list[str], feature_index: int, attrs: dict[str, str]) -> Feat | None:
        x = float(tokens[0])
        y = float(tokens[1])
        cursor = 2
        resize = 0.0
        if tokens[cursor] == "-1":
            number = int(float(tokens[cursor + 1]))
            resize = float(tokens[cursor + 2])
            cursor += 3
        else:
            number = int(float(tokens[cursor]))
            cursor += 1
        polarity = tokens[cursor].upper()[:1]
        orient = _parse_orient(tokens[cursor + 2 :])
        sym = self._symbol(number, resize)
        if sym is None:
            return None
        center = (self._scale(x), self._scale(y))
        placed = self._place_symbol(sym, center, orient[0], orient[1])
        if placed is None:
            if sym.kind != "custom":
                self.unknown += 1
            return None
        points, holes, feature_mm, geom = placed
        diameter = sym.dims[0] if sym.kind in {"r", "hole"} and sym.dims else None
        return Feat(
            index=feature_index,
            record="pad",
            polarity=polarity if polarity in {"P", "N"} else "P",
            geom=geom,
            feature_mm=feature_mm,
            center=center,
            points=points,
            holes=holes,
            diameter=diameter,
            attrs=attrs,
        )

    def _line(self, tokens: list[str], feature_index: int, attrs: dict[str, str]) -> Feat | None:
        xs, ys, xe, ye = (self._scale(float(tokens[i])) for i in range(4))
        sym = self._symbol(int(float(tokens[4])), 0.0)
        polarity = tokens[5].upper()[:1]
        if sym is None:
            return None
        stroke = _stroke_width(sym)
        if stroke is None:
            self.unknown += 1
            return None
        width, cap = stroke
        centerline = [(xs, ys), (xe, ye)]
        points = buffer_centerline(centerline, width, cap)
        return Feat(
            index=feature_index,
            record="line",
            polarity=polarity if polarity in {"P", "N"} else "P",
            geom=_geom_from_points(points) if points else None,
            feature_mm=width,
            points=points,
            centerline=centerline,
            attrs=attrs,
        )

    def _arc(self, tokens: list[str], feature_index: int, attrs: dict[str, str]) -> Feat | None:
        xs, ys, xe, ye, xc, yc = (self._scale(float(tokens[i])) for i in range(6))
        sym = self._symbol(int(float(tokens[6])), 0.0)
        polarity = tokens[7].upper()[:1]
        clockwise = _is_cw(tokens[9]) if len(tokens) > 9 else False
        if sym is None:
            return None
        stroke = _stroke_width(sym)
        if stroke is None:
            self.unknown += 1
            return None
        width, _cap = stroke
        centerline = arc_points_ends((xs, ys), (xe, ye), (xc, yc), clockwise)
        points = buffer_centerline(centerline, width, "round")
        return Feat(
            index=feature_index,
            record="arc",
            polarity=polarity if polarity in {"P", "N"} else "P",
            geom=_geom_from_points(points) if points else None,
            feature_mm=width,
            points=points,
            centerline=centerline,
            attrs=attrs,
        )

    def _scale(self, value: float) -> float:
        return value * 25.4 if self.unit == "INCH" else value

    def _place_symbol(self, sym: Symbol, center, rotation: float, mirror: bool):
        if sym.kind == "custom":
            geom = self._custom_geom(sym.raw)
            if geom is None:
                return None
            if mirror:
                geom = shp_scale(geom, xfact=-1.0, origin=(0.0, 0.0))
            if abs(rotation) > 1e-9:
                geom = shp_rotate(geom, rotation, origin=(0.0, 0.0))
            geom = shp_translate(geom, center[0], center[1])
            geom = _clean(geom)
            if geom is None:
                return None
            parts = polygon_parts(geom)
            if not parts:
                return None
            points, holes = parts[0]
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            return points, holes, min(max(xs) - min(xs), max(ys) - min(ys)), geom
        placed = _symbol_points(sym, center, rotation, mirror)
        if placed is None:
            return None
        points, holes, feature_mm = placed
        geom = _geom_from_points(points, holes)
        if geom is None:
            return None
        return points, holes, feature_mm, geom

    def _custom_geom(self, name: str) -> BaseGeometry | None:
        if name in self._custom:
            return self._custom[name]
        if not self.allow_custom or self.job is None:
            self.unknown += 1
            return None
        symbols = _find_child(self.job, "symbols")
        folder = _find_child(symbols, name) if symbols is not None else None
        text = None
        if folder is not None:
            features = _find_child(folder, "features")
            text = _read_text(features) if features is not None else _read_text(folder / "features")
        if not text:
            self.unknown += 1
            self._custom[name] = None
            return None
        nested = _FileParser(self.job, allow_custom=False)
        geoms = [feat.geom for feat in nested.parse(text) if feat.polarity != "N" and feat.geom is not None]
        geom = _clean(unary_union(geoms)) if geoms else None
        if geom is None:
            self.unknown += 1
        self._custom[name] = geom
        return geom


def _assign_pairs(data: dict[str, str], text: str) -> None:
    if "\n" not in text and text.count("=") > 1:
        for key, value in re.findall(r"([A-Za-z0-9_]+)\s*=\s*(\S+)", text):
            data[key.upper()] = value
        return
    for line in text.splitlines() if "\n" in text else [text]:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip().upper()] = value.strip()


def _parse_matrix(text: str) -> tuple[str, list[dict], list[dict]]:
    unit = "INCH"
    steps: list[dict] = []
    layers: list[dict] = []
    current: str | None = None
    data: dict[str, str] = {}

    def finish() -> None:
        nonlocal current, data
        if current == "STEP" and data.get("NAME"):
            steps.append(data)
        elif current == "LAYER" and data.get("NAME"):
            layers.append(data)
        current = None
        data = {}

    for line in _lines(text):
        if current is None and "{" in line and "}" in line:
            head, rest = line.split("{", 1)
            body, _tail = rest.split("}", 1)
            current = head.strip().upper()
            data = {}
            _assign_pairs(data, body)
            finish()
            continue
        if current is None and line.endswith("{") and "=" not in line:
            current = line[:-1].strip().upper()
            data = {}
            continue
        if line == "}" and current is not None:
            finish()
            continue
        if current is None:
            head = line.upper()
            if head.startswith("U ") or head.startswith("UNITS"):
                token = line.split("=", 1)[-1] if "=" in line else line.split(None, 1)[-1]
                unit = _unit_name(token)
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            data[key.strip().upper()] = value.strip()
    return unit, steps, layers


def _parse_eda(text: str) -> dict[tuple[str, int], str]:
    assigned: dict[tuple[str, int], str] = {}
    lyr_names: list[str] = []
    current = ""
    for line in _lines(text):
        body, _attrs = _split_attr(line)
        parts = body.split()
        if not parts:
            continue
        record = parts[0].upper()
        if record == "LYR":
            lyr_names = parts[1:]
        elif record == "NET":
            name = parts[1] if len(parts) > 1 else ""
            current = "" if name in {"$NONE$", ""} else name.strip().strip('"')
        elif record == "FID" and len(parts) >= 4 and lyr_names:
            try:
                lyr = int(parts[2])
                feature = int(parts[3])
            except ValueError:
                continue
            if 0 <= lyr < len(lyr_names):
                assigned[(lyr_names[lyr], feature)] = current
    return assigned


def _parse_attrlist(text: str) -> tuple[str | None, dict[str, str]]:
    unit = None
    values: dict[str, str] = {}
    for line in _lines(text):
        if "=" in line:
            key, value = line.split("=", 1)
        else:
            bits = line.split(None, 1)
            if len(bits) < 2:
                continue
            key, value = bits
        key = key.strip().lstrip(".").lower()
        value = value.strip().strip('"')
        if key in {"units", "unit", "u"}:
            unit = _unit_name(value)
        else:
            values[key] = value
    return unit, values


def _pick(values: dict[str, str], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in values:
            continue
        match = _NUMBER.search(values[key])
        if match:
            return float(match.group(0))
    return None


def _lookup_net(table: dict[tuple[str, int], str], layer: str, index: int) -> str | None:
    if (layer, index) in table:
        return table[(layer, index)]
    for (name, feature), net in table.items():
        if feature == index and name.lower() == layer.lower():
            return net
    return None


def _apply_nets(features: list[Feat], table: dict[tuple[str, int], str], layer: str) -> None:
    for feat in features:
        named = _net_from_attrs(feat.attrs)
        if named is not None:
            feat.net = named
        override = _lookup_net(table, layer, feat.index)
        if override is not None:
            feat.net = override


def _kind(records: list[str]) -> str:
    if "surface" in records:
        return "zone"
    if "line" in records or "arc" in records:
        return "trace"
    return "pad"


def _commit_layer(
    board: Board,
    layer: str,
    polarity: str,
    features: list[Feat],
    outline: BaseGeometry | None,
    layer_net: str,
) -> None:
    positive = polarity != "NEGATIVE"
    if not positive and outline is None:
        board.warnings.append(
            f"Negative layer {layer} has no profile, so its features are drawn as copper."
        )
        positive = True

    groups: dict[str, list[Feat]] = {}
    extras: dict[str, list[BaseGeometry]] = {}
    cutouts: list[BaseGeometry] = []

    def copper_polarity(feat: Feat) -> bool:
        if positive:
            return feat.polarity != "N"
        return feat.polarity == "N"

    for feat in features:
        if feat.geom is None:
            continue
        if copper_polarity(feat):
            groups.setdefault(feat.net, []).append(feat)
        else:
            cutouts.append(feat.geom)

    if not positive and outline is not None:
        base = outline
        if cutouts:
            base = _clean(base.difference(_clean(unary_union(cutouts)) or base))
            cutouts = []
        if base is not None:
            extras.setdefault(layer_net, []).append(base)
        if not layer_net:
            board.warnings.append(f"Negative layer {layer} has no net, so its plane is unassigned.")

    cut = _clean(unary_union(cutouts)) if cutouts else None
    nets = set(groups) | set(extras)
    for net in nets:
        feats = groups.get(net, [])
        geoms = [feat.geom for feat in feats if feat.geom is not None]
        geoms.extend(extras.get(net, []))
        if not geoms:
            continue
        merged = _clean(unary_union(geoms))
        if merged is not None and cut is not None:
            merged = _clean(merged.difference(cut))
        if merged is None:
            continue
        records = [feat.record for feat in feats]
        if net in extras:
            records.append("surface")
        kind = _kind(records)
        widths = [feat.feature_mm for feat in feats if feat.feature_mm]
        feature_mm = None if kind == "zone" or not widths else min(widths)
        for points, holes in polygon_parts(merged):
            board.copper.append(
                CopperPoly(
                    points=points,
                    layer=layer,
                    net=net,
                    kind=kind,
                    holes=holes,
                    feature_mm=feature_mm,
                    source=f"{layer}:{net}:{kind}",
                )
            )
        remember_net(board, net)

    for feat in features:
        if feat.record != "pad" or not feat.points or feat.center is None:
            continue
        if not copper_polarity(feat):
            continue
        board.pads.append(
            Pad(
                footprint=layer,
                name=str(feat.index),
                net=feat.net,
                layers=[layer],
                center=feat.center,
                points=feat.points,
                source=f"{layer}:{feat.index}",
            )
        )
        remember_net(board, feat.net)


def _outline_from(features: list[Feat]) -> tuple[list | None, list[list], BaseGeometry | None]:
    surfaces = [feat for feat in features if feat.record == "surface" and feat.points and len(feat.points) >= 3]
    if surfaces:
        feat = max(surfaces, key=lambda item: abs(shoelace(item.points or [])))
        geom = feat.geom or _geom_from_points(feat.points or [], feat.holes)
        return feat.points, feat.holes, geom
    loops = chain_loops([feat.centerline for feat in features if feat.centerline])
    if not loops:
        return None, [], None
    loops.sort(key=lambda ring: abs(shoelace(ring)), reverse=True)
    geom = _geom_from_points(loops[0], loops[1:])
    return loops[0], loops[1:], geom


def _layer_attr(layer_dir: Path | None, job_unit: str) -> tuple[str, dict[str, str]]:
    if layer_dir is None:
        return job_unit, {}
    attr = _find_child(layer_dir, "attrlist")
    text = _read_text(attr) if attr is not None else _read_text(layer_dir / "attrlist")
    if not text:
        return job_unit, {}
    unit, values = _parse_attrlist(text)
    return unit or job_unit, values


def _to_mm(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    return value * 25.4 if unit == "INCH" else value


def load_odb(path: str | Path) -> Board:
    path = Path(path)
    root = find_job_root(path)
    if root is None:
        raise ValueError(
            "That folder is not an ODB++ job (no matrix/matrix). "
            "Open the job folder exported from Altium or Xpedition, or a .zip/.tgz/.tar of it."
        )
    return _load_job(root)


def _load_job(root: Path) -> Board:
    matrix = _find_child(_find_child(root, "matrix") or root, "matrix")
    text = _read_text(matrix) if matrix is not None else None
    if not text:
        raise ValueError("ODB++ matrix/matrix is missing or empty.")
    job_unit, steps, matrix_layers = _parse_matrix(text)
    step_name = _choose_step(root, steps)
    step_dir = _find_child(_find_child(root, "steps") or root, step_name)
    if step_dir is None:
        raise ValueError(f"ODB++ step '{step_name}' has no steps/{step_name} folder.")

    board = Board(name=root.name)
    copper_rows = []
    dielectrics = []
    drills = []
    masks = []
    for index, row in enumerate(matrix_layers):
        kind = row.get("TYPE", "").upper()
        name = row["NAME"]
        polarity = row.get("POLARITY", "POSITIVE").upper()
        order = int(float(row["ROW"])) if row.get("ROW") else index
        entry = (order, name, polarity, row)
        if kind in _COPPER_TYPES:
            copper_rows.append(entry)
        elif kind == "DIELECTRIC":
            dielectrics.append(entry)
        elif kind == "DRILL":
            drills.append(entry)
        elif kind in _MASK_TYPES:
            masks.append(entry)
        elif kind in _SKIP_TYPES:
            continue
    copper_rows.sort(key=lambda item: item[0])
    if not copper_rows:
        raise ValueError("ODB++ job has no signal, plane, or mixed layers.")

    layers_dir = _find_child(step_dir, "layers")
    eda_dir = _find_child(step_dir, "eda")
    eda_file = _find_child(eda_dir, "data") if eda_dir is not None else None
    eda_text = _read_text(eda_file) if eda_file is not None else _read_text(step_dir / "eda" / "data")
    net_table = _parse_eda(eda_text) if eda_text else {}

    profile_file = _find_child(step_dir, "profile")
    profile_text = _read_text(profile_file) if profile_file is not None else _read_text(step_dir / "profile")
    outline_geom = None
    if profile_text:
        profile_features = _FileParser(root, allow_custom=True).parse(profile_text)
        outline, holes, outline_geom = _outline_from(profile_features)
        board.outline = outline
        board.holes.extend(holes)
    if board.outline is None:
        board.warnings.append("ODB++ profile did not form a closed outline. The copper bounding box is used instead.")

    stack: list[tuple[int, StackLayer]] = []
    for order, name, _polarity, _row in copper_rows:
        layer_dir = _find_child(layers_dir, name) if layers_dir is not None else None
        unit, attrs = _layer_attr(layer_dir, job_unit)
        thickness = _to_mm(_pick(attrs, _THICK_KEYS), unit)
        stack.append((order, StackLayer(name=name, kind="copper", thickness_mm=thickness if thickness else 0.035)))
        board.layers.append(Layer(id=len(board.layers), name=name, kind="signal"))
    for order, name, _polarity, _row in dielectrics:
        layer_dir = _find_child(layers_dir, name) if layers_dir is not None else None
        unit, attrs = _layer_attr(layer_dir, job_unit)
        thickness = _to_mm(_pick(attrs, _THICK_KEYS), unit) or 0.0
        eps = _pick(attrs, _EPS_KEYS)
        loss = _pick(attrs, _LOSS_KEYS)
        if thickness > 0 and eps is None:
            eps = 4.5
            if loss is None:
                loss = 0.02
            board.warnings.append(f"{name} has thickness but no dielectric constant. Using εr 4.5 and tanδ 0.02.")
        elif thickness > 0 and loss is None:
            loss = 0.02
        stack.append(
            (
                order,
                StackLayer(
                    name=name,
                    kind="dielectric",
                    thickness_mm=thickness,
                    eps_r=eps,
                    loss_tangent=loss,
                    material="FR4" if eps else None,
                ),
            )
        )
    for order, name, _polarity, _row in masks:
        layer_dir = _find_child(layers_dir, name) if layers_dir is not None else None
        unit, attrs = _layer_attr(layer_dir, job_unit)
        thickness = _to_mm(_pick(attrs, _THICK_KEYS), unit) or 0.0
        stack.append((order, StackLayer(name=name, kind="mask", thickness_mm=thickness)))
    ordered = [layer for _order, layer in sorted(stack, key=lambda item: item[0])]
    real = any(layer.kind == "dielectric" and layer.thickness_mm > 0 for layer in ordered)
    finish_stackup(board, ordered, real=real)

    unknown = 0
    slots = 0
    copper_names = [name for _order, name, _polarity, _row in copper_rows]
    for _order, name, polarity, _row in copper_rows:
        layer_dir = _find_child(layers_dir, name) if layers_dir is not None else None
        unit, attrs = _layer_attr(layer_dir, job_unit)
        layer_net = _net_from_attrs(attrs) or ""
        features: list[Feat] = []
        if layer_dir is not None:
            feat_file = _find_child(layer_dir, "features")
            feat_text = _read_text(feat_file) if feat_file is not None else _read_text(layer_dir / "features")
            if feat_text:
                parser = _FileParser(root, allow_custom=True)
                features = parser.parse(feat_text)
                unknown += parser.unknown
                _apply_nets(features, net_table, name)
        _commit_layer(board, name, polarity, features, outline_geom, layer_net)

    first_last = (copper_names[0], copper_names[-1])
    for _order, name, _polarity, row in drills:
        layer_dir = _find_child(layers_dir, name) if layers_dir is not None else None
        if layer_dir is None:
            continue
        feat_file = _find_child(layer_dir, "features")
        feat_text = _read_text(feat_file) if feat_file is not None else _read_text(layer_dir / "features")
        if not feat_text:
            continue
        parser = _FileParser(root, allow_custom=True)
        features = parser.parse(feat_text)
        unknown += parser.unknown
        _apply_nets(features, net_table, name)
        start = row.get("START_NAME") or first_last[0]
        end = row.get("END_NAME") or first_last[1]
        if start not in copper_names or end not in copper_names:
            start, end = first_last
        for feat in features:
            if feat.record in {"line", "arc"} or feat.diameter is None:
                if feat.record in {"line", "arc", "pad", "surface"}:
                    slots += 1
                continue
            if _is_npth(feat.attrs):
                board.holes.append(circle_world(feat.center or (0.0, 0.0), feat.diameter / 2))
                continue
            board.vias.append(
                Via(
                    x=feat.center[0] if feat.center else 0.0,
                    y=feat.center[1] if feat.center else 0.0,
                    drill_mm=feat.diameter,
                    size_mm=feat.diameter,
                    layers=(start, end),
                    net=feat.net,
                    source=f"{name}:{feat.index}",
                )
            )
            remember_net(board, feat.net)
    if unknown:
        board.warnings.append(f"Skipped {unknown} features with symbols this reader does not draw.")
    if slots:
        board.warnings.append(f"Skipped {slots} slotted or non-round drills.")
    return flip_y(board)


def _choose_step(root: Path, steps: list[dict]) -> str:
    names = [step.get("NAME", "") for step in steps if step.get("NAME")]
    for name in names:
        if name.lower() == "pcb":
            return name
    steps_dir = _find_child(root, "steps")
    if steps_dir is not None:
        folders = [child.name for child in steps_dir.iterdir() if child.is_dir()]
        for name in names:
            for folder in folders:
                if folder.lower() == name.lower():
                    return folder
        if len(folders) == 1:
            return folders[0]
        if folders:
            return folders[0]
    if names:
        return names[0]
    raise ValueError("ODB++ matrix has no STEP record.")
