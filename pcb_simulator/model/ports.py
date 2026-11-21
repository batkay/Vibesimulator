"""Lumped ports.

A port is a termination resistor between signal copper and reference copper,
plus a source when that port is the one being excited. The resistance is the
instrument impedance (50 Ω unless edited). It is not the trace impedance.

A vertical port is a zero-thickness sheet perpendicular to the trace, spanning
from the signal layer down to the reference layer. A two-click port is built
at the first click, on that one net. The second click names the reference.
If that net is already under the click, the sheet stays there, on the nearest
layer of that net. Otherwise it runs toward the click. It touches no other net.
"""

from __future__ import annotations

import math

from shapely.geometry import Point, box

from .board import Board, Pad
from .geom import as_polygon
from .mesh import PortBox

# One default copper cell. A sheet this wide fits on one USB-C pad.
SHEET_MM = 0.2
_SHEET_MM = SHEET_MM
_NARROW_MM = (0.2, 0.14, 0.08)

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


def port_at_click(
    board: Board,
    a: tuple[float, float, str],
    b: tuple[float, float, str],
    existing: list[PortBox],
    r_ohm: float = 50.0,
) -> tuple[PortBox | None, str]:
    """Sheet at the first click, reference taken from the second click.

    The first click is the signal net. The second click names the reference
    net. The sheet is one cell across, centered on the signal click, and it
    gets narrower when that is what keeps a third net out. If that reference
    net is already under the click, the sheet stays there and stops on the
    nearest layer of that net. Otherwise it runs toward the second click and
    stops once it is on that copper.
    """
    ax, ay, al = a
    bx, by, bl = b
    za = board.layer_z(al)
    zb = board.layer_z(bl)
    if za is None or zb is None:
        return None, "Those clicks are not on copper layers."
    pieces = _copper_pieces(board)
    signal = _net_on_layer(pieces, ax, ay, al)
    reference = _net_on_layer(pieces, bx, by, bl)
    if not signal:
        return None, "Click copper for the signal net."
    if not reference:
        return None, "Click the reference on copper."
    if signal == reference:
        return None, f"The reference has to be a different net. The first click was {signal}."
    local, ref_layer, local_reason = _local_span(board, pieces, ax, ay, al, signal, reference)
    if local is not None and ref_layer is not None:
        x0, y0, x1, y1 = local
        z_ref = board.layer_z(ref_layer)
        if z_ref is None:
            return None, "Those clicks are not on copper layers."
        if abs(za - z_ref) < 1e-6:
            exc = "x" if abs(x1 - x0) >= abs(y1 - y0) else "y"
            z0 = z1 = za
        else:
            exc = "z"
            z0, z1 = (za, z_ref) if za < z_ref else (z_ref, za)
    else:
        same_layer = abs(za - zb) < 1e-6
        if same_layer:
            span, reason = _same_layer_span(pieces, ax, ay, bx, by, al, signal, reference)
        else:
            span, reason = _vertical_span(board, pieces, ax, ay, bx, by, za, zb, signal, reference)
        if span is None:
            if local_reason and "touch" in local_reason and "touch" not in reason:
                return None, local_reason
            return None, reason or local_reason
        x0, y0, x1, y1 = span
        if same_layer:
            exc = "x" if abs(bx - ax) >= abs(by - ay) else "y"
            z0 = z1 = za
        else:
            exc = "z"
            z0, z1 = (za, zb) if za < zb else (zb, za)
    if abs((x0, y0, z0)[{"x": 0, "y": 1, "z": 2}[exc]] - (x1, y1, z1)[{"x": 0, "y": 1, "z": 2}[exc]]) < 1e-9:
        return None, "Those two clicks did not leave a gap for the port."
    port = PortBox(
        name=_next_name(existing),
        r_ohm=r_ohm,
        exc_dir=exc,
        start=(x0, y0, z0),
        stop=(x1, y1, z1),
        net=signal,
        reference_net=reference,
        marks=((ax, ay), (bx, by)),
    )
    return port, ""


def _copper_pieces(board: Board) -> list[tuple[str, str, str, object]]:
    pieces = []
    for poly in board.copper:
        if not poly.net:
            continue
        geom = _poly(poly.points, poly.holes)
        if geom is None or geom.is_empty:
            continue
        pieces.append((poly.layer, poly.net, poly.kind, geom))
    return pieces


def _net_on_layer(pieces, x: float, y: float, layer: str) -> str:
    """Feature copper wins over a pour when both cover the click."""
    point = Point(x, y)
    feature = ""
    zone = ""
    for layer_name, net, kind, geom in pieces:
        if layer_name != layer or not geom.covers(point):
            continue
        if kind == "zone":
            zone = zone or net
        else:
            feature = feature or net
    return feature or zone


def _nets_in(pieces, rect, layers: set[str]) -> set[str]:
    found: set[str] = set()
    for layer_name, net, _kind, geom in pieces:
        if layer_name not in layers:
            continue
        if geom.intersection(rect).area > 1e-8:
            found.add(net)
    return found


def _local_span(board, pieces, ax, ay, signal_layer, signal, reference):
    """Small sheet at the signal click when that reference net is already there.

    The reference layer is the one closest to the signal layer. Returns
    ``(bounds, layer, "")`` or ``(None, None, reason)``.
    """
    za = board.layer_z(signal_layer)
    if za is None:
        return None, None, "Those clicks are not on copper layers."
    ordered: list[tuple[float, float, str]] = []
    for name in board.copper_names_top_to_bottom():
        z = board.layer_z(name)
        if z is not None:
            ordered.append((abs(z - za), z, name))
    ordered.sort()
    reason = f"No {reference} copper under the first click."
    for width in _NARROW_MM:
        half = width / 2.0
        rect = box(ax - half, ay - half, ax + half, ay + half)
        hits = {name: _nets_in(pieces, rect, {name}) for _dist, _z, name in ordered}
        if signal not in hits.get(signal_layer, set()):
            return None, None, "The first click is not on that net."
        ref_layer = ""
        z_ref = za
        for _dist, z, name in ordered:
            if reference in hits.get(name, set()):
                ref_layer = name
                z_ref = z
                break
        if not ref_layer:
            continue
        lo, hi = (za, z_ref) if za < z_ref else (z_ref, za)
        extra: set[str] = set()
        for _dist, z, name in ordered:
            if z < lo - 1e-9 or z > hi + 1e-9:
                continue
            extra |= hits.get(name, set()) - {signal, reference}
        if extra:
            reason = f"That port would also touch {', '.join(sorted(extra))}."
            continue
        return rect.bounds, ref_layer, ""
    return None, None, reason


def _same_layer_span(pieces, ax, ay, bx, by, layer, signal, reference):
    return _aimed_span(pieces, ax, ay, bx, by, {layer}, signal, reference)


def _vertical_span(board, pieces, ax, ay, bx, by, za, zb, signal, reference):
    lo, hi = (za, zb) if za < zb else (zb, za)
    layers: set[str] = set()
    for name in board.copper_names_top_to_bottom():
        z = board.layer_z(name)
        if z is not None and lo - 1e-6 <= z <= hi + 1e-6:
            layers.add(name)
    return _aimed_span(pieces, ax, ay, bx, by, layers, signal, reference)


def _aimed_span(pieces, ax, ay, bx, by, layers: set[str], signal, reference):
    dx, dy = bx - ax, by - ay
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return None, "Click the reference away from the first point."
    horizontal = abs(dx) >= abs(dy)
    direction = 1.0 if (dx if horizontal else dy) >= 0 else -1.0
    start = ax if horizontal else ay
    aim = bx if horizontal else by
    reach = aim + direction * SHEET_MM
    reason = (
        f"No {reference} copper in line with that click. "
        "Click the reference beside the trace, or on another layer under it."
    )
    for width in _NARROW_MM:
        half = width / 2.0
        if horizontal:
            cross = (ay - half, ay + half)
        else:
            cross = (ax - half, ax + half)
        search = _strip(start, reach, cross, horizontal)
        end = _into_net(pieces, search, layers, reference, start, direction, horizontal)
        if end is None:
            continue
        back = _into_net(
            pieces,
            _strip(start, start - direction * SHEET_MM, cross, horizontal),
            layers,
            signal,
            start,
            -direction,
            horizontal,
        )
        origin = start if back is None else back
        rect = _strip(origin, end, cross, horizontal)
        nets = _nets_in(pieces, rect, layers)
        extra = nets - {signal, reference}
        if extra:
            reason = f"That port would also touch {', '.join(sorted(extra))}."
            continue
        if signal not in nets or reference not in nets:
            continue
        return rect.bounds, ""
    return None, reason


def _strip(origin: float, end: float, cross: tuple[float, float], horizontal: bool):
    lo, hi = min(origin, end), max(origin, end)
    c0, c1 = cross
    if horizontal:
        return box(lo, c0, hi, c1)
    return box(c0, lo, c1, hi)


def _into_net(pieces, strip, layers, net, start, direction, horizontal):
    """Coordinate `_SHEET_MM` past the near edge of `net`, toward `direction`."""
    wanted = {layers} if isinstance(layers, str) else set(layers)
    best = None
    for layer_name, piece_net, _kind, geom in pieces:
        if layer_name not in wanted or piece_net != net:
            continue
        piece = geom.intersection(strip)
        if piece.is_empty:
            continue
        minx, miny, maxx, maxy = piece.bounds
        lo, hi = (minx, maxx) if horizontal else (miny, maxy)
        if direction > 0:
            if hi <= start + 1e-9:
                continue
            near = max(lo, start)
            end = min(hi, near + SHEET_MM)
            dist = near - start
        else:
            if lo >= start - 1e-9:
                continue
            near = min(hi, start)
            end = max(lo, near - SHEET_MM)
            dist = start - near
        if abs(end - start) <= 1e-9:
            continue
        if best is None or dist < best[0]:
            best = (dist, end)
    return None if best is None else best[1]


def port_inside_region(port: PortBox, region) -> bool:
    mx = (port.start[0] + port.stop[0]) / 2
    my = (port.start[1] + port.stop[1]) / 2
    return bool(region.covers(Point(mx, my)) or region.intersects(Point(mx, my).buffer(1e-6)))


def port_length_mm(port: PortBox) -> float:
    return math.dist(port.start, port.stop)
