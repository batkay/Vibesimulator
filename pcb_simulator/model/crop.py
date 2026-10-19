"""Choose the copper that goes into one simulation.

Crop mode keeps a rectangle (or the bounding box of the selected nets) plus a
margin, clipped to the board outline. Nets that are checked are kept, and the
reference net is kept even when it is not checked. With no nets checked, every
net in the region is kept. Outline mode uses the whole board outline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from shapely.geometry import Point, Polygon, box

from .board import Board, CopperPoly, Pad, Via
from .geom import as_polygon, polygon_parts, shoelace


@dataclass
class CropSettings:
    mode: str = "crop"  # "crop" or "outline"
    rect: tuple[float, float, float, float] | None = None
    margin_mm: float = 2.0
    selected_nets: list[str] = field(default_factory=list)
    reference_net: str = "GND"


@dataclass
class CroppedBoard:
    region: Polygon
    copper: list[CopperPoly]
    vias: list[Via]
    pads: list[Pad]
    warnings: list[str]


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


def _allowed(net: str, settings: CropSettings) -> bool:
    if not settings.selected_nets:
        return True
    return net in settings.selected_nets or net == settings.reference_net


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
    if settings.reference_net and not any(poly.net == settings.reference_net for poly in copper):
        warnings.append(f"No {settings.reference_net} copper in the simulation region.")
    if not copper:
        warnings.append("The simulation region has no copper.")
    if progress is not None:
        progress({"event": "crop", "copper": len(copper), "vias": len(vias)})
    return CroppedBoard(region=region, copper=copper, vias=vias, pads=pads, warnings=warnings)


def region_ring(region: Polygon) -> list[tuple[float, float]]:
    if region.geom_type != "Polygon":
        return []
    pts = [(float(x), float(y)) for x, y in list(region.exterior.coords)[:-1]]
    if shoelace(pts) == 0:
        return []
    return pts
