"""Load a KiCad `.kicad_pcb` (v6 through v9, and name-only nets) into a Board.

Copper is stored as world-space polygons. Zone metal comes from the filled
polygon; an unfilled zone is a warning and adds no copper.
"""

from __future__ import annotations

import math
from pathlib import Path

from shapely.affinity import rotate as shp_rotate
from shapely.affinity import translate as shp_translate
from shapely.geometry import Point
from shapely.ops import unary_union

from .board import Board, CopperPoly, Layer, Pad, StackLayer, Via, assign_stackup_z
from .geom import (
    arc_points_center,
    arc_points_three,
    as_polygon,
    buffer_centerline,
    chain_loops,
    circle_world,
    pad_polygon,
    polygon_parts,
    shoelace,
    kicad_rotate,
)
from .sexpr import parse_sexpr

_PAD_SHAPES = {"rect", "roundrect", "circle", "oval"}
_MASK_TYPES = {
    "Top Solder Mask",
    "Bottom Solder Mask",
    "Top Silk Screen",
    "Bottom Silk Screen",
    "Top Solder Paste",
    "Bottom Solder Paste",
}
_COPPER_TYPES = {"copper", "signal"}
_DIELECTRIC_TYPES = {"core", "prepreg", "dielectric"}


def load_kicad_pcb(path: str | Path) -> Board:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    board = parse_kicad_pcb(text, name=path.name)
    return board


def parse_kicad_pcb(text: str, name: str = "") -> Board:
    tree = parse_sexpr(text)
    if not tree or tree[0] != "kicad_pcb":
        raise ValueError("Not a KiCad board file (missing kicad_pcb)")
    board = Board(name=name)
    board.layers = _layers(tree)
    copper_names = [layer.name for layer in board.layers if _is_copper_layer(layer)]
    board.thickness_mm = _general_thickness(tree) or 1.6
    board.stackup, board.assumed_stackup = _stackup(tree, copper_names, board.thickness_mm)
    if board.assumed_stackup:
        board.warnings.append(
            "No copper stackup in the file. Assuming 1.6 mm FR-4 (εr 4.5, tanδ 0.02) "
            "and 35 µm copper. Edit the stackup before trusting a result."
        )
    assign_stackup_z(board.stackup)
    board.nets = _net_table(tree)

    for node in _children(tree, "segment"):
        _add_segment(board, node, _xy_of)
    for node in _children(tree, "arc"):
        _add_arc_track(board, node, _xy_of)
    for node in _children(tree, "via"):
        _add_via(board, node, _xy_of, copper_names)
    for node in list(_children(tree, "zone")):
        _add_zone(board, node)
    for node in list(_children(tree, "footprint")) + list(_children(tree, "module")):
        _add_footprint(board, node, copper_names)

    outline, holes = _outline(tree, board)
    board.outline = outline
    board.holes = holes
    if outline is None:
        board.warnings.append("Edge.Cuts did not form a closed outline. The copper bounding box is used instead.")
    return board


def _children(node, name: str):
    for child in node[1:]:
        if isinstance(child, list) and child and child[0] == name:
            yield child


def _find(node, name: str):
    for child in _children(node, name):
        return child
    return None


def _num(value, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _xy(node) -> tuple[float, float] | None:
    if node is None or len(node) < 3:
        return None
    if not isinstance(node[1], (int, float)) or not isinstance(node[2], (int, float)):
        return None
    return (float(node[1]), float(node[2]))


def _at(node) -> tuple[float, float, float]:
    point = _xy(node)
    if point is None:
        return (0.0, 0.0, 0.0)
    rot = _num(node[3], 0.0) if len(node) > 3 else 0.0
    return (point[0], point[1], rot)


def _xy_of(node) -> tuple[float, float]:
    return _xy(node) or (0.0, 0.0)


def _is_copper_layer(layer: Layer) -> bool:
    return layer.kind == "signal" or layer.name.endswith(".Cu")


def _layers(tree) -> list[Layer]:
    node = _find(tree, "layers")
    layers: list[Layer] = []
    if node is None:
        return layers
    for child in node[1:]:
        if not isinstance(child, list) or len(child) < 3:
            continue
        layer_id = child[0] if isinstance(child[0], int) else len(layers)
        name = str(child[1])
        kind = str(child[2])
        user = str(child[3]) if len(child) > 3 else None
        layers.append(Layer(id=int(layer_id), name=name, kind=kind, user_name=user))
    return layers


def _general_thickness(tree) -> float | None:
    general = _find(tree, "general")
    if general is None:
        return None
    thickness = _find(general, "thickness")
    if thickness is None or len(thickness) < 2:
        return None
    return _num(thickness[1], 1.6)


def _stack_kind(name: str, type_name: str) -> str:
    if type_name.lower() in _COPPER_TYPES or name.endswith(".Cu"):
        return "copper"
    if type_name.lower() in _DIELECTRIC_TYPES or name.lower().startswith("dielectric"):
        return "dielectric"
    if type_name in _MASK_TYPES or "Mask" in type_name or "Silk" in type_name or "Paste" in type_name:
        return "mask"
    return "other"


def _stackup(tree, copper_names: list[str], thickness: float) -> tuple[list[StackLayer], bool]:
    setup = _find(tree, "setup")
    stack = _find(setup, "stackup") if setup is not None else None
    layers: list[StackLayer] = []
    if stack is not None:
        for child in _children(stack, "layer"):
            if len(child) < 2:
                continue
            name = str(child[1])
            type_node = _find(child, "type")
            type_name = str(type_node[1]) if type_node is not None and len(type_node) > 1 else ""
            thick = _find(child, "thickness")
            material = _find(child, "material")
            eps = _find(child, "epsilon_r")
            loss = _find(child, "loss_tangent")
            layers.append(
                StackLayer(
                    name=name,
                    kind=_stack_kind(name, type_name),
                    thickness_mm=_num(thick[1]) if thick is not None and len(thick) > 1 else 0.0,
                    material=str(material[1]) if material is not None and len(material) > 1 else None,
                    eps_r=_num(eps[1]) if eps is not None and len(eps) > 1 else None,
                    loss_tangent=_num(loss[1]) if loss is not None and len(loss) > 1 else None,
                )
            )
    if any(layer.kind == "copper" for layer in layers):
        return layers, False
    names = copper_names or ["F.Cu", "B.Cu"]
    assumed: list[StackLayer] = []
    n_gaps = max(len(names) - 1, 1)
    diel = thickness / n_gaps
    for index, name in enumerate(names):
        assumed.append(StackLayer(name=name, kind="copper", thickness_mm=0.035))
        if index != len(names) - 1:
            assumed.append(
                StackLayer(
                    name=f"dielectric {index + 1}",
                    kind="dielectric",
                    thickness_mm=diel,
                    material="FR4",
                    eps_r=4.5,
                    loss_tangent=0.02,
                )
            )
    return assumed, True


def _net_table(tree) -> dict[int, str]:
    nets: dict[int, str] = {}
    for node in _children(tree, "net"):
        if len(node) >= 3 and isinstance(node[1], int):
            nets[int(node[1])] = str(node[2])
        elif len(node) >= 2 and isinstance(node[1], int):
            nets[int(node[1])] = ""
    return nets


class _Nets:
    def __init__(self, table: dict[int, str]):
        self.table = table

    def resolve(self, node) -> str:
        """Accept (net 2), (net 2 \"SIG\"), and (net \"SIG\")."""
        if node is None or len(node) < 2:
            return ""
        if isinstance(node[1], str):
            return node[1]
        if isinstance(node[1], int):
            if len(node) >= 3 and isinstance(node[2], str):
                name = node[2]
                self.table.setdefault(int(node[1]), name)
                return name
            return self.table.get(int(node[1]), "")
        return ""


def _add_segment(board: Board, node, xy_fn, nets: _Nets | None = None, width_scale: float = 1.0):
    nets = nets or _Nets(board.nets)
    start = _find(node, "start")
    end = _find(node, "end")
    width = _find(node, "width")
    layer = _find(node, "layer")
    if start is None or end is None or layer is None:
        return
    a = xy_fn(start)
    b = xy_fn(end)
    w = _num(width[1], 0.0) * width_scale if width is not None and len(width) > 1 else 0.0
    poly = buffer_centerline([a, b], w)
    if not poly:
        return
    board.copper.append(
        CopperPoly(
            points=poly,
            layer=str(layer[1]),
            net=nets.resolve(_find(node, "net")),
            kind="trace",
            feature_mm=w,
            source="segment",
        )
    )


def _add_arc_track(board: Board, node, xy_fn, nets: _Nets | None = None):
    nets = nets or _Nets(board.nets)
    start_n = _find(node, "start")
    end_n = _find(node, "end")
    mid_n = _find(node, "mid")
    layer = _find(node, "layer")
    width = _find(node, "width")
    if start_n is None or end_n is None or layer is None:
        return
    start = xy_fn(start_n)
    end = xy_fn(end_n)
    if mid_n is not None:
        pts = arc_points_three(start, xy_fn(mid_n), end)
    else:
        angle = _find(node, "angle")
        # Track arcs in current files use start/mid/end. Angle form: start is the center.
        pts = arc_points_center(start, end, _num(angle[1], 0.0) if angle is not None else 0.0)
    w = _num(width[1], 0.0) if width is not None and len(width) > 1 else 0.0
    poly = buffer_centerline(pts, w)
    if not poly:
        return
    board.copper.append(
        CopperPoly(
            points=poly,
            layer=str(layer[1]),
            net=nets.resolve(_find(node, "net")),
            kind="trace",
            feature_mm=w,
            source="arc",
        )
    )


def _span_layers(start: str, end: str, copper_names: list[str]) -> list[str]:
    if start == "*.Cu" or end == "*.Cu":
        return list(copper_names)
    if start not in copper_names or end not in copper_names:
        return [name for name in (start, end) if name.endswith(".Cu") or name in copper_names]
    i, j = copper_names.index(start), copper_names.index(end)
    lo, hi = sorted((i, j))
    return copper_names[lo : hi + 1]


def _add_via(board: Board, node, xy_fn, copper_names: list[str], nets: _Nets | None = None):
    nets = nets or _Nets(board.nets)
    at = _find(node, "at")
    size = _find(node, "size")
    drill = _find(node, "drill")
    layers = _find(node, "layers")
    if at is None or layers is None or len(layers) < 3:
        return
    x, y = xy_fn(at)
    drill_mm = _drill_mm(drill)
    size_mm = _num(size[1], drill_mm) if size is not None and len(size) > 1 else drill_mm
    start, end = str(layers[1]), str(layers[2])
    spanned = _span_layers(start, end, copper_names)
    net = nets.resolve(_find(node, "net"))
    board.vias.append(Via(x=x, y=y, drill_mm=drill_mm, size_mm=size_mm, layers=(start, end), net=net, source="via"))
    if size_mm > 0 and spanned:
        ring = circle_world((x, y), size_mm / 2)
        for layer in spanned:
            board.copper.append(
                CopperPoly(points=ring, layer=layer, net=net, kind="via", feature_mm=size_mm, source="via")
            )


def _drill_mm(node) -> float:
    if node is None or len(node) < 2:
        return 0.0
    if isinstance(node[1], (int, float)):
        return float(node[1])
    # (drill oval dx dy)
    nums = [float(v) for v in node[1:] if isinstance(v, (int, float))]
    return max(nums) if nums else 0.0


def _pts(node) -> list[tuple[float, float]]:
    pts_node = node if node and node[0] == "pts" else _find(node, "pts")
    if pts_node is None:
        return []
    points = []
    for xy in _children(pts_node, "xy"):
        point = _xy(xy)
        if point is not None:
            points.append(point)
    return points


def _add_zone(board: Board, node, nets: _Nets | None = None, transform=None):
    nets = nets or _Nets(board.nets)
    layer_node = _find(node, "layer")
    layer = str(layer_node[1]) if layer_node is not None and len(layer_node) > 1 else ""
    net = nets.resolve(_find(node, "net"))
    net_name = _find(node, "net_name")
    if net_name is not None and len(net_name) > 1 and isinstance(net_name[1], str):
        net = net or net_name[1]
    fills = list(_children(node, "filled_polygon"))
    if not fills:
        board.warnings.append(
            f"Zone on {layer or 'unknown layer'} net {net or '(unnamed)'} has no fill. "
            "Fill zones in KiCad before simulating; the outline is not copper."
        )
        return
    for fill in fills:
        rings = [_pts(pts) for pts in _children(fill, "pts")]
        rings = [ring for ring in rings if len(ring) >= 3]
        if not rings:
            continue
        fill_layer_node = _find(fill, "layer")
        fill_layer = str(fill_layer_node[1]) if fill_layer_node is not None and len(fill_layer_node) > 1 else layer
        if transform is not None:
            rings = [[transform(x, y) for x, y in ring] for ring in rings]
        outer, holes = rings[0], rings[1:]
        poly = as_polygon(outer, holes)
        if poly is None:
            board.warnings.append(f"Skipped an invalid zone fill on {fill_layer} net {net}.")
            continue
        board.copper.append(
            CopperPoly(
                points=[(float(x), float(y)) for x, y in list(poly.exterior.coords)[:-1]],
                holes=[[(float(x), float(y)) for x, y in list(ring.coords)[:-1]] for ring in poly.interiors],
                layer=fill_layer,
                net=net,
                kind="zone",
                source="zone",
            )
        )


def _expand_pad_layers(raw: list[str], copper_names: list[str]) -> list[str]:
    if any(name == "*.Cu" for name in raw):
        return list(copper_names)
    return [name for name in raw if name.endswith(".Cu") or name in copper_names]


def _add_footprint(board: Board, node, copper_names: list[str]):
    at = _at(_find(node, "at"))
    layer_node = _find(node, "layer")
    fp_layer = str(layer_node[1]) if layer_node is not None and len(layer_node) > 1 else "F.Cu"
    flip = fp_layer == "B.Cu" or fp_layer.startswith("B.")
    origin = (at[0], at[1])
    rotation = at[2]
    ref = _footprint_ref(node)
    nets = _Nets(board.nets)

    def xform(x, y):
        # Back-side footprints are stored unmirrored; KiCad mirrors local X, then
        # rotates with the Y-down convention (positive angle is CCW on the canvas).
        lx = -x if flip else x
        rx, ry = kicad_rotate(lx, y, rotation)
        return (origin[0] + rx, origin[1] + ry)

    for pad_node in _children(node, "pad"):
        _add_pad(board, pad_node, ref, origin, rotation, flip, copper_names, nets)
    for line in _children(node, "fp_line"):
        _collect_fp_graphic(line, xform, board)
    for arc in _children(node, "fp_arc"):
        _collect_fp_graphic(arc, xform, board)
    for rect in _children(node, "fp_rect"):
        _collect_fp_graphic(rect, xform, board)
    for circle in _children(node, "fp_circle"):
        _collect_fp_graphic(circle, xform, board)
    for poly in _children(node, "fp_poly"):
        _collect_fp_graphic(poly, xform, board)
    for zone in _children(node, "zone"):
        _add_zone(board, zone, nets, transform=xform)


def _footprint_ref(node) -> str:
    for prop in _children(node, "property"):
        if len(prop) >= 3 and prop[1] == "Reference":
            return str(prop[2])
    for text in _children(node, "fp_text"):
        if len(text) >= 3 and text[1] == "reference":
            return str(text[2])
    if len(node) > 1 and isinstance(node[1], str):
        return node[1]
    return ""


def _vec2(node) -> tuple[float, float] | None:
    if node is None or len(node) < 3:
        return None
    if isinstance(node[1], (int, float)) and isinstance(node[2], (int, float)):
        return (float(node[1]), float(node[2]))
    return None


def _stroke_width(node) -> float:
    width = _find(node, "width")
    if width is not None and len(width) > 1 and isinstance(width[1], (int, float)):
        return float(width[1])
    stroke = _find(node, "stroke")
    if stroke is None:
        return 0.0
    width = _find(stroke, "width")
    if width is not None and len(width) > 1 and isinstance(width[1], (int, float)):
        return float(width[1])
    return 0.0


def _is_filled(node) -> bool:
    fill = _find(node, "fill")
    if fill is None or len(fill) < 2:
        return False
    if isinstance(fill[1], str):
        return fill[1].lower() in ("yes", "true", "solid")
    kind = _find(fill, "type")
    if kind is not None and len(kind) > 1:
        return str(kind[1]).lower() in ("solid", "yes")
    return False


def _as_poly(points: list[tuple[float, float]] | None):
    if not points:
        return None
    return as_polygon(points)


def _primitive_local(node) -> list:
    """Pad-primitive geometry in the pad's own frame, before the board angle is applied."""
    kind = node[0] if node else ""
    width = _stroke_width(node)
    filled = _is_filled(node)
    geoms = []
    if kind == "gr_poly":
        pts = _pts(node)
        if filled:
            poly = _as_poly(pts)
            if poly is not None:
                geoms.append(poly)
        if width > 0 and len(pts) >= 2:
            loop = list(pts)
            if math.hypot(loop[0][0] - loop[-1][0], loop[0][1] - loop[-1][1]) > 1e-9:
                loop.append(loop[0])
            geoms.append(_as_poly(buffer_centerline(loop, width)))
    elif kind == "gr_line":
        start, end = _xy(_find(node, "start")), _xy(_find(node, "end"))
        if start and end and width > 0:
            geoms.append(_as_poly(buffer_centerline([start, end], width)))
    elif kind == "gr_rect":
        start, end = _xy(_find(node, "start")), _xy(_find(node, "end"))
        if start and end:
            x0, y0 = start
            x1, y1 = end
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            if filled:
                geoms.append(_as_poly(corners))
            if width > 0:
                geoms.append(_as_poly(buffer_centerline(corners + [corners[0]], width)))
    elif kind == "gr_circle":
        center, end = _xy(_find(node, "center")), _xy(_find(node, "end"))
        if center and end:
            radius = math.hypot(end[0] - center[0], end[1] - center[1])
            if radius > 0 and filled:
                outer_r = radius + (width / 2 if width > 0 else 0.0)
                geoms.append(Point(center).buffer(outer_r, quad_segs=8))
            elif radius > 0 and width > 0:
                outer = Point(center).buffer(radius + width / 2, quad_segs=8)
                inner_r = radius - width / 2
                if inner_r > 1e-6:
                    geoms.append(outer.difference(Point(center).buffer(inner_r, quad_segs=8)))
                else:
                    geoms.append(outer)
    elif kind == "gr_arc":
        lines = _graphic_polylines(node)
        if lines and width > 0:
            geoms.append(_as_poly(buffer_centerline(lines[0], width)))
    return [geom for geom in geoms if geom is not None and not geom.is_empty]


def _custom_local_geoms(node, size: tuple[float, float], rratio: float, widths: list[float]) -> list:
    sx, sy = size
    options = _find(node, "options")
    anchor = "circle"
    if options is not None:
        anchor_node = _find(options, "anchor")
        if anchor_node is not None and len(anchor_node) > 1:
            anchor = str(anchor_node[1])
    geoms = []
    if sx > 0 and sy > 0:
        try:
            pts = pad_polygon("circle" if anchor == "circle" else "rect", (sx, sy), (0.0, 0.0), 0.0, False, rratio)
        except ValueError:
            pts = []
        poly = _as_poly(pts)
        if poly is not None:
            geoms.append(poly)
    primitives = _find(node, "primitives")
    if primitives is None:
        return geoms
    for child in primitives[1:]:
        if not isinstance(child, list) or not child:
            continue
        kind = child[0]
        if kind == "gr_vector":
            continue
        stroke = _stroke_width(child)
        if stroke > 0:
            widths.append(stroke)
        if kind not in ("gr_poly", "gr_line", "gr_rect", "gr_circle", "gr_arc"):
            continue
        geoms.extend(_primitive_local(child))
    return geoms


def _place_pad_parts(geoms, shape_center: tuple[float, float], rotation: float):
    usable = [geom for geom in geoms if geom is not None and not geom.is_empty]
    if not usable:
        return []
    merged = unary_union(usable)
    merged = shp_rotate(merged, -rotation, origin=(0, 0))
    merged = shp_translate(merged, shape_center[0], shape_center[1])
    return polygon_parts(merged)


def _add_pad(board, node, ref, origin, rotation, flip, copper_names, nets: _Nets):
    # (pad "1" smd rect ...)
    if len(node) < 4:
        return
    pad_name = str(node[1])
    shape = str(node[3])
    at = _at(_find(node, "at"))
    size_node = _find(node, "size")
    if size_node is None or len(size_node) < 3:
        return
    sx, sy = float(size_node[1]), float(size_node[2])
    local_center = (-at[0], at[1]) if flip else (at[0], at[1])
    # The (at) angle is already the board-frame angle. KiCad leaves it out when that
    # angle is 0, even if the footprint itself is rotated. Do not add the footprint
    # rotation again, and do not negate it for a back-side footprint.
    total_rot = at[2]
    rotated = kicad_rotate(local_center[0], local_center[1], rotation)
    anchor = (origin[0] + rotated[0], origin[1] + rotated[1])
    offset = _vec2(_find(node, "offset")) or (0.0, 0.0)
    shifted = kicad_rotate(offset[0], offset[1], total_rot)
    shape_center = (anchor[0] + shifted[0], anchor[1] + shifted[1])
    layers_node = _find(node, "layers")
    raw_layers = [str(v) for v in layers_node[1:]] if layers_node is not None else []
    layers = _expand_pad_layers(raw_layers, copper_names)
    rratio_node = _find(node, "roundrect_rratio")
    rratio = _num(rratio_node[1], 0.25) if rratio_node is not None and len(rratio_node) > 1 else 0.25
    drill = _drill_mm(_find(node, "drill"))
    net = nets.resolve(_find(node, "net"))
    widths = [min(sx, sy)] if sx > 0 and sy > 0 else []
    if shape == "custom":
        geoms = _custom_local_geoms(node, (sx, sy), rratio, widths)
    elif shape in _PAD_SHAPES:
        try:
            geoms = [_as_poly(pad_polygon(shape, (sx, sy), (0.0, 0.0), 0.0, False, rratio))]
        except ValueError:
            geoms = []
    else:
        board.warnings.append(
            f"Skipped {shape} pad {ref}.{pad_name}; supported shapes are rect, roundrect, circle, oval, and custom."
        )
        return
    parts = _place_pad_parts(geoms, shape_center, total_rot)
    if not parts:
        board.warnings.append(f"Skipped pad {ref}.{pad_name}.")
        return
    largest = max(parts, key=lambda part: abs(shoelace(part[0])))
    placed = as_polygon(largest[0], largest[1])
    center = (float(placed.centroid.x), float(placed.centroid.y)) if placed is not None else shape_center
    feature = min(widths) if widths else min(sx, sy)
    pad = Pad(
        footprint=ref,
        name=pad_name,
        net=net,
        layers=layers,
        center=center,
        points=largest[0],
        drill_mm=drill if drill > 0 else None,
        source=f"{ref}.{pad_name}",
    )
    board.pads.append(pad)
    for part_points, part_holes in parts:
        for layer in layers:
            board.copper.append(
                CopperPoly(
                    points=part_points,
                    holes=part_holes,
                    layer=layer,
                    net=net,
                    kind="pad",
                    feature_mm=feature,
                    source=pad.source,
                )
            )
    if drill > 0 and len(layers) >= 1:
        start, end = layers[0], layers[-1]
        board.vias.append(
            Via(
                x=anchor[0],
                y=anchor[1],
                drill_mm=drill,
                size_mm=min(sx, sy),
                layers=(start, end),
                net=net,
                source=pad.source,
            )
        )


def _collect_fp_graphic(node, xform, board: Board):
    # Reused by the outline pass. Footprint graphics that are not Edge.Cuts are ignored.
    layer = _find(node, "layer")
    if layer is None or len(layer) < 2 or str(layer[1]) != "Edge.Cuts":
        return
    # Stash on the board via a private list. The outline walker reads board-level graphics
    # itself; footprint Edge.Cuts are appended as already-transformed polylines.
    lines = getattr(board, "_fp_outline", None)
    if lines is None:
        lines = []
        board._fp_outline = lines  # type: ignore[attr-defined]
    for poly in _graphic_polylines(node):
        lines.append([xform(x, y) for x, y in poly])


def _graphic_polylines(node) -> list[list[tuple[float, float]]]:
    kind = node[0]
    if kind in ("gr_line", "fp_line"):
        start, end = _xy(_find(node, "start")), _xy(_find(node, "end"))
        if start and end:
            return [[start, end]]
    elif kind in ("gr_rect", "fp_rect"):
        start, end = _xy(_find(node, "start")), _xy(_find(node, "end"))
        if start and end:
            x0, y0 = start
            x1, y1 = end
            return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]
    elif kind in ("gr_circle", "fp_circle"):
        center, end = _xy(_find(node, "center")), _xy(_find(node, "end"))
        if center and end:
            radius = ((end[0] - center[0]) ** 2 + (end[1] - center[1]) ** 2) ** 0.5
            ring = circle_world(center, radius, 48)
            return [ring + [ring[0]]]
    elif kind in ("gr_poly", "fp_poly"):
        pts = _pts(node)
        if len(pts) >= 3:
            return [pts + [pts[0]]]
    elif kind in ("gr_arc", "fp_arc"):
        start, end = _xy(_find(node, "start")), _xy(_find(node, "end"))
        mid = _xy(_find(node, "mid"))
        if start and end and mid:
            return [arc_points_three(start, mid, end)]
        angle = _find(node, "angle")
        if start and end and angle is not None:
            return [arc_points_center(start, end, _num(angle[1], 0.0))]
    return []


def _outline(tree, board: Board):
    polylines: list[list[tuple[float, float]]] = []
    for kind in ("gr_line", "gr_arc", "gr_rect", "gr_poly", "gr_circle"):
        for node in _children(tree, kind):
            layer = _find(node, "layer")
            if layer is None or len(layer) < 2 or str(layer[1]) != "Edge.Cuts":
                continue
            polylines.extend(_graphic_polylines(node))
    polylines.extend(getattr(board, "_fp_outline", []))
    loops = chain_loops(polylines)
    if not loops:
        return None, []
    loops.sort(key=lambda ring: abs(shoelace(ring)), reverse=True)
    outer = loops[0]
    outer_poly = as_polygon(outer)
    holes = []
    if outer_poly is not None:
        for ring in loops[1:]:
            hole = as_polygon(ring)
            if hole is not None and outer_poly.contains(hole.representative_point()):
                holes.append(ring)
    return outer, holes
