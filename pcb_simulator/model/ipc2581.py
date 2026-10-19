"""Load an IPC-2581 file into a Board.

Altium and Xpedition export this XML. Coordinates and thicknesses follow
CadHeader units (millimeters or inches). Geometry is built Y-up, then Y is
negated once so the Y-down view matches the CAD window.

Pad copper is taken from LayerFeature. A PadStackDef is only flattened onto a
conductor layer that has no LayerFeature copper, so the same pad is not meshed
twice.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .board import Board, CopperPoly, Layer, Pad, StackLayer, Via
from .exchange import finish_stackup, flip_y, remember_net
from .geom import arc_points_ends, as_polygon, buffer_centerline, circle_world, pad_polygon, transform_point

_PRIMITIVES = {
    "Circle",
    "RectCenter",
    "RectRound",
    "RectCorner",
    "Oval",
    "Diamond",
    "Donut",
    "Polygon",
    "Polyline",
    "Contour",
    "Butterfly",
    "Thermal",
    "Moire",
}
_SKIP_DEFS = {"PadStackDef", "DictionaryStandard", "DictionaryUser", "DictionaryFont"}


def load_ipc2581(path: str | Path) -> Board:
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"Not valid IPC-2581 XML: {exc}") from exc
    local = _local(root.tag).upper().replace("_", "").replace("-", "")
    if "IPC2581" not in local:
        raise ValueError(
            "This XML file is not IPC-2581. Export IPC-2581 from Altium or Xpedition and open that."
        )
    scale = _unit_scale(root)
    board = Board(name=path.name)
    if scale == 1.0 and not any(_local(el.tag) == "CadHeader" for el in root.iter()):
        board.warnings.append("IPC-2581 file has no CadHeader units. Coordinates are read as millimeters.")

    functions = _layer_functions(root)
    stack, conductors = _stackup(root, functions, scale, board)
    finish_stackup(board, stack, real=any(layer.kind == "dielectric" and layer.thickness_mm > 0 for layer in stack))
    if not conductors:
        conductors = [layer.name for layer in board.stackup if layer.kind == "copper"]
    for index, name in enumerate(conductors):
        board.layers.append(Layer(id=index, name=name, kind="signal"))

    _profile(root, board, scale)
    dictionary = _dictionary(root)
    state = {"index": 0, "special": False, "scale": scale}
    for feature in root.iter():
        if _local(feature.tag) != "LayerFeature":
            continue
        layer = _attr(feature, "layerRef", "layer") or ""
        if layer not in conductors:
            continue
        _walk(feature, "", board, layer, dictionary, scale, state)
    if state["special"]:
        board.warnings.append("Skipped thermal, butterfly, or moire pads.")

    parents = {child: parent for parent in root.iter() for child in parent}
    _holes(root, board, parents, conductors, scale)
    _padstacks(root, board, parents, dictionary, conductors, scale)
    if board.outline is None:
        board.warnings.append("IPC-2581 profile did not form a closed outline. The copper bounding box is used instead.")
    return flip_y(board)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(el: ET.Element | None, *names: str) -> str | None:
    if el is None:
        return None
    lowered = {key.lower(): value for key, value in el.attrib.items()}
    for name in names:
        if name in el.attrib:
            return el.attrib[name]
        found = lowered.get(name.lower())
        if found is not None:
            return found
    return None


def _num(el: ET.Element | None, *names: str, default: float = 0.0) -> float:
    raw = _attr(el, *names)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _flag(el: ET.Element, name: str) -> bool:
    return (_attr(el, name) or "").strip().lower() in {"true", "1", "yes", "y"}


def _unit_scale(root: ET.Element) -> float:
    for el in root.iter():
        if _local(el.tag) != "CadHeader":
            continue
        token = _attr(el, "units", "unit") or ""
        if not token:
            for child in el:
                if _local(child.tag).upper().startswith("UNIT"):
                    token = _attr(child, "value", "units", "unit") or (child.text or "")
                    break
        return 25.4 if "INCH" in token.upper() else 1.0
    return 1.0


def _is_conductor(func: str) -> bool:
    text = func.upper()
    return text in {"CONDUCTOR", "SIGNAL", "PLANE"} or "CONDUCT" in text


def _layer_functions(root: ET.Element) -> dict[str, str]:
    functions: dict[str, str] = {}
    for el in root.iter():
        if _local(el.tag) in {"Material", "Entry", "Color", "LineDesc"}:
            continue
        name = _attr(el, "name")
        func = _attr(el, "layerFunction", "function")
        if name and func and name not in functions:
            functions[name] = func.upper()
    return functions


def _material(el: ET.Element) -> tuple[float | None, float | None]:
    eps = None
    loss = None
    for node in el.iter():
        raw_eps = _attr(node, "dielectricConstant", "dielectric_constant")
        raw_loss = _attr(node, "lossTangent", "loss_tangent")
        if raw_eps:
            eps = float(raw_eps)
        if raw_loss:
            loss = float(raw_loss)
    return eps, loss


def _stackup(
    root: ET.Element,
    functions: dict[str, str],
    scale: float,
    board: Board,
) -> tuple[list[StackLayer], list[str]]:
    elements = [el for el in root.iter() if _local(el.tag) == "StackupLayer"]
    if elements and all(_attr(el, "sequence") for el in elements):
        elements.sort(key=lambda el: float(_attr(el, "sequence") or 0))
    stack: list[StackLayer] = []
    conductors: list[str] = []
    for el in elements:
        ref = _attr(el, "layerOrGroupRef", "layerRef", "name") or ""
        if not ref:
            continue
        func = functions.get(ref, _attr(el, "layerFunction", "function") or "")
        eps, loss = _material(el)
        thickness = _num(el, "thickness") * scale
        kind = _stack_kind(ref, func, eps, conductors)
        if kind == "skip":
            continue
        if kind == "copper":
            if ref not in conductors:
                conductors.append(ref)
            stack.append(StackLayer(name=ref, kind="copper", thickness_mm=thickness if thickness > 0 else 0.035))
            continue
        if kind == "dielectric":
            if thickness > 0 and eps is None:
                eps = 4.5
                if loss is None:
                    loss = 0.02
                board.warnings.append(
                    f"{ref} has thickness but no dielectric constant. Using εr 4.5 and tanδ 0.02."
                )
            elif thickness > 0 and loss is None:
                loss = 0.02
            stack.append(
                StackLayer(
                    name=ref,
                    kind="dielectric",
                    thickness_mm=thickness,
                    eps_r=eps,
                    loss_tangent=loss,
                    material="FR4" if eps else None,
                )
            )
            continue
        if kind == "mask":
            stack.append(StackLayer(name=ref, kind="mask", thickness_mm=thickness))
    if not conductors:
        for name, func in functions.items():
            if _is_conductor(func) and name not in conductors:
                conductors.append(name)
    return stack, conductors


def _stack_kind(ref: str, func: str, eps: float | None, conductors: list[str]) -> str:
    text = func.upper()
    if ref in conductors or _is_conductor(text):
        return "copper"
    if "MASK" in text:
        return "mask"
    if any(word in text for word in ("SILK", "PASTE", "DOCUMENT", "DRILL")):
        return "skip"
    if "DIEL" in text or eps is not None:
        return "dielectric"
    return "skip"


def _drop_close(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) >= 2 and abs(points[0][0] - points[-1][0]) < 1e-9 and abs(points[0][1] - points[-1][1]) < 1e-9:
        return points[:-1]
    return points


def _ring_from(poly: ET.Element, scale: float) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for child in list(poly):
        tag = _local(child.tag)
        if tag in {"PolyBegin", "PolyStepSegment"}:
            points.append((_num(child, "x") * scale, _num(child, "y") * scale))
        elif tag == "PolyStepCurve" and points:
            end = (_num(child, "x") * scale, _num(child, "y") * scale)
            center = (_num(child, "centerX") * scale, _num(child, "centerY") * scale)
            arc = arc_points_ends(points[-1], end, center, _flag(child, "clockwise"))
            points.extend(arc[1:])
    return _drop_close(points)


def _contour(el: ET.Element, scale: float) -> tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]]:
    outers: list[list[tuple[float, float]]] = []
    holes: list[list[tuple[float, float]]] = []

    def take(poly: ET.Element, into: list) -> None:
        ring = _ring_from(poly, scale)
        if len(ring) >= 3:
            into.append(ring)

    nodes = [el] if _local(el.tag) == "Polygon" else list(el)
    for node in nodes:
        tag = _local(node.tag)
        if tag == "Polygon":
            take(node, outers)
            for child in node:
                if _local(child.tag) == "Cutout":
                    for sub in child.iter():
                        if _local(sub.tag) == "Polygon" and sub is not node:
                            take(sub, holes)
        elif tag == "Cutout":
            for sub in node.iter():
                if _local(sub.tag) == "Polygon":
                    take(sub, holes)
        elif tag == "Polygon":
            take(node, outers)
    if _local(el.tag) == "Contour":
        outers.clear()
        holes.clear()
        for child in el:
            tag = _local(child.tag)
            if tag == "Polygon":
                take(child, outers)
            elif tag == "Cutout":
                for sub in child.iter():
                    if _local(sub.tag) == "Polygon":
                        take(sub, holes)
    return outers, holes


def _profile(root: ET.Element, board: Board, scale: float) -> None:
    for el in root.iter():
        if _local(el.tag) != "Profile":
            continue
        outers, holes = _contour(el, scale)
        if outers:
            board.outline = outers[0]
            board.holes.extend(holes)
        return


def _dictionary(root: ET.Element) -> dict[str, ET.Element]:
    found: dict[str, ET.Element] = {}
    for entry in root.iter():
        if _local(entry.tag) != "Entry":
            continue
        ident = _attr(entry, "id")
        prim = _first_primitive(entry)
        if ident and prim is not None:
            found[ident] = prim
    return found


def _first_primitive(el: ET.Element) -> ET.Element | None:
    for node in el.iter():
        if node is el:
            continue
        if _local(node.tag) in _PRIMITIVES:
            return node
    return None


def _lookup(dictionary: dict[str, ET.Element], ident: str | None) -> ET.Element | None:
    if not ident:
        return None
    if ident in dictionary:
        return dictionary[ident]
    for key, value in dictionary.items():
        if key.lower() == ident.lower():
            return value
    return None


def _walk(el, net: str, board: Board, layer: str, dictionary, scale: float, state: dict) -> None:
    tag = _local(el.tag)
    if tag == "Set":
        net = _attr(el, "net", "netName") or net
        for child in list(el):
            _walk(child, net, board, layer, dictionary, scale, state)
        return
    if tag in {"Polyline", "Line", "Arc"}:
        _add_stroke(el, net, board, layer, scale, state)
        return
    if tag in {"Contour", "Polygon"}:
        _add_zone(el, net, board, layer, state)
        return
    if tag == "Pad":
        _add_pad(el, net, board, layer, dictionary, scale, state)
        return
    if tag in {"Butterfly", "Thermal", "Moire"}:
        state["special"] = True
        return
    for child in list(el):
        _walk(child, net, board, layer, dictionary, scale, state)


def _add_stroke(el, net: str, board: Board, layer: str, scale: float, state: dict) -> None:
    points, width, cap = _stroke_points(el, scale)
    index = state["index"]
    state["index"] += 1
    if width is None or width <= 0 or len(points) < 2:
        return
    ring = buffer_centerline(points, width, cap)
    if not ring:
        return
    board.copper.append(
        CopperPoly(
            points=ring,
            layer=layer,
            net=net,
            kind="trace",
            feature_mm=width,
            source=f"{layer}:{index}",
        )
    )
    remember_net(board, net)


def _stroke_points(el: ET.Element, scale: float) -> tuple[list[tuple[float, float]], float | None, str]:
    tag = _local(el.tag)
    if tag == "Line" and _attr(el, "startX") is not None:
        points = [
            (_num(el, "startX") * scale, _num(el, "startY") * scale),
            (_num(el, "endX") * scale, _num(el, "endY") * scale),
        ]
    elif tag == "Arc" and _attr(el, "startX") is not None:
        start = (_num(el, "startX") * scale, _num(el, "startY") * scale)
        end = (_num(el, "endX") * scale, _num(el, "endY") * scale)
        center = (_num(el, "centerX") * scale, _num(el, "centerY") * scale)
        points = arc_points_ends(start, end, center, _flag(el, "clockwise"))
    else:
        points = _ring_from(el, scale)
        if len(points) < 2:
            # Steps may sit one level down, beside a nested polygon.
            for child in el.iter():
                if child is not el and _local(child.tag) in {"Polygon", "Polyline"}:
                    points = _ring_from(child, scale)
                    if len(points) >= 2:
                        break
    width = _num(el, "lineWidth", "width", default=-1)
    cap = "round"
    for node in el.iter():
        if _local(node.tag) != "LineDesc":
            continue
        width = _num(node, "lineWidth", "width", default=width)
        end = (_attr(node, "lineEnd") or "").upper()
        if "SQUARE" in end:
            cap = "square"
    if width < 0:
        return points, None, cap
    return points, width * scale, cap


def _add_zone(el, net: str, board: Board, layer: str, state: dict) -> None:
    from .geom import polygon_parts

    index = state["index"]
    state["index"] += 1
    # scale is recovered from the points already stored by _contour's caller via the element.
    # The walk does not pass scale into zones; read it from coordinates by calling _contour
    # with the same scale the rest of the file used. Stored on the board only after flip,
    # so pass scale through state.
    scale = state.get("scale", 1.0)
    outers, holes = _contour(el, scale)
    if not outers:
        return
    geom = as_polygon(outers[0], holes)
    if geom is None:
        return
    for points, inner in polygon_parts(geom):
        board.copper.append(
            CopperPoly(
                points=points,
                layer=layer,
                net=net,
                kind="zone",
                holes=inner,
                source=f"{layer}:{index}",
            )
        )
    remember_net(board, net)


def _add_pad(el, net: str, board: Board, layer: str, dictionary, scale: float, state: dict) -> None:
    index = state["index"]
    state["index"] += 1
    center, rotation, mirror = _placement(el, scale)
    prim = _pad_primitive(el, dictionary)
    if prim is None:
        state["special"] = state["special"] or False
        board.warnings.append(f"Skipped a pad on {layer} with no primitive.")
        return
    if _local(prim.tag) in {"Butterfly", "Thermal", "Moire"}:
        state["special"] = True
        return
    placed = _place_primitive(prim, center, rotation, mirror, scale)
    if placed is None:
        board.warnings.append(f"Skipped a pad on {layer} ({_local(prim.tag)}).")
        return
    points, holes, feature_mm = placed
    board.copper.append(
        CopperPoly(
            points=points,
            layer=layer,
            net=net,
            kind="pad",
            holes=holes,
            feature_mm=feature_mm,
            source=f"{layer}:{index}",
        )
    )
    board.pads.append(
        Pad(
            footprint=layer,
            name=str(index),
            net=net,
            layers=[layer],
            center=center,
            points=points,
            source=f"{layer}:{index}",
        )
    )
    remember_net(board, net)


def _placement(el: ET.Element, scale: float) -> tuple[tuple[float, float], float, bool]:
    for child in list(el):
        if _local(child.tag) in {"Location", "Xform"}:
            return (
                (_num(child, "x") * scale, _num(child, "y") * scale),
                _num(child, "rotation"),
                _flag(child, "mirror"),
            )
    if _attr(el, "x") is not None:
        return ((_num(el, "x") * scale, _num(el, "y") * scale), _num(el, "rotation"), _flag(el, "mirror"))
    return ((0.0, 0.0), 0.0, False)


def _pad_primitive(el: ET.Element, dictionary: dict[str, ET.Element]) -> ET.Element | None:
    for child in el.iter():
        if child is el:
            continue
        tag = _local(child.tag)
        if tag == "StandardPrimitiveRef":
            return _lookup(dictionary, _attr(child, "id") or (child.text or "").strip())
        if tag in _PRIMITIVES:
            return child
    return None


def _place_primitive(el, center, rotation, mirror, scale):
    tag = _local(el.tag)
    if tag == "Circle":
        diameter = _num(el, "diameter") * scale
        if diameter <= 0:
            return None
        points = pad_polygon("circle", (diameter, diameter), center, rotation, mirror)
        return points, [], diameter
    if tag == "RectCenter":
        width = _num(el, "width") * scale
        height = _num(el, "height") * scale
        if width <= 0 or height <= 0:
            return None
        points = pad_polygon("rect", (width, height), center, rotation, mirror)
        return points, [], min(width, height)
    if tag == "RectRound":
        width = _num(el, "width") * scale
        height = _num(el, "height") * scale
        radius = _num(el, "radius", "cornerRadius") * scale
        if width <= 0 or height <= 0:
            return None
        ratio = 0.0 if min(width, height) <= 0 else max(0.0, min(0.5, radius / min(width, height)))
        points = pad_polygon("roundrect", (width, height), center, rotation, mirror, ratio)
        return points, [], min(width, height)
    if tag == "Oval":
        width = _num(el, "width") * scale
        height = _num(el, "height") * scale
        if width <= 0 or height <= 0:
            return None
        points = pad_polygon("oval", (width, height), center, rotation, mirror)
        return points, [], min(width, height)
    if tag in {"Polygon", "Contour", "Polyline"}:
        outers, holes = _contour(el, scale)
        if not outers:
            return None
        points = [transform_point(x, y, center, rotation, mirror) for x, y in outers[0]]
        moved = [[transform_point(x, y, center, rotation, mirror) for x, y in ring] for ring in holes]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return points, moved, min(max(xs) - min(xs), max(ys) - min(ys))
    return None


def _location(el: ET.Element, scale: float) -> tuple[float, float] | None:
    for child in list(el):
        if _local(child.tag) in {"Location", "Xform"}:
            return (_num(child, "x") * scale, _num(child, "y") * scale)
    if _attr(el, "x") is not None and _attr(el, "y") is not None:
        return (_num(el, "x") * scale, _num(el, "y") * scale)
    return None


def _ancestor(el: ET.Element | None, parents: dict, names: set[str]) -> bool:
    while el is not None:
        if _local(el.tag) in names:
            return True
        el = parents.get(el)
    return False


def _set_net(el: ET.Element | None, parents: dict) -> str:
    while el is not None:
        if _local(el.tag) == "Set":
            return _attr(el, "net", "netName") or ""
        el = parents.get(el)
    return ""


def _unplated(status: str) -> bool:
    text = status.upper().replace("-", "").replace("_", "")
    return "NON" in text or "UNPLAT" in text or "NPTH" in text


def _holes(root, board: Board, parents: dict, conductors: list[str], scale: float) -> None:
    if not conductors:
        return
    for el in root.iter():
        if _local(el.tag) != "Hole":
            continue
        if _ancestor(el, parents, _SKIP_DEFS):
            continue
        diameter = _num(el, "diameter") * scale
        loc = _location(el, scale)
        if diameter <= 0 or loc is None:
            continue
        start = _attr(el, "fromLayer", "startLayer") or ""
        end = _attr(el, "toLayer", "endLayer") or ""
        if start not in conductors or end not in conductors:
            start, end = conductors[0], conductors[-1]
        net = _set_net(el, parents)
        if _unplated(_attr(el, "platingStatus", "plating") or ""):
            board.holes.append(circle_world(loc, diameter / 2))
            continue
        board.vias.append(
            Via(
                x=loc[0],
                y=loc[1],
                drill_mm=diameter,
                size_mm=diameter,
                layers=(start, end),
                net=net,
                source="hole",
            )
        )
        remember_net(board, net)


def _padstacks(root, board: Board, parents, dictionary, conductors: list[str], scale: float) -> None:
    filled = {poly.layer for poly in board.copper}
    if conductors and all(name in filled for name in conductors):
        return
    defs: dict[str, dict[str, ET.Element]] = {}
    for el in root.iter():
        if _local(el.tag) != "PadStackDef":
            continue
        name = _attr(el, "name")
        if not name:
            continue
        pads: dict[str, ET.Element] = {}
        for child in el.iter():
            if _local(child.tag) not in {"PadStackPadDef", "PadstackPadDef"}:
                continue
            layer = _attr(child, "layerRef", "layer") or ""
            prim = _first_primitive(child)
            if layer and prim is not None and layer not in filled:
                pads[layer] = prim
        defs[name] = pads
    if not defs:
        return
    state = {"index": 0, "special": False}
    for el in root.iter():
        ref = _attr(el, "padstackDefRef", "padStackDefRef")
        if not ref or ref not in defs or _ancestor(el, parents, _SKIP_DEFS):
            continue
        center, rotation, mirror = _placement(el, scale)
        if center == (0.0, 0.0) and _location(el, scale) is None:
            continue
        for layer, prim in defs[ref].items():
            if layer in filled or layer not in conductors:
                continue
            if _local(prim.tag) in {"Butterfly", "Thermal", "Moire"}:
                state["special"] = True
                continue
            placed = _place_primitive(prim, center, rotation, mirror, scale)
            if placed is None:
                continue
            points, holes, feature_mm = placed
            source = f"{layer}:padstack:{state['index']}"
            state["index"] += 1
            board.copper.append(
                CopperPoly(
                    points=points,
                    layer=layer,
                    net=_set_net(el, parents),
                    kind="pad",
                    holes=holes,
                    feature_mm=feature_mm,
                    source=source,
                )
            )
            board.pads.append(
                Pad(
                    footprint=layer,
                    name=source,
                    net=_set_net(el, parents),
                    layers=[layer],
                    center=center,
                    points=points,
                    source=source,
                )
            )
            remember_net(board, _set_net(el, parents))
    if state["special"]:
        board.warnings.append("Skipped thermal, butterfly, or moire pads.")
