"""Copper grid, DC IR drop, and plane-pair impedance.

Sheet resistance uses the stored copper thickness and the conductivity.
A via uses the plating thickness. The openEMS mesh is not involved.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import csc_matrix
from scipy.sparse.linalg import splu

from ..model.board import Board
from ..model.crop import CropSettings, crop_board
from ..model.geom import as_polygon

MU0 = 1.25663706212e-6
EPS0 = 8.854187817e-12
NODE_LIMIT = 200_000
SHORT_OHM = 1e-6


@dataclass
class PdnTerminal:
    """A VRM or a load, already located on a pad."""

    name: str
    x: float
    y: float
    net: str
    layer: str = ""
    volts: float = 1.0
    amps: float = 0.1
    r_ohm: float = 0.0
    l_nh: float = 0.0
    gx: float | None = None
    gy: float | None = None
    g_layer: str = ""


@dataclass
class PdnPart:
    """A series RLC between two pads. A missing element is absent. A decap is open at DC."""

    name: str
    ax: float
    ay: float
    net_a: str
    bx: float
    by: float
    net_b: str
    r_ohm: float | None = None
    l_nh: float | None = None
    c_nf: float | None = None
    layer_a: str = ""
    layer_b: str = ""


@dataclass
class Node:
    layer: str
    ix: int
    iy: int
    net: str
    x: float
    y: float


@dataclass
class Branch:
    a: int
    b: int
    r_ohm: float
    l_h: float = 0.0
    thickness_m: float = 0.0
    kind: str = "sheet"  # sheet, via, part, vrm


@dataclass
class Shunt:
    power: int
    ground: int
    c_f: float


@dataclass
class Grid:
    pitch_mm: float
    origin_x: float
    origin_y: float
    nodes: list[Node]
    branches: list[Branch]
    shunts: list[Shunt]
    conductivity: float
    power_layer: str
    ground_layer: str
    lookup: dict[tuple[str, int, int, str], int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PdnResult:
    ok: bool
    message: str
    drops: list[dict]
    voltages: list[tuple[float, float, float, float]]
    density: list[tuple[float, float, float, float]]
    freq_hz: np.ndarray
    impedance: dict[str, np.ndarray]
    impedance_bare: dict[str, np.ndarray]
    z_target: float | None
    warnings: list[str]
    # Filled after the openEMS run. The lumped sweep stays in impedance for the circuit tests.
    field_freq_hz: np.ndarray | None = None
    field_impedance: dict[str, np.ndarray] | None = None
    field_decayed: bool = True


def via_resistance_ohm(length_mm: float, drill_mm: float, plating_mm: float, conductivity: float) -> float | None:
    """Barrel resistance. Area is π * plating * (drill − plating)."""
    if length_mm <= 0 or plating_mm <= 0 or drill_mm <= plating_mm or conductivity <= 0:
        return None
    area_mm2 = math.pi * plating_mm * (drill_mm - plating_mm)
    length_m = length_mm * 1e-3
    area_m2 = area_mm2 * 1e-6
    return (1.0 / conductivity) * length_m / area_m2


def sheet_resistance_ohm(thickness_mm: float, conductivity: float) -> float | None:
    """Resistance of one square. R = 1 / (σ t)."""
    if thickness_mm <= 0 or conductivity <= 0:
        return None
    return 1.0 / (conductivity * thickness_mm * 1e-3)


def _skin_factor(thickness_m: float, conductivity: float, freq_hz: float) -> float:
    if freq_hz <= 0 or thickness_m <= 0 or conductivity <= 0:
        return 1.0
    delta = 1.0 / math.sqrt(math.pi * freq_hz * MU0 * conductivity)
    if delta <= 0:
        return 1.0
    return max(1.0, thickness_m / delta)


def build_grid(
    board: Board,
    crop: CropSettings,
    power_net: str,
    pitch_mm: float,
    conductivity: float,
    plating_mm: float,
    power_layer: str = "",
    ground_layer: str = "",
) -> Grid:
    """Rasterize the power net and the reference net inside the crop."""
    pitch = max(0.2, float(pitch_mm))
    cropped = crop_board(
        board,
        CropSettings(
            mode=crop.mode,
            rect=crop.rect,
            margin_mm=crop.margin_mm,
            selected_nets=[power_net] if power_net else [],
            reference_net=crop.reference_net,
        ),
    )
    warnings = list(cropped.warnings)
    reference = crop.reference_net
    polys: dict[tuple[str, str], list] = {}
    for poly in cropped.copper:
        if poly.net not in {power_net, reference}:
            continue
        geom = as_polygon(poly.points, poly.holes)
        if geom is None or geom.is_empty:
            continue
        polys.setdefault((poly.layer, poly.net), []).append(geom)
    nodes: list[Node] = []
    lookup: dict[tuple[str, int, int, str], int] = {}
    xs: list[float] = []
    ys: list[float] = []
    for geoms in polys.values():
        for geom in geoms:
            minx, miny, maxx, maxy = geom.bounds
            xs.extend((minx, maxx))
            ys.extend((miny, maxy))
    if not xs:
        grid = Grid(pitch, 0.0, 0.0, [], [], [], conductivity, power_layer, ground_layer, {}, warnings)
        warnings.append("The power and reference nets have no copper in the region.")
        grid.warnings = warnings
        return grid
    origin_x = math.floor(min(xs) / pitch) * pitch
    origin_y = math.floor(min(ys) / pitch) * pitch
    from shapely import unary_union
    from shapely.geometry import box
    from shapely.prepared import prep

    for (layer, net), geoms in polys.items():
        thickness = _copper_thickness(board, layer)
        if thickness <= 0:
            warnings.append(f"{layer} has no copper thickness, so it is left out of the PDN.")
            continue
        merged = unary_union(geoms)
        if merged.is_empty:
            continue
        prepared = prep(merged)
        minx, miny, maxx, maxy = merged.bounds
        ix0 = int(math.floor((minx - origin_x) / pitch))
        iy0 = int(math.floor((miny - origin_y) / pitch))
        ix1 = int(math.floor((maxx - origin_x) / pitch))
        iy1 = int(math.floor((maxy - origin_y) / pitch))
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                # Inset so a polygon that only shares the grid line is not metal.
                # A real overlap, including a trace thinner than the pitch, still hits.
                inset = 1e-6
                cell = box(
                    origin_x + ix * pitch + inset,
                    origin_y + iy * pitch + inset,
                    origin_x + (ix + 1) * pitch - inset,
                    origin_y + (iy + 1) * pitch - inset,
                )
                if not prepared.intersects(cell):
                    continue
                key = (layer, ix, iy, net)
                if key in lookup:
                    continue
                lookup[key] = len(nodes)
                nodes.append(
                    Node(
                        layer,
                        ix,
                        iy,
                        net,
                        origin_x + (ix + 0.5) * pitch,
                        origin_y + (iy + 0.5) * pitch,
                    )
                )
    if len(nodes) > NODE_LIMIT:
        warnings.append(f"The PDN grid has {len(nodes):,} nodes. Raise the pitch or crop the board.")
    branches: list[Branch] = []
    for (layer, _ix, _iy, net), index in lookup.items():
        node = nodes[index]
        for dix, diy in ((1, 0), (0, 1)):
            other = lookup.get((layer, node.ix + dix, node.iy + diy, net))
            if other is None:
                continue
            thickness = _copper_thickness(board, layer)
            resistance = sheet_resistance_ohm(thickness, conductivity)
            if resistance is None:
                continue
            branches.append(Branch(index, other, resistance, thickness_m=thickness * 1e-3, kind="sheet"))
    _add_vias(
        board, cropped.vias, power_net, reference, plating_mm, conductivity, pitch, lookup, nodes, branches, warnings
    )
    power_name, ground_name = _choose_layers(board, nodes, power_net, reference, power_layer, ground_layer, warnings)
    gap = _gap(board, power_name, ground_name)
    shunts: list[Shunt] = []
    if gap is not None and power_name and ground_name and power_name != ground_name:
        h_mm, eps_r = gap
        if h_mm > 0:
            area = (pitch * 1e-3) ** 2
            c_cell = EPS0 * eps_r * area / (h_mm * 1e-3)
            inductance = MU0 * (h_mm * 1e-3)
            for key, index in lookup.items():
                layer, ix, iy, net = key
                if layer != power_name or net != power_net:
                    continue
                ground = lookup.get((ground_name, ix, iy, reference))
                if ground is None:
                    continue
                shunts.append(Shunt(index, ground, c_cell))
            for branch in branches:
                if branch.kind != "sheet":
                    continue
                if nodes[branch.a].net != power_net or nodes[branch.a].layer != power_name:
                    continue
                if nodes[branch.b].layer != power_name:
                    continue
                both = lookup.get((ground_name, nodes[branch.a].ix, nodes[branch.a].iy, reference))
                other = lookup.get((ground_name, nodes[branch.b].ix, nodes[branch.b].iy, reference))
                if both is not None and other is not None:
                    branch.l_h = inductance
    else:
        if power_name and ground_name == power_name:
            warnings.append("AC impedance uses one power layer and one ground layer with a dielectric between them.")
    return Grid(pitch, origin_x, origin_y, nodes, branches, shunts, conductivity, power_name, ground_name, lookup, warnings)


def solve_pdn(
    grid: Grid,
    power_net: str,
    reference_net: str,
    vrm: PdnTerminal | None,
    loads: list[PdnTerminal],
    parts: list[PdnPart],
    freq_hz: np.ndarray | None = None,
    ripple_mv: float | None = None,
    step_a: float | None = None,
) -> PdnResult:
    """DC drop, then |Z(f)| with the VRM shorted through its series R and L."""
    empty = np.array([], dtype=float)
    warnings = list(grid.warnings)
    if vrm is None:
        return _fail("Place a VRM on the power net.", warnings)
    if not loads:
        return _fail("Place at least one load.", warnings)
    if len(grid.nodes) > NODE_LIMIT:
        return _fail(warnings[-1] if warnings else "The PDN grid is too fine.", warnings)
    if not grid.nodes:
        return _fail("The power and reference nets have no copper in the region.", warnings)
    vrm_power = nearest_node(grid, vrm.x, vrm.y, power_net, vrm.layer) or nearest_node(grid, vrm.x, vrm.y, power_net)
    vrm_ground = _ground_node(grid, vrm, reference_net)
    if vrm_power is None:
        return _fail("The VRM power click is not on the power net.", warnings)
    if vrm_ground is None:
        return _fail("The VRM ground click is not on the reference net.", warnings)
    placed = _placed_parts(grid, parts, warnings)
    connected, open_loads = _reachable(grid, vrm_power, vrm_ground, loads, power_net, reference_net, placed)
    for load in open_loads:
        warnings.append(f"{load.name} is not connected to the VRM.")
    if not connected:
        return _fail("No load has a copper path to the VRM.", warnings)
    volts, density = _solve_dc(grid, vrm, vrm_power, vrm_ground, connected, power_net, reference_net, placed)
    if volts is None:
        return _fail("The DC solve did not settle. Check that the VRM and the loads share copper.", warnings)
    drops = []
    for load, power_id, ground_id in connected:
        power_v = volts[power_id]
        ground_v = volts[ground_id]
        loop = vrm.volts - (power_v - ground_v)
        drops.append(
            {
                "name": load.name,
                "loop_mv": loop * 1e3,
                "power_mv": (vrm.volts - power_v) * 1e3,
                "ground_mv": ground_v * 1e3,
                "connected": True,
            }
        )
    for load in open_loads:
        drops.append({"name": load.name, "loop_mv": None, "power_mv": None, "ground_mv": None, "connected": False})
    frequencies = np.geomspace(1e3, 1e9, 50) if freq_hz is None else np.asarray(freq_hz, dtype=float)
    impedance = _solve_ac(grid, vrm, vrm_power, vrm_ground, connected, placed, frequencies, with_parts=True, with_vrm=True)
    impedance_bare = _solve_ac(
        grid, vrm, vrm_power, vrm_ground, connected, placed, frequencies, with_parts=False, with_vrm=True
    )
    target = None
    if ripple_mv is not None and step_a not in (None, 0):
        target = (float(ripple_mv) * 1e-3) / float(step_a)
    pitch = grid.pitch_mm
    voltage_cells = [
        (grid.nodes[index].x - pitch / 2, grid.nodes[index].y - pitch / 2, pitch, float(value))
        for index, value in enumerate(volts)
        if grid.nodes[index].net == power_net and np.isfinite(value)
    ]
    message = "PDN finished."
    if open_loads:
        message = "PDN finished. " + " ".join(f"{load.name} is open." for load in open_loads)
    return PdnResult(True, message, drops, voltage_cells, density, frequencies, impedance, impedance_bare, target, warnings)


def _ground_node(grid: Grid, term: PdnTerminal, reference_net: str) -> int | None:
    """Return node at the ground click. An older terminal has no ground click, so it uses the power point."""
    gx = term.x if term.gx is None else term.gx
    gy = term.y if term.gy is None else term.gy
    if term.g_layer:
        found = nearest_node(grid, gx, gy, reference_net, term.g_layer)
        if found is not None:
            return found
    return nearest_node(grid, gx, gy, reference_net)


def nearest_node(grid: Grid, x: float, y: float, net: str, layer: str = "") -> int | None:
    """Metal cell under the point, or the nearest cell of that net within two pitches."""
    if not grid.nodes or grid.pitch_mm <= 0:
        return None
    ix = int(math.floor((x - grid.origin_x) / grid.pitch_mm))
    iy = int(math.floor((y - grid.origin_y) / grid.pitch_mm))
    best = None
    best_d = 1e9
    for radius in range(0, 3):
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if max(abs(dx), abs(dy)) != radius and radius != 0:
                    continue
                for key, index in _keys_at(grid, ix + dx, iy + dy, net, layer):
                    node = grid.nodes[index]
                    distance = (node.x - x) ** 2 + (node.y - y) ** 2
                    if distance < best_d:
                        best_d = distance
                        best = index
        if best is not None:
            return best
    return None


def _keys_at(grid: Grid, ix: int, iy: int, net: str, layer: str):
    if layer:
        found = grid.lookup.get((layer, ix, iy, net))
        return [] if found is None else [((layer, ix, iy, net), found)]
    return [item for item in grid.lookup.items() if item[0][1] == ix and item[0][2] == iy and item[0][3] == net]


def _copper_thickness(board: Board, layer: str) -> float:
    for item in board.stackup:
        if item.kind == "copper" and item.name == layer:
            return float(item.thickness_mm)
    return 0.0


def _add_vias(board, vias, power_net, reference, plating_mm, conductivity, pitch, lookup, nodes, branches, warnings) -> None:
    names = board.copper_names_top_to_bottom()
    rank = {name: index for index, name in enumerate(names)}
    for via in vias:
        if via.net not in {power_net, reference}:
            continue
        if len(via.layers) < 2:
            continue
        start, stop = via.layers[0], via.layers[1]
        if start not in rank or stop not in rank:
            continue
        lo, hi = sorted((rank[start], rank[stop]))
        span = [name for name in names if lo <= rank[name] <= hi]
        hits = []
        half = pitch / 2 + 1e-9
        for layer in span:
            found = _cell_containing(nodes, lookup, layer, via.net, via.x, via.y, half, pitch)
            if found is not None:
                hits.append((board.layer_z(layer) or 0.0, found))
        hits.sort()
        for (z0, a), (z1, b) in zip(hits, hits[1:]):
            resistance = via_resistance_ohm(abs(z1 - z0), via.drill_mm, plating_mm, conductivity)
            if resistance is None:
                warnings.append(f"A {via.net} via at {via.x:.2f}, {via.y:.2f} has no plating resistance.")
                continue
            thickness = plating_mm * 1e-3
            branches.append(Branch(a, b, resistance, thickness_m=thickness, kind="via"))


def _cell_containing(nodes, lookup, layer, net, x, y, half, pitch) -> int | None:
    best = None
    best_d = 1e9
    for key, index in lookup.items():
        if key[0] != layer or key[3] != net:
            continue
        node = nodes[index]
        if abs(node.x - x) <= half and abs(node.y - y) <= half:
            return index
        distance = (node.x - x) ** 2 + (node.y - y) ** 2
        if distance < best_d:
            best_d = distance
            best = index
    if best is None:
        return None
    node = nodes[best]
    if abs(node.x - x) <= pitch and abs(node.y - y) <= pitch:
        return best
    return None


def _choose_layers(board, nodes, power_net, reference, power_layer, ground_layer, warnings):
    power_counts: dict[str, int] = {}
    ground_layers: set[str] = set()
    for node in nodes:
        if node.net == power_net:
            power_counts[node.layer] = power_counts.get(node.layer, 0) + 1
        elif node.net == reference:
            ground_layers.add(node.layer)
    chosen_power = power_layer if power_layer in power_counts else ""
    if not chosen_power and power_counts:
        chosen_power = max(power_counts, key=power_counts.get)
    chosen_ground = ground_layer if ground_layer in ground_layers else ""
    if not chosen_ground and chosen_power and ground_layers:
        z_power = board.layer_z(chosen_power)
        ranked = []
        for name in ground_layers:
            if name == chosen_power:
                continue
            z_ground = board.layer_z(name)
            distance = abs((z_power or 0.0) - (z_ground or 0.0))
            ranked.append((distance, name))
        if ranked:
            ranked.sort()
            chosen_ground = ranked[0][1]
    if not chosen_ground and ground_layers:
        chosen_ground = sorted(ground_layers)[0]
    return chosen_power, chosen_ground


def _gap(board: Board, power: str, ground: str):
    if not power or not ground or power == ground:
        return None
    z_power = board.layer_z(power)
    z_ground = board.layer_z(ground)
    if z_power is None or z_ground is None:
        return None
    lo, hi = min(z_power, z_ground), max(z_power, z_ground)
    for dielectric in board.dielectrics():
        if dielectric.z_mm is None:
            continue
        if abs(dielectric.z_mm - lo) < 1e-6 and abs(dielectric.z_mm + dielectric.thickness_mm - hi) < 1e-6:
            return dielectric.thickness_mm, float(dielectric.eps_r or 4.5)
    if hi > lo:
        return hi - lo, board.top_eps()
    return None


def _placed_parts(grid, parts, warnings):
    placed = []
    for part in parts:
        a = nearest_node(grid, part.ax, part.ay, part.net_a, part.layer_a) or nearest_node(
            grid, part.ax, part.ay, part.net_a
        )
        b = nearest_node(grid, part.bx, part.by, part.net_b, part.layer_b) or nearest_node(
            grid, part.bx, part.by, part.net_b
        )
        if a is None or b is None or a == b:
            warnings.append(f"{part.name} does not land on copper at both pads.")
            continue
        placed.append((part, a, b))
    return placed


def _dc_edges(grid, placed):
    edges: dict[int, list[tuple[int, float]]] = {}

    def add(a, b, resistance):
        edges.setdefault(a, []).append((b, resistance))
        edges.setdefault(b, []).append((a, resistance))

    for branch in grid.branches:
        add(branch.a, branch.b, branch.r_ohm)
    for part, a, b in placed:
        if part.c_nf:
            continue
        add(a, b, part.r_ohm if part.r_ohm else SHORT_OHM)
    return edges


def _reachable(grid, vrm_power, vrm_ground, loads, power_net, reference_net, placed):
    edges = _dc_edges(grid, placed)
    power_reach = _walk(edges, vrm_power)
    ground_reach = _walk(edges, vrm_ground)
    connected = []
    opened = []
    for load in loads:
        power_id = nearest_node(grid, load.x, load.y, power_net, load.layer) or nearest_node(grid, load.x, load.y, power_net)
        ground_id = _ground_node(grid, load, reference_net)
        if power_id is None or ground_id is None or power_id not in power_reach or ground_id not in ground_reach:
            opened.append(load)
            continue
        connected.append((load, power_id, ground_id))
    return connected, opened


def _ac_nodes(grid: Grid, start: int, placed, with_parts: bool) -> set[int]:
    edges: dict[int, list[tuple[int, float]]] = {}

    def add(a, b):
        edges.setdefault(a, []).append((b, 1.0))
        edges.setdefault(b, []).append((a, 1.0))

    for branch in grid.branches:
        add(branch.a, branch.b)
    for shunt in grid.shunts:
        add(shunt.power, shunt.ground)
    if with_parts:
        for _part, a, b in placed:
            add(a, b)
    return _walk(edges, start)


def _walk(edges, start: int) -> set[int]:
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        for other, _resistance in edges.get(node, []):
            if other not in seen:
                seen.add(other)
                stack.append(other)
    return seen


def _solve_dc(grid, vrm, vrm_power, vrm_ground, connected, power_net, reference_net, placed):
    fixed = {vrm_ground: 0.0, vrm_power: float(vrm.volts)}
    edges = _dc_edges(grid, placed)
    keep = _walk(edges, vrm_ground) | _walk(edges, vrm_power)
    free = [index for index in range(len(grid.nodes)) if index in keep and index not in fixed]
    index_of = {node: row for row, node in enumerate(free)}
    if not free and not connected:
        return None, []
    count = len(free)
    matrix = np.zeros((count, count), dtype=float) if count else None
    rhs = np.zeros(count, dtype=float)
    stamps = []
    for branch in grid.branches:
        stamps.append((branch.a, branch.b, branch.r_ohm))
    for part, a, b in placed:
        if part.c_nf:
            continue
        stamps.append((a, b, part.r_ohm if part.r_ohm else SHORT_OHM))
    data_r, data_c, data_v = [], [], []
    for a, b, resistance in stamps:
        _stamp_conductance(data_r, data_c, data_v, rhs, index_of, fixed, a, b, 1.0 / max(resistance, SHORT_OHM))
    for load, power_id, ground_id in connected:
        _stamp_current(rhs, index_of, power_id, -float(load.amps))
        _stamp_current(rhs, index_of, ground_id, float(load.amps))
    solved = np.full(len(grid.nodes), np.nan, dtype=float)
    for node, value in fixed.items():
        solved[node] = value
    if count:
        matrix = csc_matrix((data_v, (data_r, data_c)), shape=(count, count), dtype=float)
        try:
            values = splu(matrix).solve(rhs)
        except Exception:
            return None, []
        if not np.all(np.isfinite(values)):
            return None, []
        for node, row in index_of.items():
            solved[node] = values[row]
    density = []
    pitch = grid.pitch_mm
    for branch in grid.branches:
        if branch.kind != "sheet" or branch.r_ohm <= 0:
            continue
        va, vb = solved[branch.a], solved[branch.b]
        if not (np.isfinite(va) and np.isfinite(vb)):
            continue
        current = abs(va - vb) / branch.r_ohm
        width = branch.thickness_m * 1e3 * pitch
        if width <= 0:
            continue
        node = grid.nodes[branch.a]
        other = grid.nodes[branch.b]
        density.append(((node.x + other.x) / 2 - pitch / 2, (node.y + other.y) / 2 - pitch / 2, pitch, current / width))
    return solved, density


def _solve_ac(grid, vrm, vrm_power, vrm_ground, connected, placed, frequencies, with_parts: bool, with_vrm: bool = True):
    result = {load.name: np.full(len(frequencies), np.nan, dtype=complex) for load, _p, _g in connected}
    if not connected:
        return result
    short = with_vrm and abs(vrm.r_ohm) < 1e-15 and abs(vrm.l_nh) < 1e-15
    keep = _ac_nodes(grid, vrm_ground, placed, with_parts)
    for column, freq in enumerate(frequencies):
        omega = 2 * math.pi * float(freq)
        fixed = {vrm_ground: 0.0}
        if short:
            fixed[vrm_power] = 0.0
        free = [index for index in keep if index not in fixed]
        index_of = {node: row for row, node in enumerate(free)}
        count = len(free)
        if count == 0:
            continue
        rhs_list = []
        for load, power_id, ground_id in connected:
            rhs = np.zeros(count, dtype=complex)
            _stamp_current(rhs, index_of, power_id, 1.0)
            _stamp_current(rhs, index_of, ground_id, -1.0)
            rhs_list.append(rhs)
        rows, cols, values = [], [], []
        for branch in grid.branches:
            factor = _skin_factor(branch.thickness_m, grid.conductivity, float(freq)) if branch.kind in {"sheet", "via"} else 1.0
            impedance = branch.r_ohm * factor + 1j * omega * branch.l_h
            if abs(impedance) < SHORT_OHM:
                impedance = SHORT_OHM
            _stamp_conductance(rows, cols, values, None, index_of, fixed, branch.a, branch.b, 1.0 / impedance)
        for shunt in grid.shunts:
            _stamp_conductance(rows, cols, values, None, index_of, fixed, shunt.power, shunt.ground, 1j * omega * shunt.c_f)
        if with_vrm and not short:
            impedance = float(vrm.r_ohm) + 1j * omega * float(vrm.l_nh) * 1e-9
            if abs(impedance) < SHORT_OHM:
                impedance = SHORT_OHM
            _stamp_conductance(rows, cols, values, None, index_of, fixed, vrm_power, vrm_ground, 1.0 / impedance)
        if with_parts:
            for part, a, b in placed:
                impedance = _part_impedance(part, omega)
                if impedance is None:
                    continue
                _stamp_conductance(rows, cols, values, None, index_of, fixed, a, b, 1.0 / impedance)
        if not values:
            continue
        matrix = csc_matrix((values, (rows, cols)), shape=(count, count), dtype=complex)
        try:
            lu = splu(matrix)
        except Exception:
            continue
        for (load, power_id, ground_id), rhs in zip(connected, rhs_list):
            try:
                solved = lu.solve(rhs)
            except Exception:
                continue
            if not np.all(np.isfinite(solved)):
                continue
            power_v = 0.0 if power_id in fixed else solved[index_of[power_id]]
            ground_v = 0.0 if ground_id in fixed else solved[index_of[ground_id]]
            result[load.name][column] = power_v - ground_v
    return result


def _part_impedance(part: PdnPart, omega: float):
    if omega <= 0:
        return None
    impedance = 0j
    present = False
    if part.r_ohm:
        impedance += float(part.r_ohm)
        present = True
    if part.l_nh:
        impedance += 1j * omega * float(part.l_nh) * 1e-9
        present = True
    if part.c_nf:
        capacitance = float(part.c_nf) * 1e-9
        if capacitance <= 0:
            return None
        impedance += 1.0 / (1j * omega * capacitance)
        present = True
    if not present:
        return None
    if abs(impedance) < SHORT_OHM:
        return SHORT_OHM
    return impedance


def _stamp_conductance(rows, cols, values, rhs, index_of, fixed, a, b, conductance) -> None:
    ia = index_of.get(a)
    ib = index_of.get(b)
    va = fixed.get(a)
    vb = fixed.get(b)
    if ia is not None and ib is not None:
        rows.extend((ia, ib, ia, ib))
        cols.extend((ia, ib, ib, ia))
        values.extend((conductance, conductance, -conductance, -conductance))
    elif ia is not None and vb is not None:
        rows.append(ia)
        cols.append(ia)
        values.append(conductance)
        if rhs is not None:
            rhs[ia] += conductance * vb
    elif ib is not None and va is not None:
        rows.append(ib)
        cols.append(ib)
        values.append(conductance)
        if rhs is not None:
            rhs[ib] += conductance * va


def _stamp_current(rhs, index_of, node, amps) -> None:
    row = index_of.get(node)
    if row is not None:
        rhs[row] += amps


def _fail(message: str, warnings: list[str]) -> PdnResult:
    return PdnResult(False, message, [], [], [], np.array([], dtype=float), {}, {}, None, warnings)
