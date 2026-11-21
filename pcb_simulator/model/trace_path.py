"""Approximate length along a net, and a point at a distance from a port.

Zones are left out. A pour is not a trace, and its outline would measure the
board instead of the route. The path is the copper centerline. From the port
it follows the longest route on the net, so a second pad on that net does not
hold the marker in a loop. The distance is geometric, so it matches the TDR
millimeter axis only while the wave is still going out and εeff is close to
the real velocity.
"""

from __future__ import annotations

import math

from .board import Board
from .geom import as_polygon

_JOIN_MM = 0.25
_SNAP_MM = 5.0

# id(board), net -> graph or None. Cleared when another board is opened.
_cache: dict[tuple[int, str], dict | None] = {}


def clear_trace_cache() -> None:
    _cache.clear()


def trace_point(
    board: Board,
    net: str,
    origin: tuple[float, float],
    distance_mm: float,
) -> tuple[tuple[float, float], bool] | None:
    """Return (point, clamped). Clamped means the trace ended before `distance_mm`."""
    graph = _graph(board, net)
    if graph is None:
        return None
    located = _locate(graph, origin)
    if located is None:
        return None
    if distance_mm <= 1e-6:
        return located["point"], False
    ends = _ends_for(graph, located["i"])
    goal = _far_end(ends, located)
    return _follow(graph, ends, located, goal, distance_mm)


def route_length(board: Board, net: str, origin: tuple[float, float]) -> float | None:
    """Millimetres from ``origin`` to the far end of the longest route on ``net``.

    Zones are left out, same as the TDR marker. None when the net has no centerline
    under the origin.
    """
    graph = _graph(board, net)
    if graph is None:
        return None
    located = _locate(graph, origin)
    if located is None:
        return None
    ends = _ends_for(graph, located["i"])
    goal = _far_end(ends, located)
    length = _distance_to(ends, goal, located)
    if length <= 1e-6:
        return None
    return float(length)


def trace_route(
    board: Board,
    net: str,
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[float, list[tuple[float, float]]] | None:
    """Shortest centerline between two points on one net, and the polyline."""
    graph = _graph(board, net)
    if graph is None:
        return None
    a = _locate(graph, start)
    b = _locate(graph, end)
    if a is None or b is None:
        return None
    return _route(graph, a, b)


def _graph(board: Board, net: str) -> dict | None:
    if not net:
        return None
    key = (id(board), net)
    if key not in _cache:
        _cache[key] = _build(board, net)
    return _cache[key]


def _build(board: Board, net: str) -> dict | None:
    nodes: list[tuple[float, float]] = []
    link: dict[int, dict[int, float]] = {}
    pieces: list[tuple[list[int], object]] = []

    def add_node(xy: tuple[float, float]) -> int:
        for index, point in enumerate(nodes):
            if math.dist(point, xy) <= 0.05:
                return index
        nodes.append((float(xy[0]), float(xy[1])))
        link[len(nodes) - 1] = {}
        return len(nodes) - 1

    def add_edge(i: int, j: int, length: float) -> None:
        if i == j:
            return
        length = max(float(length), 0.0)
        for src, dst in ((i, j), (j, i)):
            have = link[src].get(dst)
            if have is None or length < have:
                link[src][dst] = length

    def add_piece(geom, *coords: tuple[float, float]) -> None:
        indexes = [add_node(xy) for xy in coords]
        if len(indexes) == 2:
            add_edge(indexes[0], indexes[1], math.dist(coords[0], coords[1]))
        pieces.append((indexes, geom))

    for poly in board.copper:
        if poly.net != net or poly.kind == "zone":
            continue
        geom = as_polygon(poly.points)
        line = _centerline(poly.points)
        if line is None:
            continue
        a, b = line
        if math.dist(a, b) < 0.05:
            add_piece(geom, a)
        else:
            add_piece(geom, a, b)
    for pad in board.pads:
        if pad.net != net:
            continue
        add_piece(as_polygon(pad.points), (float(pad.center[0]), float(pad.center[1])))
    for via in board.vias:
        if via.net != net:
            continue
        add_piece(None, (float(via.x), float(via.y)))

    for left in range(len(pieces)):
        nodes_a, geom_a = pieces[left]
        for right in range(left + 1, len(pieces)):
            nodes_b, geom_b = pieces[right]
            if not _pieces_meet(geom_a, geom_b, nodes, nodes_a, nodes_b):
                continue
            pair = _closest_nodes(nodes, nodes_a, nodes_b)
            if pair is None:
                continue
            i, j, gap = pair
            add_edge(i, j, gap)
    if not nodes:
        return None
    return {"nodes": nodes, "adj": link}


def _pieces_meet(geom_a, geom_b, nodes, nodes_a, nodes_b) -> bool:
    if geom_a is not None and geom_b is not None:
        try:
            if geom_a.distance(geom_b) <= _JOIN_MM:
                return True
        except Exception:
            pass
    pair = _closest_nodes(nodes, nodes_a, nodes_b)
    return pair is not None and pair[2] <= _JOIN_MM


def _closest_nodes(nodes, group_a, group_b) -> tuple[int, int, float] | None:
    best = None
    for i in group_a:
        for j in group_b:
            if i == j:
                return i, j, 0.0
            gap = math.dist(nodes[i], nodes[j])
            if best is None or gap < best[2]:
                best = (i, j, gap)
    return best


def _centerline(points: list[tuple[float, float]]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    cleaned = [(float(x), float(y)) for x, y in points]
    if len(cleaned) >= 2 and math.dist(cleaned[0], cleaned[-1]) < 1e-9:
        cleaned = cleaned[:-1]
    if not cleaned:
        return None
    count = len(cleaned)
    mx = sum(x for x, _y in cleaned) / count
    my = sum(y for _x, y in cleaned) / count
    centered = [(x - mx, y - my) for x, y in cleaned]
    sxx = sum(x * x for x, _y in centered)
    syy = sum(y * y for _x, y in centered)
    sxy = sum(x * y for x, y in centered)
    theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
    ux, uy = math.cos(theta), math.sin(theta)
    projs = [x * ux + y * uy for x, y in centered]
    perps = [-x * uy + y * ux for x, y in centered]
    span = max(projs) - min(projs)
    width = max(perps) - min(perps)
    if span < max(0.05, width * 1.5):
        return ((mx, my), (mx, my))
    p0, p1 = min(projs), max(projs)
    # The centerline goes through the centroid. `perps` are already relative to it.
    return (mx + ux * p0, my + uy * p0), (mx + ux * p1, my + uy * p1)


def _locate(graph: dict, xy: tuple[float, float]) -> dict | None:
    nodes = graph["nodes"]
    best = None
    for index, point in enumerate(nodes):
        off = math.dist(point, xy)
        if best is None or off < best[0]:
            best = (off, index, index, point, {index: 0.0})
    seen = set()
    for i, nbrs in graph["adj"].items():
        ax, ay = nodes[i]
        for j, length in nbrs.items():
            key = (min(i, j), max(i, j))
            if key in seen:
                continue
            seen.add(key)
            bx, by = nodes[j]
            point, dist_i, off = _project(xy, (ax, ay), (bx, by), length)
            if best is None or off < best[0]:
                best = (off, i, j, point, {i: dist_i, j: max(length - dist_i, 0.0)})
    if best is None or best[0] > _SNAP_MM:
        return None
    _off, i, j, point, along = best
    return {"i": i, "j": j, "point": point, "along": along}


def _project(xy, a, b, length: float) -> tuple[tuple[float, float], float, float]:
    if length <= 1e-12:
        return a, 0.0, math.dist(xy, a)
    vx, vy = b[0] - a[0], b[1] - a[1]
    frac = ((xy[0] - a[0]) * vx + (xy[1] - a[1]) * vy) / (length * length)
    frac = min(max(frac, 0.0), 1.0)
    point = (a[0] + vx * frac, a[1] + vy * frac)
    return point, frac * length, math.dist(xy, point)


def _ends_for(graph: dict, seed: int) -> dict:
    """One pair of endpoints for the connected copper that contains `seed`.

    A second pad, or a loop of pads, can reach the trace either way. The
    straightest step then stays in that loop. The two farthest nodes are the
    ends of the route, and the walk below follows the short path between them.
    """
    found = graph.setdefault("ends", [])
    for ends in found:
        if seed in ends["dist"][ends["a"]]:
            return ends
    adj = graph["adj"]
    dist0, _prev0 = _dijkstra(adj, seed)
    end_a = max(dist0, key=dist0.get)
    dist_a, prev_a = _dijkstra(adj, end_a)
    end_b = max(dist_a, key=dist_a.get)
    # prev[node] is one step back toward the source, so it points at that end.
    dist_b, prev_b = _dijkstra(adj, end_b)
    ends = {
        "a": end_a,
        "b": end_b,
        "dist": {end_a: dist_a, end_b: dist_b},
        "prev": {end_a: prev_a, end_b: prev_b},
    }
    found.append(ends)
    return ends


def _far_end(ends: dict, located: dict) -> int:
    reach_a = _distance_to(ends, ends["a"], located)
    reach_b = _distance_to(ends, ends["b"], located)
    if reach_a >= reach_b:
        return ends["a"]
    return ends["b"]


def _distance_to(ends: dict, end: int, located: dict) -> float:
    dist_map = ends["dist"][end]
    best = None
    for node, along in located["along"].items():
        if node not in dist_map:
            continue
        total = along + dist_map[node]
        if best is None or total < best:
            best = total
    if best is None:
        return 0.0
    return best


def _follow(graph: dict, ends: dict, located: dict, goal: int, distance_mm: float):
    nodes = graph["nodes"]
    dist_map = ends["dist"][goal]
    prev = ends["prev"][goal]
    ranked = []
    for node, along in located["along"].items():
        if node not in dist_map:
            continue
        ranked.append((dist_map[node], along, node))
    if not ranked:
        return located["point"], distance_mm > 1e-6
    _dist, first, node = min(ranked)
    if distance_mm <= first + 1e-9:
        frac = 0.0 if first <= 1e-12 else distance_mm / first
        return _lerp(located["point"], nodes[node], frac), False
    remaining = distance_mm - first
    seen = set()
    while len(seen) <= len(nodes):
        if node == goal or node in seen:
            return nodes[node], True
        seen.add(node)
        nxt = prev.get(node)
        if nxt is None or nxt not in graph["adj"][node]:
            return nodes[node], True
        length = graph["adj"][node][nxt]
        if remaining <= length + 1e-9:
            frac = 0.0 if length <= 1e-12 else remaining / length
            return _lerp(nodes[node], nodes[nxt], frac), False
        remaining -= length
        node = nxt
    return nodes[node], True


def _route(graph: dict, start: dict, end: dict) -> tuple[float, list[tuple[float, float]]] | None:
    if math.dist(start["point"], end["point"]) <= 1e-6 and start["i"] == end["i"] and start["j"] == end["j"]:
        return 0.0, [start["point"]]
    nodes = graph["nodes"]
    begin = len(nodes)
    finish = begin + 1
    adj = {key: dict(value) for key, value in graph["adj"].items()}
    adj[begin] = {}
    adj[finish] = {}
    for index, length in start["along"].items():
        adj[begin][index] = length
        adj[index][begin] = length
    for index, length in end["along"].items():
        adj[finish][index] = length
        adj[index][finish] = length
    if start["i"] != start["j"] and {start["i"], start["j"]} == {end["i"], end["j"]}:
        gap = abs(start["along"][start["i"]] - end["along"][start["i"]])
        adj[begin][finish] = gap
        adj[finish][begin] = gap
    dist, prev = _dijkstra(adj, begin)
    if finish not in dist:
        return None
    path = [finish]
    node = finish
    while node != begin:
        node = prev[node]
        path.append(node)
    path.reverse()
    points = [start["point"]]
    for index in path[1:-1]:
        points.append(nodes[index])
    points.append(end["point"])
    return dist[finish], _simplify(points)


def _dijkstra(adj: dict[int, dict[int, float]], start: int) -> tuple[dict[int, float], dict[int, int]]:
    dist = {start: 0.0}
    prev: dict[int, int] = {}
    heap = [(0.0, start)]
    while heap:
        so_far, node = heap.pop(0)
        if so_far > dist.get(node, 1e300) + 1e-9:
            continue
        for nxt, length in adj[node].items():
            step = so_far + length
            if step + 1e-9 < dist.get(nxt, 1e300):
                dist[nxt] = step
                prev[nxt] = node
                heap.append((step, nxt))
        heap.sort()
    return dist, prev


def _simplify(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    kept = [points[0]]
    for point in points[1:]:
        if math.dist(kept[-1], point) > 1e-6:
            kept.append(point)
    return kept


def _lerp(a, b, frac: float) -> tuple[float, float]:
    frac = min(max(frac, 0.0), 1.0)
    return (a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac)


