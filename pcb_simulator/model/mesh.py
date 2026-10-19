"""Cartesian mesh for openEMS.

Lines are millimeters in the same frame as the board. Cell counts are the
number of gaps between lines.

The background cell is a fraction of a wavelength. A trace is refined across
its width; along its length, and in open space, the cell stays at that
background size. With the default lattice, every copper edge snaps to one
pitch so a pad field cannot draw a private line at every corner. Pour
outlines are not seeded vertex by vertex, because a Cartesian line runs the
full width of the region. Z follows each dielectric slab. A trace-width cell
is not applied through the stackup or the air box.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .board import Board
from .crop import CroppedBoard

C0 = 299_792_458.0


@dataclass
class MeshSettings:
    f_max_hz: float = 5e9
    cells_per_wavelength: int = 10
    cells_per_feature: int = 2
    # Finest cell, in Z and on the exact-edge path. The copper lattice never goes below this.
    min_cell_mm: float = 0.05
    # Shared XY pitch at cells_per_feature 2. 0 seeds each feature on its own edges.
    copper_cell_mm: float = 0.2
    max_ratio: float = 1.4
    z_cells_per_dielectric: int = 4
    pml_cells: int = 8
    air_cells: int = 2


@dataclass
class PortBox:
    """Axis-aligned lumped port, before snapping onto mesh lines."""

    name: str
    r_ohm: float
    exc_dir: str
    start: tuple[float, float, float]
    stop: tuple[float, float, float]
    net: str = ""
    reference_net: str = ""
    footprint: str = ""
    pad: str = ""
    # The two clicks. The sheet is the axis-aligned box; these ends stay where the user clicked.
    marks: tuple[tuple[float, float], tuple[float, float]] | None = None

    def snapped(self, x_lines, y_lines, z_lines) -> "PortBox":
        start = _snap_corner(self.start, self.stop, self.exc_dir, x_lines, y_lines, z_lines, True)
        stop = _snap_corner(self.stop, self.start, self.exc_dir, x_lines, y_lines, z_lines, False)
        return PortBox(
            name=self.name,
            r_ohm=self.r_ohm,
            exc_dir=self.exc_dir,
            start=start,
            stop=stop,
            net=self.net,
            reference_net=self.reference_net,
            footprint=self.footprint,
            pad=self.pad,
            marks=self.marks,
        )


@dataclass
class Mesh:
    x: list[float]
    y: list[float]
    z: list[float]
    dx_mm: float
    dx_max_mm: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def cells(self) -> tuple[int, int, int]:
        return (max(len(self.x) - 1, 0), max(len(self.y) - 1, 0), max(len(self.z) - 1, 0))

    @property
    def cell_count(self) -> int:
        nx, ny, nz = self.cells
        return nx * ny * nz

    def ram_bytes_rough(self) -> int:
        """Order-of-magnitude FDTD memory. Labeled as rough in the UI."""
        return self.cell_count * 100


def _wavelength_dx(board: Board, settings: MeshSettings) -> float:
    eps = max((d.eps_r or 1.0) for d in board.dielectrics()) if board.dielectrics() else board.top_eps()
    eps = max(eps, 1.0)
    lam_mm = (C0 / settings.f_max_hz) / math.sqrt(eps) * 1e3
    return max(lam_mm / max(settings.cells_per_wavelength, 1), 1e-4)


def _feature_res(feature_mm: float | None, settings: MeshSettings, dx_max: float) -> float:
    if not feature_mm or feature_mm <= 0:
        return dx_max
    res = min(dx_max, max(feature_mm / max(settings.cells_per_feature, 1), 1e-4))
    floor = settings.min_cell_mm
    if floor > 0:
        res = max(res, min(floor, dx_max))
    return res


def _copper_pitch(settings: MeshSettings, dx_max: float) -> float:
    """One pitch for every copper edge. 0 keeps the exact feature edges.

    ``cells_per_feature`` 2 is ``copper_cell_mm``. A higher count tightens that
    pitch, and the result never drops below ``min_cell_mm``. Mixed pitches are
    what make the ratio pass cut the coarse cells back down.
    """
    if settings.copper_cell_mm <= 0:
        return 0.0
    pitch = settings.copper_cell_mm * 2.0 / max(settings.cells_per_feature, 1)
    floor = settings.min_cell_mm
    if floor > 0:
        pitch = max(pitch, min(floor, dx_max))
    return min(dx_max, max(pitch, 1e-4))


def target_dx(board: Board, cropped: CroppedBoard, settings: MeshSettings) -> float:
    """Finest cell the copper asks for. Open areas use the wavelength cell instead."""
    dx_max = _wavelength_dx(board, settings)
    pitch = _copper_pitch(settings, dx_max)
    if pitch > 0 and (cropped.copper or cropped.vias):
        return pitch
    fine = dx_max
    for poly in cropped.copper:
        if poly.kind == "zone":
            continue
        fine = min(fine, _feature_res(poly.feature_mm, settings, dx_max))
    for via in cropped.vias:
        fine = min(fine, _feature_res(via.drill_mm, settings, dx_max))
    return fine


def build_mesh(
    board: Board,
    cropped: CroppedBoard,
    ports: list[PortBox],
    settings: MeshSettings,
) -> Mesh:
    if settings.f_max_hz <= 0:
        raise ValueError("f_max must be positive")
    dx_max = _wavelength_dx(board, settings)
    dx_min = target_dx(board, cropped, settings)
    minx, miny, maxx, maxy = cropped.region.bounds
    margin = _air_margin(dx_min, dx_max, settings.max_ratio, settings.pml_cells + settings.air_cells)
    x0, x1 = minx - margin, maxx + margin
    y0, y1 = miny - margin, maxy + margin

    pitch = _copper_pitch(settings, dx_max)
    x_seeds: list[tuple[float, float]] = [(minx, dx_max), (maxx, dx_max)]
    y_seeds: list[tuple[float, float]] = [(miny, dx_max), (maxy, dx_max)]
    for poly in cropped.copper:
        if poly.kind == "zone":
            _seed_zone(poly, dx_max, x_seeds, y_seeds, pitch)
        else:
            _seed_extent(poly.points, poly.feature_mm, settings, dx_max, x_seeds, y_seeds)
    for via in cropped.vias:
        r = via.drill_mm / 2
        if pitch > 0:
            via_res = pitch
        else:
            via_res = _feature_res(via.drill_mm, settings, dx_max)
        _seed_edges(via.x - r, via.x + r, via_res, pitch, x_seeds)
        _seed_edges(via.y - r, via.y + r, via_res, pitch, y_seeds)
    for port in ports:
        port_res = pitch if pitch > 0 else dx_min
        _seed_edges(port.start[0], port.stop[0], port_res, pitch, x_seeds)
        _seed_edges(port.start[1], port.stop[1], port_res, pitch, y_seeds)

    x_lines = _axis(x_seeds, x0, x1, dx_min, dx_max, settings.max_ratio)
    y_lines = _axis(y_seeds, y0, y1, dx_min, dx_max, settings.max_ratio)
    z_lines = _z_lines(board, settings, dx_max)
    return Mesh(x=x_lines, y=y_lines, z=z_lines, dx_mm=dx_min, dx_max_mm=dx_max)


def _snap_pitch(value: float, pitch: float) -> float:
    if pitch <= 1e-9:
        return value
    return round(round(value / pitch) * pitch, 6)


def _seed_edges(lo: float, hi: float, res: float, pitch: float, seeds) -> None:
    """Two edges. A positive pitch snaps both onto that lattice, and not finer than it."""
    if pitch > 0:
        res = max(res, pitch)
    if abs(hi - lo) < 1e-9:
        pos = _snap_pitch(lo, pitch) if pitch > 0 else lo
        seeds.append((pos, res))
        return
    if hi < lo:
        lo, hi = hi, lo
    if pitch <= 0:
        seeds.append((lo, res))
        seeds.append((hi, res))
        return
    a = _snap_pitch(lo, pitch)
    b = _snap_pitch(hi, pitch)
    if b - a < pitch * 0.5:
        b = round(a + pitch, 6)
    seeds.append((a, res))
    seeds.append((b, res))


def _seed_extent(points, feature_mm, settings: MeshSettings, dx_max: float, x_seeds, y_seeds) -> None:
    """Seed the bounding box, not every arc vertex.

    A long trace is refined across its width. Along its length the cell stays
    at the wavelength size. On the shared lattice every edge lands on the same
    pitch, so corners that are a hair apart do not each become a grid line.
    """
    if len(points) < 2:
        return
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    pitch = _copper_pitch(settings, dx_max)
    res = pitch if pitch > 0 else _feature_res(feature_mm, settings, dx_max)
    short = min(span_x, span_y)
    long = max(span_x, span_y)
    long_res = dx_max
    if long >= max(short * 3.0, res * 4.0) and short > 0:
        if span_x <= span_y:
            _seed_edges(min(xs), max(xs), res, pitch, x_seeds)
            _seed_edges(min(ys), max(ys), long_res, pitch, y_seeds)
        else:
            _seed_edges(min(xs), max(xs), long_res, pitch, x_seeds)
            _seed_edges(min(ys), max(ys), res, pitch, y_seeds)
    else:
        _seed_edges(min(xs), max(xs), res, pitch, x_seeds)
        _seed_edges(min(ys), max(ys), res, pitch, y_seeds)


def _seed_zone(poly, dx_max: float, x_seeds, y_seeds, pitch: float = 0.0) -> None:
    """Seed the pour's bounding box only.

    A ground pour's outline follows every pad with thermal spokes and arc
    facets. Each of those vertices is a Cartesian grid line across the whole
    region, which is what made a small crop tens of thousands of MB. The pour
    is still meshed as metal; the lines are the board-scale extents. On the
    shared lattice those extents snap to the same pitch as the pads, so a pour
    edge a few micrometres off a pad does not pull the whole axis back down.
    """
    xs = [p[0] for p in poly.points]
    ys = [p[1] for p in poly.points]
    if not xs:
        return
    _seed_edges(min(xs), max(xs), dx_max, pitch, x_seeds)
    _seed_edges(min(ys), max(ys), dx_max, pitch, y_seeds)


def _air_margin(dx_min: float, dx_max: float, ratio: float, n_outer: int) -> float:
    """Room to grade from the finest copper cell out to the wavelength cell, then PML."""
    ramp = sum(_ramp_widths(dx_min, dx_max, ratio, 1e9))
    return ramp + max(n_outer, 1) * dx_max + dx_max


def _z_lines(board: Board, settings: MeshSettings, dz_max: float) -> list[float]:
    """Stackup cells follow each slab, not the lateral trace size.

    A 0.1 mm trace used to set dz for the dielectric and for the air above and
    below it. Each slab now gets `z_cells_per_dielectric` (more only when the
    slab is electrically thick). Air grades from that cell up to the wavelength
    cell so a thin prepreg does not refine the whole height.
    """
    zs = sorted({layer.z_mm for layer in board.stackup if layer.kind == "copper" and layer.z_mm is not None})
    if len(zs) < 2:
        base = zs[0] if zs else 0.0
        zs = [base, base + (board.thickness_mm or 1.6)]
    n_min = max(settings.z_cells_per_dielectric, 1)
    pts = [zs[0]]
    edge_dz: list[float] = []
    for z0, z1 in zip(zs, zs[1:]):
        thickness = z1 - z0
        if thickness <= 1e-9:
            continue
        n = max(n_min, math.ceil(thickness / dz_max))
        floor = settings.min_cell_mm
        if floor > 0 and thickness / n < floor:
            n = max(1, math.floor(thickness / floor + 1e-9))
        dz = thickness / n
        edge_dz.append(dz)
        for i in range(1, n + 1):
            pts.append(z0 + dz * i)
    if not edge_dz:
        edge_dz = [dz_max]
    n_outer = max(settings.pml_cells, 0) + max(settings.air_cells, 0)
    below = _air_points(zs[0], -1.0, edge_dz[0], dz_max, settings.max_ratio, n_outer)
    above = _air_points(pts[-1], 1.0, edge_dz[-1], dz_max, settings.max_ratio, n_outer)
    # Slabs stay on their own cell size. Grading a 0.1 mm prepreg's cell through
    # the core refines the whole stack, and the copper sheet between them is
    # the one place a dz step does not have to be smooth.
    return _merge(below + pts + above, 1e-6)


def _air_points(origin: float, direction: float, start_dz: float, dz_max: float, ratio: float, n_outer: int) -> list[float]:
    pts: list[float] = []
    z = origin
    cell = min(dz_max, max(start_dz, 1e-6) * max(ratio, 1.01))
    for _ in range(80):
        if cell >= dz_max * 0.999:
            break
        z = z + direction * cell
        pts.append(z)
        cell = min(dz_max, cell * max(ratio, 1.01))
    for _ in range(max(n_outer, 1)):
        z = z + direction * dz_max
        pts.append(z)
    if direction < 0:
        pts.reverse()
    return pts


def _axis(
    seeds: list[tuple[float, float]],
    lo: float,
    hi: float,
    dx_min: float,
    dx_max: float,
    ratio: float,
) -> list[float]:
    """Grade from each seed's own cell size up to `dx_max`.

    Buffered traces drop vertices much closer than a cell. Merging those to a
    third of the local cell keeps a sub-cell sliver from refining the board.
    Coarse anchors (board edges, pour extents) use a tiny tolerance so a third
    of a wavelength cannot swallow a nearby trace.
    """
    if hi < lo:
        lo, hi = hi, lo
    dx_max = max(dx_max, 1e-4)
    dx_min = min(max(dx_min, 1e-4), dx_max)
    if hi - lo < 1e-9:
        hi = lo + dx_max
    items = [(lo, dx_max), (hi, dx_max)]
    for pos, res in seeds:
        if lo - 1e-9 <= pos <= hi + 1e-9:
            pos = min(max(pos, lo), hi)
            res = min(max(res, 1e-4), dx_max)
            items.append((pos, res))
    items.sort(key=lambda item: item[0])
    merged: list[tuple[float, float]] = []
    for pos, res in items:
        if not merged:
            merged.append((pos, res))
            continue
        prev, prev_res = merged[-1]
        tol = min(prev_res, res) / 3.0
        # Two coarse edges a hair apart are not a trace. Leaving the sliver
        # makes the ratio pass tile that hair across the whole axis.
        if min(prev_res, res) >= dx_max * 0.99:
            tol = dx_min / 3.0
        if pos - prev <= tol:
            if res < prev_res - 1e-12:
                merged[-1] = (pos, res)
            else:
                merged[-1] = (prev, min(prev_res, res))
        else:
            merged.append((pos, res))
    if len(merged) == 1:
        merged = [(lo, dx_max), (hi, dx_max)]
    merged = _relax_seed_res(merged, dx_max, ratio)
    res_of = {round(pos, 6): res for pos, res in merged}
    lines = _smooth([pos for pos, _res in merged], dx_max, ratio, 1e-6, res_of=res_of, dx_min=dx_min)
    return _enforce_ratio(lines, dx_max, ratio)


def _reachable_edge(origin: float, length: float, ratio: float, dx: float) -> float:
    """Coarsest cell that can sit `length` away from a cell of size `origin`."""
    origin = min(max(origin, 1e-6), dx)
    if length <= origin * 1.02:
        return min(dx, max(length, 1e-6))
    cell = origin
    used = 0.0
    reached = origin
    for _ in range(64):
        nxt = min(dx, cell * max(ratio, 1.01))
        if used + nxt > length + 1e-9:
            room = length - used
            if room >= cell / ratio - 1e-9:
                return min(dx, max(reached, min(nxt, room)))
            return min(dx, reached)
        used += nxt
        cell = nxt
        reached = cell
        if cell >= dx * 0.999:
            return dx
    return min(dx, reached)


def _relax_seed_res(
    merged: list[tuple[float, float]],
    dx: float,
    ratio: float,
) -> list[tuple[float, float]]:
    """Pull a coarse seed down when the next feature is too close to ramp to it.

    The ramp then lives in the long gap. A short gap between a pour edge and a
    pad stays a few feature cells instead of a single wavelength cell.
    """
    if len(merged) < 2:
        return merged
    pos = [p for p, _res in merged]
    res = [min(max(r, 1e-6), dx) for _p, r in merged]
    for _ in range(max(len(res) * 2, 2)):
        changed = False
        for i in range(len(res) - 1):
            length = pos[i + 1] - pos[i]
            if length <= 1e-9:
                continue
            cap_here = _reachable_edge(res[i + 1], length, ratio, dx)
            cap_next = _reachable_edge(res[i], length, ratio, dx)
            if cap_here < res[i] - 1e-6:
                res[i] = cap_here
                changed = True
            if cap_next < res[i + 1] - 1e-6:
                res[i + 1] = cap_next
                changed = True
        if not changed:
            break
    return list(zip(pos, res))


def _merge(pts: list[float], tol: float) -> list[float]:
    ordered = sorted(pts)
    out = [ordered[0]]
    for value in ordered[1:]:
        if value - out[-1] >= tol:
            out.append(value)
    return out


def _relax_subdx(pts: list[float], dx: float, ratio: float) -> list[float]:
    """Split feature-scale gaps so neighbors stay within `ratio`.

    Gaps wider than dx are left for `_smooth`. Splitting those here walks the
    fine spacing across the whole board.
    """
    pts = list(pts)
    for _ in range(max(len(pts) * 6, 8)):
        gaps = [pts[i + 1] - pts[i] for i in range(len(pts) - 1)]
        moved = False
        for i in range(len(gaps) - 1):
            left, right = gaps[i], gaps[i + 1]
            if left > dx * 1.02 or right > dx * 1.02:
                continue
            if max(left, right) / min(left, right) <= ratio * 1.02:
                continue
            if right >= left:
                small, large = left, right
                sign = 1.0
            else:
                small, large = right, left
                sign = -1.0
            piece = min(small * ratio, large / 2)
            remain = large - piece
            if piece < small / ratio or remain < piece / ratio:
                continue
            pts = _merge(pts + [pts[i + 1] + sign * piece], 1e-6)
            moved = True
            break
        if not moved:
            break
    return pts


def _cells_fit(cells: list[float], start_res: float, stop_res: float, dx: float, ratio: float) -> bool:
    if not cells or max(cells) > dx * 1.02:
        return False
    if cells[0] > start_res * ratio * 1.02 or cells[-1] > stop_res * ratio * 1.02:
        return False
    if cells[0] < start_res / ratio / 1.02 or cells[-1] < stop_res / ratio / 1.02:
        return False
    return all(max(a, b) / min(a, b) <= ratio * 1.02 for a, b in zip(cells, cells[1:]))


def _ramp_widths(res: float, dx: float, ratio: float, limit: float) -> list[float]:
    cell = min(dx, res * ratio)
    widths = []
    pos = 0.0
    while cell < dx * 0.999 and pos + cell < limit - 1e-9:
        widths.append(cell)
        pos += cell
        cell = min(dx, cell * ratio)
    return widths


def _short_cells(length: float, start_res: float, stop_res: float, dx: float, ratio: float) -> list[float] | None:
    n0 = max(int(math.ceil(length / dx)), 1)
    fine = max(min(start_res, stop_res), length * 1e-6)
    # A feature-length gap is shorter than a full ramp to dx, so the search has
    # to reach a cell near the feature size or the fallback is one coarse cell.
    n_hi = max(n0 + 48, int(math.ceil(length / max(fine, 1e-6))))
    n_hi = min(n_hi, n0 + 2000)
    for n in range(n0, n_hi + 1):
        step = length / n
        if _cells_fit([step] * n, start_res, stop_res, dx, ratio):
            return [step] * n
        for k in range(1, 25):
            growth = 1.0 + (ratio - 1.0) * k / 24.0
            if abs(growth - 1.0) < 1e-9:
                continue
            factor = (growth**n - 1.0) / (growth - 1.0)
            first = length / factor
            forward = [first * growth**i for i in range(n)]
            if _cells_fit(forward, start_res, stop_res, dx, ratio):
                return forward
            backward = list(reversed(forward))
            if _cells_fit(backward, start_res, stop_res, dx, ratio):
                return backward
    return None


def _grade_segment(
    start: float,
    stop: float,
    start_res: float,
    stop_res: float,
    dx: float,
    ratio: float,
) -> list[float]:
    length = stop - start
    if length <= 1e-9:
        return [start, stop]
    start_res = min(max(start_res, length * 1e-9), dx)
    stop_res = min(max(stop_res, length * 1e-9), dx)
    # `dx` is the coarsest cell. A trace narrower than that still needs its own
    # cells; stopping at `dx` would put one cell across the copper.
    fine = min(start_res, stop_res)
    if length <= fine * 1.02:
        return [start, stop]
    left = _ramp_widths(start_res, dx, ratio, length)
    right = _ramp_widths(stop_res, dx, ratio, length)
    cells = None
    if sum(left) + sum(right) < length - 1e-9:
        mid = length - sum(left) - sum(right)
        n = max(int(math.ceil(mid / dx)), 1)
        step = mid / n
        edge_l = left[-1] if left else step
        edge_r = right[-1] if right else step
        if step <= dx * 1.02 and step >= edge_l / ratio / 1.02 and step >= edge_r / ratio / 1.02:
            cells = left + [step] * n + list(reversed(right))
    if cells is not None and not _cells_fit(cells, start_res, stop_res, dx, ratio):
        cells = None
    if cells is None:
        cells = _short_cells(length, start_res, stop_res, dx, ratio)
    if cells is None:
        n = max(int(math.ceil(length / fine)), 1) if fine > 0 else 1
        uniform = [length / n] * n
        if _cells_fit(uniform, start_res, stop_res, dx, ratio):
            cells = uniform
    if cells is None:
        cells = _bridge(length, start_res, stop_res, dx, ratio)
    if cells is None:
        cells = _grow_fill(length, start_res, stop_res, dx, ratio)
    points = [start]
    acc = start
    for width in cells[:-1]:
        acc += width
        points.append(acc)
    points.append(stop)
    return _merge(points, 1e-6)


def _smooth(
    pts: list[float],
    dx: float,
    ratio: float,
    tol: float,
    res_of: dict[float, float] | None = None,
    dx_min: float | None = None,
) -> list[float]:
    """Fill gaps, grading from each seed's cell size up to dx."""
    fine = dx if dx_min is None else min(dx_min, dx)
    fixed = _relax_subdx(_merge(pts, tol), fine, ratio)
    resolved = _res_along(fixed, res_of or {}, fine)
    out = [fixed[0]]
    for i, (a, b) in enumerate(zip(fixed, fixed[1:])):
        segment = _grade_segment(a, b, resolved[i], resolved[i + 1], dx, ratio)
        out.extend(segment[1:])
    return _merge(out, 1e-6)


def _res_along(pts: list[float], res_of: dict[float, float], default: float) -> list[float]:
    """Resolution at each point. Inserted points take the finer neighbor seed."""
    known = [res_of.get(round(p, 6)) for p in pts]
    out: list[float] = []
    for i in range(len(pts)):
        if known[i] is not None:
            out.append(known[i])
            continue
        left = next((known[j] for j in range(i - 1, -1, -1) if known[j] is not None), None)
        right = next((known[j] for j in range(i + 1, len(known)) if known[j] is not None), None)
        if left is None and right is None:
            out.append(default)
        elif left is None:
            out.append(right)
        elif right is None:
            out.append(left)
        else:
            out.append(min(left, right))
    return out


def _ratios_ok(cells: list[float], near: float, far: float, dx: float, ratio: float) -> bool:
    if not cells:
        return False
    limit = ratio * 1.02
    prev = near
    for cell in cells:
        if cell <= 1e-9 or cell > dx * 1.05:
            return False
        if max(prev, cell) / min(prev, cell) > limit:
            return False
        prev = cell
    return max(prev, far) / min(prev, far) <= limit


def _bridge(length: float, near: float, far: float, dx: float, ratio: float) -> list[float] | None:
    """Cells that sum to `length`, matching `near` on the start side and `far` on the end.

    Returns None when the span is too short for those two sizes. The caller
    then includes another coarse cell and tries again, instead of cutting
    every background cell in half.
    """
    if length <= 1e-9:
        return None
    ratio = max(ratio, 1.01)
    near = min(max(near, 1e-6), dx)
    far = min(max(far, 1e-6), dx)

    def accept(seq: list[float]) -> list[float] | None:
        if abs(sum(seq) - length) > max(1e-3, length * 1e-6):
            scale = length / sum(seq)
            seq = [c * scale for c in seq]
        return seq if _ratios_ok(seq, near, far, dx, ratio) else None

    single = accept([length])
    if single is not None:
        return single

    max_n = min(80, max(int(math.ceil(length / max(min(near, far), 1e-4))) + 2, 2))
    for n in range(2, max_n + 1):
        found = accept([length / n] * n)
        if found is not None:
            return found

    for n in range(2, max_n + 1):
        for k in range(1, 28):
            growth = 1.0 + (ratio - 1.0) * k / 27.0
            factor = (growth**n - 1.0) / (growth - 1.0)
            first = length / factor
            forward = [first * growth**i for i in range(n)]
            found = accept(forward)
            if found is not None:
                return found
            found = accept(list(reversed(forward)))
            if found is not None:
                return found
    return None


def _enforce_ratio(pts: list[float], dx: float, ratio: float) -> list[float]:
    """Smooth a jump between two segments.

    The coarse side is rebuilt as one ramp that ends on the wavelength cell.
    Cutting that cell in half would walk the cut through the whole margin.
    """
    pts = _merge(list(pts), 1e-6)
    if len(pts) < 3:
        return pts
    limit = max(ratio, 1.01) * 1.02
    for _ in range(max(len(pts), 8)):
        gaps = [pts[i + 1] - pts[i] for i in range(len(pts) - 1)]
        target = None
        worst = limit
        for i in range(len(gaps) - 1):
            left, right = gaps[i], gaps[i + 1]
            if left <= 1e-9 or right <= 1e-9:
                continue
            jump = max(left, right) / min(left, right)
            if jump > worst:
                worst = jump
                target = i
        if target is None:
            return pts
        repaired = _repair_join(pts, target, dx, ratio)
        if repaired is None:
            return pts
        pts = repaired
        if len(pts) > 20000:
            return pts
    return pts


def _repair_join(pts: list[float], index: int, dx: float, ratio: float) -> list[float] | None:
    left = pts[index + 1] - pts[index]
    right = pts[index + 2] - pts[index + 1]
    if right >= left:
        return _repair_side(pts, index + 1, 1, left, dx, ratio)
    return _repair_side(pts, index + 1, -1, right, dx, ratio)


def _repair_side(
    pts: list[float],
    boundary: int,
    direction: int,
    small: float,
    dx: float,
    ratio: float,
) -> list[float] | None:
    start = boundary
    end = start + direction
    for _ in range(48):
        if end < 0 or end >= len(pts) or end == start:
            return None
        span = abs(pts[end] - pts[start])
        far_index = end + direction
        if 0 <= far_index < len(pts):
            far = abs(pts[far_index] - pts[end])
        else:
            far = dx
        cells = _bridge(span, small, min(far, dx), dx, ratio)
        if cells is not None:
            return _splice(pts, start, end, direction, cells)
        nxt = end + direction
        if nxt < 0 or nxt >= len(pts):
            return None
        if abs(pts[nxt] - pts[end]) < small / ratio:
            return None
        end = nxt
    return None


def _splice(pts: list[float], start: int, end: int, direction: int, cells: list[float]) -> list[float]:
    if direction > 0:
        acc = pts[start]
        mids = []
        for width in cells[:-1]:
            acc += width
            mids.append(acc)
        return _merge(pts[: start + 1] + mids + pts[end:], 1e-6)
    cursor = pts[start]
    seq = []
    for width in cells[:-1]:
        cursor -= width
        seq.append(cursor)
    seq.reverse()
    return _merge(pts[: end + 1] + seq + pts[start:], 1e-6)


def _grow_fill(length: float, start_res: float, stop_res: float, dx: float, ratio: float) -> list[float]:
    """Cover `length` when a full ramp to dx does not fit. Adjacent cells stay within `ratio`."""
    if stop_res < start_res:
        return list(reversed(_grow_fill(length, stop_res, start_res, dx, ratio)))
    cells: list[float] = []
    remaining = length
    prev = min(max(start_res, 1e-6), dx)
    for _ in range(100000):
        if remaining <= 1e-8:
            break
        cell = min(dx, prev * ratio, remaining)
        leftover = remaining - cell
        if 1e-8 < leftover < cell / ratio:
            cell = remaining
        cells.append(cell)
        remaining -= cell
        prev = cell
        if cell >= dx * 0.999 and remaining > 1e-8:
            n = int(remaining // dx)
            for _i in range(n):
                cells.append(dx)
                remaining -= dx
            if remaining > 1e-8:
                if cells and remaining < dx / ratio:
                    cells[-1] += remaining
                else:
                    cells.append(remaining)
            break
    return cells or [length]


def _nearest(value: float, lines: list[float]) -> float:
    return min(lines, key=lambda line: abs(line - value))


def _snap_corner(corner, other, exc_dir, x_lines, y_lines, z_lines, is_start: bool):
    axes = ("x", "y", "z")
    linesets = (x_lines, y_lines, z_lines)
    snapped = []
    for index, axis in enumerate(axes):
        if abs(corner[index] - other[index]) < 1e-9:
            snapped.append(_nearest(corner[index], linesets[index]))
        else:
            snapped.append(_nearest(corner[index], linesets[index]))
    # The caller snaps start first, then stop against the original port, so both
    # corners can collapse onto the same line. Push the stop corner to a neighbor.
    if not is_start:
        for index, axis in enumerate(axes):
            if axis != exc_dir and abs(corner[index] - other[index]) < 1e-9:
                snapped[index] = _nearest(other[index], linesets[index])
            if snapped[index] == _nearest(other[index], linesets[index]) and abs(corner[index] - other[index]) >= 1e-9:
                snapped[index] = _neighbor(linesets[index], snapped[index], corner[index] - other[index])
    return (snapped[0], snapped[1], snapped[2])


def _neighbor(lines: list[float], value: float, direction: float) -> float:
    if direction >= 0:
        higher = [line for line in lines if line > value + 1e-9]
        return higher[0] if higher else value
    lower = [line for line in lines if line < value - 1e-9]
    return lower[-1] if lower else value
