"""Lumped ports.

A port is a termination resistor between signal copper and reference copper,
plus a source when that port is the one being excited. The resistance is the
instrument impedance (50 Ω unless edited). It is not the trace impedance.

A vertical port is a zero-thickness sheet perpendicular to the trace, spanning
from the signal layer down to the reference layer. A same-layer port is the
line between the two clicks, with a thin sheet around that line.
"""

from __future__ import annotations

import math

from shapely.geometry import Point

from .board import Board, Pad
from .geom import as_polygon
from .mesh import PortBox

# Width of a same-layer sheet when the two clicks are in a straight line.
_SHEET_MM = 0.2

def _next_name(existing: list[PortBox]) -> str:
    used = {port.name for port in existing}
    n = 1
    while f"P{n}" in used:
        n += 1
    return f"P{n}"


def _poly(points, holes=None):
    return as_polygon(points, holes)


def _attached_inward(board: Board, pad: Pad) -> tuple[float, float]:
    pad_poly = _poly(pad.points)
    acc_x = 0.0
    acc_y = 0.0
    found = False
    if pad_poly is None:
        return (1.0, 0.0)
    for poly in board.copper:
        if poly.kind == "zone" or poly.net != pad.net or poly.source == pad.source:
            continue
        geom = _poly(poly.points, poly.holes)
        if geom is None or not geom.intersects(pad_poly.buffer(0.05)):
            continue
        acc_x += geom.centroid.x - pad.center[0]
        acc_y += geom.centroid.y - pad.center[1]
        found = True
    if not found or (abs(acc_x) < 1e-9 and abs(acc_y) < 1e-9):
        return (1.0, 0.0)
    if abs(acc_x) >= abs(acc_y):
        return (1.0 if acc_x >= 0 else -1.0, 0.0)
    return (0.0, 1.0 if acc_y >= 0 else -1.0)


def reference_under_pad(board: Board, pad: Pad, reference_net: str) -> tuple[str, float, str, float] | None:
    """Return (signal_layer, signal_z, ref_layer, ref_z) for the closest reference plane."""
    pad_poly = _poly(pad.points)
    if pad_poly is None:
        return None
    pad_layers = []
    for layer in pad.layers:
        z = board.layer_z(layer)
        if z is not None:
            pad_layers.append((layer, z))
    if not pad_layers:
        return None
    refs: list[tuple[str, float]] = []
    for poly in board.copper:
        if poly.net != reference_net:
            continue
        z = board.layer_z(poly.layer)
        if z is None:
            continue
        geom = _poly(poly.points, poly.holes)
        if geom is not None and geom.intersects(pad_poly):
            refs.append((poly.layer, z))
    best = None
    for sl, sz in pad_layers:
        for rl, rz in refs:
            if abs(sz - rz) < 1e-6:
                continue
            dist = abs(sz - rz)
            if best is None or dist < best[0]:
                best = (dist, sl, sz, rl, rz)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4]


def vertical_port_on_pad(
    board: Board,
    pad: Pad,
    reference_net: str,
    existing: list[PortBox],
    r_ohm: float = 50.0,
) -> PortBox | None:
    found = reference_under_pad(board, pad, reference_net)
    if found is None:
        return None
    _sl, sz, _rl, rz = found
    inward = _attached_inward(board, pad)
    xs = [p[0] for p in pad.points]
    ys = [p[1] for p in pad.points]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    z0, z1 = (rz, sz) if rz < sz else (sz, rz)
    if abs(z1 - z0) < 1e-9:
        return None
    if inward[0] != 0:
        length = max(maxx - minx, 1e-3)
        inset = max(length * 0.3, 0.02)
        outer = minx if inward[0] > 0 else maxx
        x = min(max(outer + inward[0] * inset, minx), maxx)
        cy = (miny + maxy) / 2
        half = max((maxy - miny) * 0.4, 0.025)
        start = (x, cy - half, z0)
        stop = (x, cy + half, z1)
    else:
        length = max(maxy - miny, 1e-3)
        inset = max(length * 0.3, 0.02)
        outer = miny if inward[1] > 0 else maxy
        y = min(max(outer + inward[1] * inset, miny), maxy)
        cx = (minx + maxx) / 2
        half = max((maxx - minx) * 0.4, 0.025)
        start = (cx - half, y, z0)
        stop = (cx + half, y, z1)
    return PortBox(
        name=_next_name(existing),
        r_ohm=r_ohm,
        exc_dir="z",
        start=start,
        stop=stop,
        net=pad.net,
        reference_net=reference_net,
        footprint=pad.footprint,
        pad=pad.name,
    )


def _between_clicks(ax: float, ay: float, bx: float, by: float) -> tuple[float, float, float, float]:
    """Sheet between the two clicks. A level pair gets a thin width so the ends stay on the clicks."""
    x0, x1 = min(ax, bx), max(ax, bx)
    y0, y1 = min(ay, by), max(ay, by)
    if abs(bx - ax) >= abs(by - ay):
        if y1 - y0 < _SHEET_MM:
            mid = (ay + by) / 2.0
            y0, y1 = mid - _SHEET_MM / 2.0, mid + _SHEET_MM / 2.0
    elif x1 - x0 < _SHEET_MM:
        mid = (ax + bx) / 2.0
        x0, x1 = mid - _SHEET_MM / 2.0, mid + _SHEET_MM / 2.0
    if x1 - x0 < 0.08:
        mid = (ax + bx) / 2.0
        x0, x1 = mid - 0.04, mid + 0.04
    if y1 - y0 < 0.08:
        mid = (ay + by) / 2.0
        y0, y1 = mid - 0.04, mid + 0.04
    return (x0, y0, x1, y1)


def port_between(
    board: Board,
    a: tuple[float, float, str],
    b: tuple[float, float, str],
    reference_net: str,
    existing: list[PortBox],
    r_ohm: float = 50.0,
    signal_net: str = "",
) -> PortBox | None:
    """Build a port from two clicks: (x, y, layer) each.

    The drawn ends are the clicks. On one layer the sheet runs from click to click.
    On two layers the sheet stays on the first click and the second click picks the layer.
    """
    ax, ay, al = a
    bx, by, bl = b
    za = board.layer_z(al)
    zb = board.layer_z(bl)
    if za is None or zb is None:
        return None
    same_layer = abs(za - zb) < 1e-6
    if same_layer:
        exc = "x" if abs(bx - ax) >= abs(by - ay) else "y"
        z0 = z1 = za
        x0, y0, x1, y1 = _between_clicks(ax, ay, bx, by)
    else:
        exc = "z"
        z0, z1 = (za, zb) if za < zb else (zb, za)
        half = _SHEET_MM / 2.0
        x0, x1 = ax - half, ax + half
        y0, y1 = ay - half, ay + half
    if abs((x0, y0, z0)[{"x": 0, "y": 1, "z": 2}[exc]] - (x1, y1, z1)[{"x": 0, "y": 1, "z": 2}[exc]]) < 1e-9:
        return None
    return PortBox(
        name=_next_name(existing),
        r_ohm=r_ohm,
        exc_dir=exc,
        start=(x0, y0, z0),
        stop=(x1, y1, z1),
        net=signal_net,
        reference_net=reference_net,
        marks=((ax, ay), (bx, by)),
    )


def port_inside_region(port: PortBox, region) -> bool:
    mx = (port.start[0] + port.stop[0]) / 2
    my = (port.start[1] + port.stop[1]) / 2
    return bool(region.covers(Point(mx, my)) or region.intersects(Point(mx, my).buffer(1e-6)))


def port_length_mm(port: PortBox) -> float:
    return math.dist(port.start, port.stop)
