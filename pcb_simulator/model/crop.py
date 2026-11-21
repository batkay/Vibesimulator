"""Choose the copper that goes into one simulation.

Crop mode keeps a rectangle (or the bounding box of the selected nets) plus a
margin, clipped to the board outline. Nets that are checked are kept, and each
reference net is kept even when it is not checked. With no nets checked, every
net in the region is kept. Outline mode uses the whole board outline.

``signal_only`` drops reference vias, and the annular rings on those vias,
that sit farther than ``RETURN_VIA_MM`` from copper that is not a reference.
The reference pour stays. The caller decides which nets count as selected.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from shapely.geometry import Point, Polygon, box
from shapely.strtree import STRtree

from .board import Board, CopperPoly, Pad, Via
from .geom import as_polygon, polygon_parts, shoelace


@dataclass
class CropSettings:
    mode: str = "crop"  # "crop" or "outline"
    rect: tuple[float, float, float, float] | None = None
    margin_mm: float = 2.0
    selected_nets: list[str] = field(default_factory=list)
    reference_net: str = "GND"
    reference_nets: list[str] = field(default_factory=list)
    # Off keeps every allowed via. On keeps a reference via only near signal copper.
    signal_only: bool = False


# A return via this close to the signal still stitches the pair. Farther ones
# are stitching vias, and each one pulls the copper lattice across the board.
RETURN_VIA_MM = 1.5


@dataclass
class CroppedBoard:
    region: Polygon
    copper: list[CopperPoly]
    vias: list[Via]
    pads: list[Pad]
    warnings: list[str]


def _reference_names(reference_net: str | list[str] | set[str] | tuple[str, ...]) -> set[str]:
    if isinstance(reference_net, str):
        return {reference_net} if reference_net else set()
    return {name for name in reference_net if name}


def auto_crop_nets(
    port_nets: list[str],
    checked_nets: list[str],
    reference_net: str | list[str] | set[str] | tuple[str, ...],
) -> list[str]:
    """Nets that size an automatic crop.

    A placed port is the net being simulated. Checked nets are the fallback.
    Reference nets are omitted: a ground pour is the whole board, and the crop
    keeps that copper inside the box either way.
    """

    omitted = _reference_names(reference_net)

    def unique(names: list[str]) -> list[str]:
        seen: set[str] = set()
        chosen: list[str] = []
        for name in names:
            if not name or name in omitted or name in seen:
                continue
            seen.add(name)
            chosen.append(name)
        return chosen

    from_ports = unique(port_nets)
    if from_ports:
        return from_ports
    return unique(checked_nets)


def bounds_of_nets(
    board: Board,
    nets: set[str],
    extra: list[tuple[float, float]] | None = None,
) -> tuple[float, float, float, float] | None:
    """Axis-aligned bounds of the named nets. The margin is applied later."""
    xs: list[float] = []
    ys: list[float] = []
    for poly in board.copper:
        if poly.net not in nets:
            continue
        for x, y in poly.points:
            xs.append(float(x))
            ys.append(float(y))
    for pad in board.pads:
        if pad.net not in nets:
            continue
        for x, y in pad.points or [pad.center]:
            xs.append(float(x))
            ys.append(float(y))
    for via in board.vias:
        if via.net not in nets:
            continue
        radius = float(via.drill_mm) / 2.0
        xs.extend((via.x - radius, via.x + radius))
        ys.extend((via.y - radius, via.y + radius))
    for x, y in extra or []:
        xs.append(float(x))
        ys.append(float(y))
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def outline_polygon(board: Board) -> Polygon:
    if board.outline and len(board.outline) >= 3:
        poly = as_polygon(board.outline, board.holes)
        if poly is not None:
            return poly
    return _copper_bounds_polygon(board)


def _copper_bounds_polygon(board: Board) -> Polygon:
    xs: list[float] = []
    ys: list[float] = []
    for poly in board.copper:
        for x, y in poly.points:
            xs.append(x)
            ys.append(y)
    for pad in board.pads:
        xs.append(pad.center[0])
        ys.append(pad.center[1])
    if not xs:
        return box(0, 0, 1, 1)
    pad = 1.0
    return box(min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def region_polygon(board: Board, settings: CropSettings) -> Polygon:
    outline = outline_polygon(board)
    if settings.mode == "outline" or (settings.rect is None and not settings.selected_nets):
        return outline
    if settings.rect is not None:
        x0, y0, x1, y1 = settings.rect
        m = settings.margin_mm
        rect = box(min(x0, x1) - m, min(y0, y1) - m, max(x0, x1) + m, max(y0, y1) + m)
        region = outline.intersection(rect)
    else:
        selected = set(settings.selected_nets)
        xs: list[float] = []
        ys: list[float] = []
        for poly in board.copper:
            if poly.net not in selected:
                continue
            for x, y in poly.points:
                xs.append(x)
                ys.append(y)
        if not xs:
            return outline
        m = settings.margin_mm
        rect = box(min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)
        region = outline.intersection(rect)
    if region.is_empty:
        return outline
    if region.geom_type == "MultiPolygon":
        region = max(region.geoms, key=lambda g: g.area)
    if region.geom_type != "Polygon":
        return outline
    return region


def kept_references(settings: CropSettings) -> set[str]:
    names = _reference_names(settings.reference_nets)
    if settings.reference_net:
        names.add(settings.reference_net)
    return names


def _allowed(net: str, settings: CropSettings) -> bool:
    if not settings.selected_nets:
        return True
    return net in settings.selected_nets or net in kept_references(settings)


def crop_board(board: Board, settings: CropSettings, progress=None) -> CroppedBoard:
    region = region_polygon(board, settings)
    warnings: list[str] = []
    copper: list[CopperPoly] = []
    total = len(board.copper)
    if progress is not None:
        progress({"event": "crop", "done": 0, "total": total})
    last = time.monotonic()
    for index, poly in enumerate(board.copper):
        if _allowed(poly.net, settings):
            geom = as_polygon(poly.points, poly.holes)
            if geom is not None:
                clipped = geom.intersection(region)
                for ext, holes in polygon_parts(clipped):
                    copper.append(
                        CopperPoly(
                            points=ext,
                            holes=holes,
                            layer=poly.layer,
                            net=poly.net,
                            kind=poly.kind,
                            feature_mm=poly.feature_mm,
                            source=poly.source,
                        )
                    )
        if progress is not None and time.monotonic() - last >= 2.0:
            progress({"event": "crop", "done": index + 1, "total": total})
            last = time.monotonic()
    vias = []
    for via in board.vias:
        if _allowed(via.net, settings) and region.covers(Point(via.x, via.y)):
            vias.append(via)
    pads = []
    for pad in board.pads:
        if not _allowed(pad.net, settings):
            continue
        if region.covers(Point(*pad.center)) or region.intersects(as_polygon(pad.points) or Point(*pad.center)):
            pads.append(pad)
    if settings.signal_only:
        copper, vias, omitted = _nearby_returns(copper, vias, pads, settings)
        if omitted:
            warnings.append(
                f"Left out {omitted} reference vias farther than {RETURN_VIA_MM:g} mm from the signal copper."
            )
    present = {poly.net for poly in copper}
    for name in sorted(kept_references(settings)):
        if name not in present:
            warnings.append(f"No {name} copper in the simulation region.")
    if not copper:
        warnings.append("The simulation region has no copper.")
    if progress is not None:
        progress({"event": "crop", "copper": len(copper), "vias": len(vias)})
    return CroppedBoard(region=region, copper=copper, vias=vias, pads=pads, warnings=warnings)


def _nearby_returns(
    copper: list[CopperPoly],
    vias: list[Via],
    pads: list[Pad],
    settings: CropSettings,
) -> tuple[list[CopperPoly], list[Via], int]:
    """Drop reference vias, and their annular rings, that sit far from the signal.

    The reference pour stays. A via within ``RETURN_VIA_MM`` of non-reference
    copper stays, which is the stitch beside a pair. With no signal copper,
    every via stays.
    """

    refs = kept_references(settings)
    signal = _signal_geometry(copper, vias, pads, refs)
    if not signal:
        return copper, vias, 0
    tree = STRtree(signal)
    limit = RETURN_VIA_MM

    def near(geom) -> bool:
        if geom is None or geom.is_empty:
            return False
        query = geom.buffer(limit)
        for index in tree.query(query):
            if signal[int(index)].distance(geom) <= limit + 1e-9:
                return True
        return False

    kept_copper: list[CopperPoly] = []
    for poly in copper:
        if poly.net in refs and poly.kind == "via":
            if near(as_polygon(poly.points, poly.holes)):
                kept_copper.append(poly)
            continue
        kept_copper.append(poly)
    kept_vias: list[Via] = []
    omitted = 0
    for via in vias:
        if via.net not in refs:
            kept_vias.append(via)
            continue
        barrel = _via_footprint(via.x, via.y, via.drill_mm)
        if near(barrel):
            kept_vias.append(via)
        else:
            omitted += 1
    return kept_copper, kept_vias, omitted


def _signal_geometry(copper: list[CopperPoly], vias: list[Via], pads: list[Pad], refs: set[str]):
    geoms = []
    for poly in copper:
        if poly.net in refs:
            continue
        geom = as_polygon(poly.points, poly.holes)
        if geom is not None and not geom.is_empty:
            geoms.append(geom)
    for pad in pads:
        if pad.net in refs:
            continue
        geom = as_polygon(pad.points) or Point(*pad.center)
        if geom is not None and not geom.is_empty:
            geoms.append(geom)
    for via in vias:
        if via.net in refs:
            continue
        geoms.append(_via_footprint(via.x, via.y, via.drill_mm))
    return geoms


def _via_footprint(x: float, y: float, drill_mm: float):
    radius = max(float(drill_mm), 0.0) / 2.0
    if radius <= 0:
        return Point(x, y)
    return Point(x, y).buffer(radius)


def region_ring(region: Polygon) -> list[tuple[float, float]]:
    if region.geom_type != "Polygon":
        return []
    pts = [(float(x), float(y)) for x, y in list(region.exterior.coords)[:-1]]
    if shoelace(pts) == 0:
        return []
    return pts
