"""2D geometry helpers. Coordinates are millimeters, Y positive up, rotation CCW."""

from __future__ import annotations

import math

from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import triangulate


def rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    """Counterclockwise if +Y is up. x' = x cos − y sin, y' = x sin + y cos."""
    th = math.radians(deg)
    c, s = math.cos(th), math.sin(th)
    return (x * c - y * s, x * s + y * c)


def kicad_rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    """KiCad's RotatePoint. Positive degrees are counterclockwise on the Y-down canvas.

    x' = x cos + y sin, y' = −x sin + y cos.
    """
    return rotate(x, y, -deg)


def transform_point(
    x: float,
    y: float,
    origin: tuple[float, float],
    rotation_deg: float,
    flip_x: bool,
) -> tuple[float, float]:
    if flip_x:
        x = -x
    rx, ry = rotate(x, y, rotation_deg)
    return (origin[0] + rx, origin[1] + ry)


def _ring(local: list[tuple[float, float]], origin, rotation_deg, flip_x) -> list[tuple[float, float]]:
    return [transform_point(x, y, origin, rotation_deg, flip_x) for x, y in local]


def circle_points(n: int = 32) -> list[tuple[float, float]]:
    return [
        (math.cos(2 * math.pi * i / n), math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


def rect_local(sx: float, sy: float) -> list[tuple[float, float]]:
    hx, hy = sx / 2, sy / 2
    return [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]


def roundrect_local(sx: float, sy: float, rratio: float, corner_steps: int = 4) -> list[tuple[float, float]]:
    # KiCad's ratio tops out at 0.5, where the radius equals half the shorter side.
    radius = max(0.0, min(rratio, 0.5)) * min(sx, sy)
    hx, hy = sx / 2, sy / 2
    radius = min(radius, hx, hy)
    if radius <= 1e-9:
        return rect_local(sx, sy)
    pts: list[tuple[float, float]] = []
    corners = [
        (hx - radius, hy - radius, 0.0),
        (-hx + radius, hy - radius, math.pi / 2),
        (-hx + radius, -hy + radius, math.pi),
        (hx - radius, -hy + radius, 3 * math.pi / 2),
    ]
    for cx, cy, a0 in corners:
        for i in range(corner_steps + 1):
            a = a0 + (math.pi / 2) * i / corner_steps
            pts.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    return pts


def oval_local(sx: float, sy: float, steps: int = 8) -> list[tuple[float, float]]:
    if sx < sy:
        pts = oval_local(sy, sx, steps)
        return [(y, x) for x, y in pts]
    radius = sy / 2
    hx = sx / 2 - radius
    pts: list[tuple[float, float]] = []
    for i in range(steps + 1):
        a = -math.pi / 2 + math.pi * i / steps
        pts.append((hx + radius * math.cos(a), radius * math.sin(a)))
    for i in range(steps + 1):
        a = math.pi / 2 + math.pi * i / steps
        pts.append((-hx + radius * math.cos(a), radius * math.sin(a)))
    return pts


def pad_polygon(
    shape: str,
    size: tuple[float, float],
    center: tuple[float, float],
    rotation_deg: float,
    flip_x: bool,
    rratio: float = 0.25,
) -> list[tuple[float, float]]:
    sx, sy = size
    shape = shape.lower()
    if shape == "circle":
        r = max(sx, sy) / 2
        local = [(r * x, r * y) for x, y in circle_points()]
    elif shape == "oval":
        local = oval_local(sx, sy)
    elif shape == "roundrect":
        local = roundrect_local(sx, sy, rratio)
    elif shape == "rect":
        local = rect_local(sx, sy)
    else:
        raise ValueError(shape)
    return _ring(local, center, rotation_deg, flip_x)


def circle_world(center: tuple[float, float], radius: float, n: int = 32) -> list[tuple[float, float]]:
    return [(center[0] + radius * x, center[1] + radius * y) for x, y in circle_points(n)]


def _circle_from_three(a, b, c):
    (x1, y1), (x2, y2), (x3, y3) = a, b, c
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return None
    ux = (
        (x1 * x1 + y1 * y1) * (y2 - y3)
        + (x2 * x2 + y2 * y2) * (y3 - y1)
        + (x3 * x3 + y3 * y3) * (y1 - y2)
    ) / d
    uy = (
        (x1 * x1 + y1 * y1) * (x3 - x2)
        + (x2 * x2 + y2 * y2) * (x1 - x3)
        + (x3 * x3 + y3 * y3) * (x2 - x1)
    ) / d
    return (ux, uy)


def _ang(center, p) -> float:
    return math.atan2(p[1] - center[1], p[0] - center[0])


def _sweep_ccw(a0: float, a1: float) -> float:
    return (a1 - a0) % (2 * math.pi)


def arc_points_three(start, mid, end, segments: int = 12) -> list[tuple[float, float]]:
    center = _circle_from_three(start, mid, end)
    if center is None:
        return [start, mid, end]
    a0 = _ang(center, start)
    am = _ang(center, mid)
    a1 = _ang(center, end)
    ccw = _sweep_ccw(a0, a1)
    # Use the direction that passes through the midpoint.
    if _sweep_ccw(a0, am) <= ccw + 1e-9:
        sweep = ccw
    else:
        sweep = ccw - 2 * math.pi
    radius = math.hypot(start[0] - center[0], start[1] - center[1])
    n = max(segments, 2)
    pts = []
    for i in range(n + 1):
        a = a0 + sweep * i / n
        pts.append((center[0] + radius * math.cos(a), center[1] + radius * math.sin(a)))
    return pts


def arc_points_center(center, start, angle_deg: float, segments: int = 16) -> list[tuple[float, float]]:
    """Legacy KiCad arc: center, start point, sweep in degrees, positive CCW."""
    radius = math.hypot(start[0] - center[0], start[1] - center[1])
    if radius < 1e-12:
        return [start]
    a0 = _ang(center, start)
    sweep = math.radians(angle_deg)
    n = max(int(segments * abs(angle_deg) / 90), 2)
    pts = []
    for i in range(n + 1):
        a = a0 + sweep * i / n
        pts.append((center[0] + radius * math.cos(a), center[1] + radius * math.sin(a)))
    return pts


def arc_points_ends(
    start: tuple[float, float],
    end: tuple[float, float],
    center: tuple[float, float],
    clockwise: bool,
    segments: int = 16,
) -> list[tuple[float, float]]:
    """Arc from start to end about center. Y positive is up; clockwise follows that frame."""
    sx, sy = start
    ex, ey = end
    cx, cy = center
    radius = math.hypot(sx - cx, sy - cy)
    if radius < 1e-12:
        return [start, end]
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    if clockwise:
        sweep = -((a0 - a1) % (2 * math.pi))
        if abs(sweep) < 1e-9:
            sweep = -2 * math.pi
    else:
        sweep = (a1 - a0) % (2 * math.pi)
        if sweep < 1e-9:
            sweep = 2 * math.pi
    n = max(int(segments * abs(sweep) / (math.pi / 2)), 2)
    pts = []
    for i in range(n + 1):
        a = a0 + sweep * i / n
        pts.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    return pts


def buffer_centerline(
    points: list[tuple[float, float]],
    width: float,
    cap_style: str = "round",
) -> list[tuple[float, float]] | None:
    if width <= 0 or len(points) < 1:
        return None
    degenerate = len(points) == 1 or all(
        math.hypot(points[0][0] - p[0], points[0][1] - p[1]) < 1e-9 for p in points
    )
    if degenerate:
        x, y = points[0]
        if cap_style == "square":
            h = width / 2
            poly = Polygon([(x - h, y - h), (x + h, y - h), (x + h, y + h), (x - h, y + h)])
        else:
            poly = Point(points[0]).buffer(width / 2, quad_segs=8)
    else:
        join = "mitre" if cap_style == "square" else "round"
        poly = LineString(points).buffer(width / 2, cap_style=cap_style, join_style=join, quad_segs=4)
    if poly.is_empty:
        return None
    return [(float(x), float(y)) for x, y in list(poly.exterior.coords)[:-1]]


def shoelace(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    acc = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        acc += x1 * y2 - x2 * y1
    return acc / 2


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def chain_loops(polylines: list[list[tuple[float, float]]], tol: float = 1e-3) -> list[list[tuple[float, float]]]:
    """Join open polylines that share endpoints. Closed rings are returned without the repeated point."""
    loops: list[list[tuple[float, float]]] = []
    open_paths: list[list[tuple[float, float]]] = []
    for line in polylines:
        if len(line) < 2:
            continue
        if _dist(line[0], line[-1]) <= tol and len(line) >= 4:
            loops.append(list(line[:-1]))
        else:
            open_paths.append(list(line))

    used = [False] * len(open_paths)

    def take(end, at_start: bool):
        for j, other in enumerate(open_paths):
            if used[j]:
                continue
            if _dist(end, other[0]) <= tol:
                used[j] = True
                return list(other[1:]), False
            if _dist(end, other[-1]) <= tol:
                used[j] = True
                return list(reversed(other[:-1])), False
        return None

    for i, path in enumerate(open_paths):
        if used[i]:
            continue
        used[i] = True
        chain = list(path)
        grew = True
        while grew and _dist(chain[0], chain[-1]) > tol:
            grew = False
            nxt = take(chain[-1], False)
            if nxt is not None:
                chain.extend(nxt[0])
                grew = True
                continue
            prev = take(chain[0], True)
            if prev is not None:
                chain = list(reversed(prev[0])) + chain
                grew = True
        if len(chain) >= 4 and _dist(chain[0], chain[-1]) <= tol:
            loops.append(chain[:-1])
    return loops


def as_polygon(points: list[tuple[float, float]], holes: list[list[tuple[float, float]]] | None = None) -> Polygon | None:
    if len(points) < 3:
        return None
    try:
        poly = Polygon(points, holes or None)
    except Exception:
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return None
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    if poly.geom_type != "Polygon" or poly.area < 1e-12:
        return None
    return poly


def polygon_parts(geom: BaseGeometry) -> list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]]:
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        geoms = [geom]
    elif geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        out: list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]] = []
        for part in geom.geoms:
            out.extend(polygon_parts(part))
        return out
    else:
        return []
    result = []
    for poly in geoms:
        if poly.area < 1e-10:
            continue
        ext = [(float(x), float(y)) for x, y in list(poly.exterior.coords)[:-1]]
        holes = [[(float(x), float(y)) for x, y in list(ring.coords)[:-1]] for ring in poly.interiors]
        if len(ext) >= 3:
            result.append((ext, holes))
    return result


def solid_triangles(points: list[tuple[float, float]], holes: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Break a polygon with holes into triangles CSXCAD can store as plain polygons."""
    if not holes:
        return [points]
    poly = as_polygon(points, holes)
    if poly is None:
        return [points]
    tris = []
    for tri in triangulate(poly):
        if poly.contains(tri.representative_point()) and tri.area > 1e-12:
            tris.append([(float(x), float(y)) for x, y in list(tri.exterior.coords)[:-1]])
    return tris or [points]
