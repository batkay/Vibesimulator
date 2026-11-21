"""Board view. Scene axes match the KiCad file and the default canvas: +X right, +Y down."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from ..model.board import Board
from ..model.geom import as_polygon
from ..model.mesh import Mesh, PortBox
from ..model.ports import SHEET_MM

_COLORS = {
    "F.Cu": "#FF4D4D",
    "In1.Cu": "#FFD24D",
    "In2.Cu": "#4DFF6A",
    "In3.Cu": "#4DFFFF",
    "In4.Cu": "#C44DFF",
    "B.Cu": "#4D7DFF",
}
_FALLBACK = ("#FF9A4D", "#B8FF4D", "#4DFFD2", "#4DA6FF", "#FF4DA6", "#E6E64D")
_PICK_PX = 8.0


def layer_color(name: str, index: int) -> QColor:
    color = QColor(_COLORS.get(name, _FALLBACK[index % len(_FALLBACK)]))
    color.setAlpha(175)
    return color


def _heat_color(value: float, lo: float, hi: float) -> QColor:
    """Red is the low end of the scale. On a voltage overlay that is the larger drop."""
    if hi <= lo:
        span = 0.5
    else:
        span = (value - lo) / (hi - lo)
    red = int(40 + 200 * (1.0 - span))
    blue = int(40 + 200 * span)
    return QColor(red, 48, blue, 150)


class _HeatItem(QGraphicsItem):
    """One item for the whole PDN overlay. A cell is a colored square, not its own item."""

    def __init__(self, cells: list[tuple[float, float, float, float]]):
        super().__init__()
        self.cells = cells
        self.setZValue(45)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        xs = [cell[0] for cell in cells]
        ys = [cell[1] for cell in cells]
        pitches = [cell[2] for cell in cells]
        self._bounds = QRectF(
            min(xs),
            min(ys),
            max(x + pitch for x, pitch in zip(xs, pitches)) - min(xs),
            max(y + pitch for y, pitch in zip(ys, pitches)) - min(ys),
        )
        values = [cell[3] for cell in cells if cell[3] == cell[3]]
        self._lo = min(values) if values else 0.0
        self._hi = max(values) if values else 0.0

    def boundingRect(self) -> QRectF:
        return self._bounds

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        for x, y, pitch, value in self.cells:
            if value != value:
                continue
            painter.setBrush(QBrush(_heat_color(value, self._lo, self._hi)))
            painter.drawRect(QRectF(x, y, pitch, pitch))


class _MeshItem(QGraphicsItem):
    def __init__(self, mesh: Mesh):
        super().__init__()
        self.mesh = mesh
        self.setZValue(40)

    def boundingRect(self) -> QRectF:
        return _scene_rect(self.mesh.x[0], self.mesh.y[0], self.mesh.x[-1], self.mesh.y[-1])

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing, False)
        # A line every pixel, drawn with alpha, composites into a white sheet.
        painter.setPen(QPen(QColor(220, 220, 210, 150), 0))
        y0, y1 = self.mesh.y[0], self.mesh.y[-1]
        x0, x1 = self.mesh.x[0], self.mesh.x[-1]
        lod = option.levelOfDetailFromTransform(painter.transform())
        min_sep = (12.0 / lod) if lod > 1e-6 else 0.0
        for x in _decimate(self.mesh.x, min_sep):
            painter.drawLine(QPointF(x, y0), QPointF(x, y1))
        for y in _decimate(self.mesh.y, min_sep):
            painter.drawLine(QPointF(x0, y), QPointF(x1, y))


def _decimate(values, min_sep: float, limit: int = 48) -> list[float]:
    """Keep a sparse grid: both ends, and at most `limit` lines across the span."""
    if len(values) <= 2:
        return list(values)
    span = float(values[-1] - values[0])
    if limit > 1 and span > 0:
        min_sep = max(min_sep, span / limit)
    if min_sep <= 0:
        return list(values)
    kept = [values[0]]
    for value in values[1:-1]:
        if value - kept[-1] >= min_sep:
            kept.append(value)
    last = values[-1]
    if last - kept[-1] < min_sep and len(kept) > 1:
        kept[-1] = last
    elif kept[-1] != last:
        kept.append(last)
    return kept


def _cosmetic(color: QColor, width: int) -> QPen:
    pen = QPen(color)
    pen.setCosmetic(True)
    pen.setWidth(width)
    pen.setCapStyle(Qt.RoundCap)
    return pen


def _pass_clicks(item) -> None:
    """Port marks sit on top of pads. Clicks belong to the tool, not the mark."""
    item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)


def _screen_dot(x: float, y: float, radius: float, fill: QColor, line: QColor) -> QGraphicsEllipseItem:
    """A marker that stays the same size on screen while the board is zoomed."""
    dot = QGraphicsEllipseItem(-radius, -radius, radius * 2, radius * 2)
    dot.setPos(x, y)
    dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
    dot.setBrush(QBrush(fill))
    dot.setPen(QPen(line))
    return dot


def _scene_rect(x0: float, y0: float, x1: float, y1: float) -> QRectF:
    left, right = min(x0, x1), max(x0, x1)
    top, bottom = min(y0, y1), max(y0, y1)
    return QRectF(left, top, right - left, bottom - top)


def _path_from_points(points, holes=None) -> QPainterPath:
    path = QPainterPath()
    path.addPolygon(_polygon(points))
    for hole in holes or []:
        if len(hole) >= 3:
            path.addPolygon(_polygon(hole))
    return path


def _polygon(points) -> QPolygonF:
    poly = QPolygonF()
    for x, y in points:
        poly.append(QPointF(x, y))
    return poly


class BoardView(QGraphicsView):
    """Tools: navigate, crop, port, VRM, load, part, and measure."""

    status_text = Signal(str)
    crop_rect = Signal(float, float, float, float)
    pad_clicked = Signal(object)
    copper_clicked = Signal(float, float, str)
    net_picked = Signal(str)
    measure_clicked = Signal(float, float, float, float)
    component_picked = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setBackgroundBrush(QColor("#20241f"))
        self.setMouseTracking(True)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.viewport().setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.board: Board | None = None
        self.ports: list[PortBox] = []
        self.vrm = None
        self.loads: list = []
        self.parts: list = []
        self._overlay_cells: list | None = None
        self.mesh: Mesh | None = None
        self.visible_layers: set[str] | None = None
        self.highlight_net: str = ""
        self.show_outline = True
        self.region_points: list[tuple[float, float]] | None = None
        self._tool = "navigate"
        self._pan = False
        self._pan_pos = None
        self._dragging = False
        self._drag_start = (0.0, 0.0)
        self._rubber: QGraphicsRectItem | None = None
        self._two_click_mode = False
        self._port_anchor: tuple[float, float] | None = None
        self._port_mark: QGraphicsEllipseItem | None = None
        self._port_hot: QGraphicsEllipseItem | None = None
        self._port_line: QGraphicsLineItem | None = None
        self._port_halo: QGraphicsLineItem | None = None
        self._port_sheet: QGraphicsRectItem | None = None
        self._measure_anchor: tuple[float, float] | None = None
        self._measurements: list[tuple[list[tuple[float, float]], str]] = []
        self._measure_items: list = []
        self._measure_preview: list = []
        self._probe_marks: list[tuple[float, float, str]] = []
        self._probe_items: list = []
        self._selected: tuple | None = None
        self._copper_tree = None
        self._copper_geoms: list = []
        self._copper_meta: list[tuple[str, str, str]] = []
        self._pad_tree = None
        self._pad_geoms: list = []
        self._pad_items: list = []

    def set_tool(self, tool: str) -> None:
        self._tool = tool
        self._dragging = False
        if self._rubber is not None:
            self.scene().removeItem(self._rubber)
            self._rubber = None
        if tool != "port":
            self.set_two_click(False)
        self.setCursor(Qt.CrossCursor if tool in {"crop", "port", "vrm", "load", "part", "measure"} else Qt.ArrowCursor)
        if tool != "measure":
            self._measure_anchor = None
            self._clear_measure_preview()

    def set_two_click(self, enabled: bool) -> None:
        """Two-click placement draws the first point, then a line that follows the cursor."""
        self._two_click_mode = enabled
        self._clear_port_preview()

    def cancel_gesture(self) -> None:
        """Drop a half-drawn crop or a line that is following the cursor. The tool stays."""
        self._dragging = False
        if self._rubber is not None:
            if self._rubber.scene() is not None:
                self.scene().removeItem(self._rubber)
            self._rubber = None
        self._measure_anchor = None
        self._clear_measure_preview()
        self._clear_port_preview()

    def set_board(self, board: Board) -> None:
        self._selected = None
        self._measurements = []
        self.board = board
        self.mesh = None
        self.region_points = None
        self.highlight_net = ""
        self.visible_layers = None
        self.rebuild()
        self.zoom_fit()

    def set_visible_layers(self, names: set[str]) -> None:
        self.visible_layers = names
        self.rebuild()

    def set_highlight(self, net: str) -> None:
        self.highlight_net = net
        self.rebuild()

    def set_ports(self, ports: list[PortBox]) -> None:
        self.ports = list(ports)
        self.rebuild()

    def set_pdn(self, vrm, loads, parts) -> None:
        self.vrm = vrm
        self.loads = list(loads)
        self.parts = list(parts)
        self.rebuild()

    def set_overlay(self, cells) -> None:
        """Power-net voltage or current-density squares. None clears the overlay."""
        self._overlay_cells = list(cells) if cells else None
        self._replace_overlay()

    def begin_span_preview(self, x: float, y: float) -> None:
        """Mark the first pad of a two-click part. The line follows the cursor."""
        self._two_click_mode = True
        self._port_anchor = (x, y)
        self._show_port_preview((x, y), (x, y))

    def set_region(self, points: list[tuple[float, float]] | None) -> None:
        self.region_points = points
        self.rebuild()

    def set_mesh(self, mesh: Mesh | None) -> None:
        self.mesh = mesh
        if self.board is None:
            self.rebuild()
            return
        # Swapping the overlay avoids redrawing every copper polygon on the window thread.
        scene = self.scene()
        for item in list(scene.items()):
            if isinstance(item, _MeshItem):
                scene.removeItem(item)
        if mesh is not None and len(mesh.x) >= 2 and len(mesh.y) >= 2:
            scene.addItem(_MeshItem(mesh))

    def model_to_scene(self, x: float, y: float) -> QPointF:
        return QPointF(x, y)

    def zoom_fit(self) -> None:
        rect = self._board_rect()
        if rect is None or rect.isEmpty():
            rect = self.scene().itemsBoundingRect()
        if rect.isEmpty():
            return
        self.fitInView(rect.adjusted(-1, -1, 1, 1), Qt.KeepAspectRatio)

    def rebuild(self) -> None:
        # Rubber-band state does not survive a scene clear.
        anchor = self._port_anchor
        measure_anchor = self._measure_anchor
        self._rubber = None
        self._dragging = False
        self._forget_port_preview_items()
        self._measure_preview = []
        self._measure_items = []
        self._probe_items = []
        self.scene().clear()
        board = self.board
        self._index_hits(board)
        if board is None:
            self._port_anchor = None
            self._measure_anchor = None
            return
        self._draw_copper(board)
        self._draw_vias(board)
        self._draw_outline(board)
        self._draw_region()
        if self.mesh is not None and len(self.mesh.x) >= 2 and len(self.mesh.y) >= 2:
            self.scene().addItem(_MeshItem(self.mesh))
        self._draw_ports()
        self._draw_pdn_marks()
        self._replace_overlay()
        self._draw_measurement()
        self._draw_probe()
        if anchor is not None and self._two_click_mode:
            self._port_anchor = anchor
            self._show_port_preview(anchor, anchor)
        if measure_anchor is not None and self._tool == "measure":
            self._measure_anchor = measure_anchor
            self._show_measure_preview(measure_anchor, measure_anchor)

    def _shown(self, name: str) -> bool:
        return self.visible_layers is None or name in self.visible_layers

    def _draw_copper(self, board: Board) -> None:
        names = board.copper_names_top_to_bottom()
        order = list(reversed(names))
        for index, name in enumerate(order):
            if not self._shown(name):
                continue
            color = layer_color(name, index)
            # Top copper is last in the painter's order, so it sits above the layers under it.
            z_value = 10 + (len(names) - names.index(name))
            for poly in board.copper:
                if poly.layer != name or len(poly.points) < 3:
                    continue
                paint = QColor(color)
                if self.highlight_net and poly.net and poly.net != self.highlight_net:
                    paint.setAlpha(35)
                path = _path_from_points(poly.points, poly.holes)
                path.setFillRule(Qt.OddEvenFill)
                item = QGraphicsPathItem(path)
                item.setPen(QPen(QColor(0, 0, 0, 0), 0))
                item.setBrush(QBrush(paint))
                item.setZValue(z_value)
                item.setData(0, name)
                self.scene().addItem(item)

    def _draw_vias(self, board: Board) -> None:
        for via in board.vias:
            if self.highlight_net and via.net and via.net != self.highlight_net:
                continue
            radius = max(via.drill_mm / 2, 0.05)
            item = QGraphicsPathItem()
            path = QPainterPath()
            path.addEllipse(QPointF(via.x, via.y), radius, radius)
            item.setPath(path)
            item.setBrush(QBrush(QColor(20, 20, 20, 220)))
            item.setPen(QPen(QColor("#d0d0d0"), 0))
            item.setZValue(30)
            self.scene().addItem(item)

    def _draw_outline(self, board: Board) -> None:
        if not self.show_outline or not board.outline or len(board.outline) < 2:
            return
        path = QPainterPath()
        first, *rest = board.outline
        path.moveTo(first[0], first[1])
        for x, y in rest:
            path.lineTo(x, y)
        path.closeSubpath()
        item = QGraphicsPathItem(path)
        item.setPen(QPen(QColor("#d5d2a8"), 0))
        item.setBrush(QBrush(Qt.NoBrush))
        item.setZValue(35)
        self.scene().addItem(item)

    def _draw_region(self) -> None:
        points = self.region_points
        if not points or len(points) < 3:
            return
        path = _path_from_points(points)
        item = QGraphicsPathItem(path)
        pen = QPen(QColor("#f2f2f2"), 0, Qt.DashLine)
        item.setPen(pen)
        item.setBrush(QBrush(Qt.NoBrush))
        item.setZValue(36)
        self.scene().addItem(item)

    def _draw_ports(self) -> None:
        for index, port in enumerate(self.ports):
            x0, y0, _z0 = port.start
            x1, y1, _z1 = port.stop
            if port.marks:
                (mx0, my0), (mx1, my1) = port.marks
            else:
                mx0, my0, mx1, my1 = x0, y0, x1, y1
            chosen = self._selected == ("port", index)
            color = QColor("#ffd45a") if chosen else QColor("#d6ffe6")
            ink = QColor("#2a2108") if chosen else QColor("#06210f")
            identity = ("port", index)

            def tag(item, identity=identity, chosen=chosen) -> None:
                item.setData(0, "port")
                item.setData(1, identity)
                if chosen:
                    item.setData(2, "selected")
                _pass_clicks(item)

            if abs(x1 - x0) > 0.02 and abs(y1 - y0) > 0.02:
                sheet = QGraphicsRectItem(_scene_rect(x0, y0, x1, y1))
                sheet.setPen(_cosmetic(color, 2))
                sheet.setBrush(QBrush(QColor(255, 212, 90, 90) if chosen else QColor(125, 255, 179, 70)))
                sheet.setZValue(60)
                tag(sheet)
                self.scene().addItem(sheet)
            halo = QGraphicsLineItem(mx0, my0, mx1, my1)
            halo.setPen(_cosmetic(ink, 5))
            halo.setZValue(60)
            tag(halo)
            self.scene().addItem(halo)
            line = QGraphicsLineItem(mx0, my0, mx1, my1)
            line.setPen(_cosmetic(color, 3 if chosen else 2))
            line.setZValue(61)
            tag(line)
            self.scene().addItem(line)
            for x, y in ((mx0, my0), (mx1, my1)):
                dot = _screen_dot(x, y, 4.5, color, ink)
                dot.setZValue(62)
                tag(dot)
                self.scene().addItem(dot)
            label = QGraphicsSimpleTextItem(port.name if not port.net else f"{port.name}  {port.net}")
            label.setBrush(QBrush(color))
            label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            tag(label)
            label.setPos((mx0 + mx1) / 2, (my0 + my1) / 2)
            label.setZValue(63)
            self.scene().addItem(label)

    def _draw_pdn_marks(self) -> None:
        if self.vrm is not None:
            self._draw_terminal(self.vrm, f"{self.vrm.name}  {self.vrm.volts:g} V", QColor("#ffb020"), "vrm")
        for load in self.loads:
            self._draw_terminal(load, f"{load.name}  {load.amps:g} A", QColor("#7ec8ff"), "load")
        for part in self.parts:
            self._draw_part(part)

    def _draw_terminal(self, term, text: str, color: QColor, kind: str) -> None:
        x, y = float(term.x), float(term.y)
        gx = term.gx
        gy = term.gy
        if gx is not None and gy is not None and (abs(gx - x) > 1e-4 or abs(gy - y) > 1e-4):
            line = QGraphicsLineItem(x, y, float(gx), float(gy))
            line.setPen(_cosmetic(color, 2))
            line.setZValue(68)
            line.setData(0, kind)
            _pass_clicks(line)
            self.scene().addItem(line)
            ground = _screen_dot(float(gx), float(gy), 4.5, color, QColor("#1a1408"))
            ground.setZValue(69)
            ground.setData(0, kind)
            _pass_clicks(ground)
            self.scene().addItem(ground)
        dot = _screen_dot(x, y, 6.0, color, QColor("#1a1408"))
        dot.setZValue(70)
        dot.setData(0, kind)
        _pass_clicks(dot)
        self.scene().addItem(dot)
        label = QGraphicsSimpleTextItem(text)
        label.setBrush(QBrush(QColor("#fff6e0")))
        label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        _pass_clicks(label)
        label.setPos(x, y)
        label.setZValue(71)
        label.setData(0, kind)
        self.scene().addItem(label)

    def _draw_part(self, part) -> None:
        line = QGraphicsLineItem(part.ax, part.ay, part.bx, part.by)
        line.setPen(_cosmetic(QColor("#d0b0ff"), 2))
        line.setZValue(68)
        line.setData(0, "part")
        _pass_clicks(line)
        self.scene().addItem(line)
        for x, y in ((part.ax, part.ay), (part.bx, part.by)):
            dot = _screen_dot(x, y, 4.5, QColor("#d0b0ff"), QColor("#1a1024"))
            dot.setZValue(69)
            dot.setData(0, "part")
            _pass_clicks(dot)
            self.scene().addItem(dot)
        bits = [part.name]
        if part.r_ohm:
            bits.append(f"{part.r_ohm:g} Ω")
        if part.l_nh:
            bits.append(f"{part.l_nh:g} nH")
        if part.c_nf:
            bits.append(f"{part.c_nf:g} nF")
        label = QGraphicsSimpleTextItem("  ".join(bits))
        label.setBrush(QBrush(QColor("#f3e8ff")))
        label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        _pass_clicks(label)
        label.setPos((part.ax + part.bx) / 2, (part.ay + part.by) / 2)
        label.setZValue(71)
        label.setData(0, "part")
        self.scene().addItem(label)

    def _replace_overlay(self) -> None:
        scene = self.scene()
        for item in list(scene.items()):
            if isinstance(item, _HeatItem):
                scene.removeItem(item)
        if self._overlay_cells:
            scene.addItem(_HeatItem(self._overlay_cells))

    def _board_rect(self) -> QRectF | None:
        board = self.board
        if board is None:
            return None
        xs: list[float] = []
        ys: list[float] = []
        if board.outline:
            xs.extend(p[0] for p in board.outline)
            ys.extend(p[1] for p in board.outline)
        if not xs:
            for poly in board.copper:
                xs.extend(p[0] for p in poly.points)
                ys.extend(p[1] for p in poly.points)
        if not xs:
            return None
        return _scene_rect(min(xs), min(ys), max(xs), max(ys))

    def _model_pos(self, event) -> tuple[float, float]:
        point = self.mapToScene(event.position().toPoint())
        return (point.x(), point.y())

    def _index_hits(self, board: Board | None) -> None:
        self._copper_tree = None
        self._copper_geoms = []
        self._copper_meta = []
        self._pad_tree = None
        self._pad_geoms = []
        self._pad_items = []
        if board is None:
            return
        from shapely.strtree import STRtree

        geoms = []
        meta = []
        for poly in board.copper:
            geom = as_polygon(poly.points, poly.holes)
            if geom is None:
                continue
            geoms.append(geom)
            meta.append((poly.layer, poly.net, poly.kind))
        self._copper_geoms = geoms
        self._copper_meta = meta
        if geoms:
            self._copper_tree = STRtree(geoms)
        pad_geoms = []
        pads = []
        for pad in board.pads:
            geom = as_polygon(pad.points)
            if geom is None:
                continue
            pad_geoms.append(geom.buffer(0.08))
            pads.append(pad)
        self._pad_geoms = pad_geoms
        self._pad_items = pads
        if pad_geoms:
            self._pad_tree = STRtree(pad_geoms)

    def pad_at(self, x: float, y: float):
        if self._pad_tree is None:
            return None
        from shapely.geometry import Point

        point = Point(x, y)
        for index in self._pad_tree.query(point):
            index = int(index)
            if self._pad_geoms[index].covers(point):
                return self._pad_items[index]
        return None

    def _copper_under(self, x: float, y: float, skip_hidden: bool) -> tuple[str, str] | None:
        if self.board is None or self._copper_tree is None:
            return None
        from shapely.geometry import Point

        point = Point(x, y)
        names = self.board.copper_names_top_to_bottom()
        rank = {name: index for index, name in enumerate(names)}
        best: tuple[tuple[int, int], str, str] | None = None
        for index in self._copper_tree.query(point):
            index = int(index)
            layer, net, _kind = self._copper_meta[index]
            if skip_hidden and not self._shown(layer):
                continue
            if not self._copper_geoms[index].covers(point):
                continue
            key = (rank.get(layer, len(names)), index)
            if best is None or key < best[0]:
                best = (key, layer, net)
        if best is None:
            return None
        return best[1], best[2]

    def layer_at(self, x: float, y: float) -> str:
        if self.board is None:
            return ""
        found = self._copper_under(x, y, skip_hidden=True)
        if found is not None:
            return found[0]
        names = self.board.copper_names_top_to_bottom()
        return names[0] if names else ""

    def net_at(self, x: float, y: float) -> str:
        found = self._copper_under(x, y, skip_hidden=False)
        return found[1] if found is not None else ""

    def trace_net_at(self, x: float, y: float) -> str:
        """Net of a trace or pad under the point. A pour alone is not a trace."""
        if self.board is not None and self._copper_tree is not None:
            from shapely.geometry import Point

            point = Point(x, y)
            names = self.board.copper_names_top_to_bottom()
            rank = {name: index for index, name in enumerate(names)}
            best: tuple[tuple[int, int], str] | None = None
            for index in self._copper_tree.query(point):
                index = int(index)
                layer, net, kind = self._copper_meta[index]
                if kind == "zone" or not net:
                    continue
                if not self._copper_geoms[index].covers(point):
                    continue
                key = (rank.get(layer, len(names)), index)
                if best is None or key < best[0]:
                    best = (key, net)
            if best is not None:
                return best[1]
        pad = self.pad_at(x, y)
        if pad is not None and pad.net:
            return pad.net
        return ""

    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            self._pan = True
            self._pan_pos = event.position().toPoint()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._tool == "crop":
            self._dragging = True
            self._drag_start = self._model_pos(event)
            self._update_rubber(self._drag_start)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._pan and self._pan_pos is not None:
            pos = event.position().toPoint()
            delta = pos - self._pan_pos
            self._pan_pos = pos
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        model = self._model_pos(event)
        if self._dragging:
            self._update_rubber(model)
            event.accept()
            return
        if self._port_anchor is not None:
            self._show_port_preview(self._port_anchor, model)
        if self._tool == "measure" and self._measure_anchor is not None:
            self._show_measure_preview(self._measure_anchor, model)
            length = math.dist(self._measure_anchor, model)
            self.status_text.emit(f"{length:.2f} mm straight")
            event.accept()
            return
        x, y = model
        net = self.net_at(x, y)
        layer = self.layer_at(x, y) if net or self.board else ""
        if net or layer:
            self.status_text.emit(f"{layer}  {net or '(no net)'}   {x:.3f}, {y:.3f} mm")
        else:
            self.status_text.emit(f"{x:.3f}, {y:.3f} mm")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.RightButton and self._pan:
            self._pan = False
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._dragging:
            end = self._model_pos(event)
            x0, y0 = self._drag_start
            x1, y1 = end
            self._dragging = False
            if self._rubber is not None:
                self.scene().removeItem(self._rubber)
                self._rubber = None
            if abs(x1 - x0) > 0.05 and abs(y1 - y0) > 0.05:
                self.crop_rect.emit(x0, y0, x1, y1)
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._tool == "port":
            x, y = self._model_pos(event)
            if self._two_click_mode:
                # The cursor is the port. A pad under it must not replace the click with the pad center.
                # A failed second click keeps the first point. The handler clears it when the port is placed.
                if self._port_anchor is None:
                    anchor = (x, y)
                    self._port_anchor = anchor
                else:
                    anchor = self._port_anchor
                self._show_port_preview(anchor, (x, y))
                self.copper_clicked.emit(x, y, self.layer_at(x, y))
                event.accept()
                return
            pad = self.pad_at(x, y)
            if pad is not None:
                self.pad_clicked.emit(pad)
            else:
                self.copper_clicked.emit(x, y, self.layer_at(x, y))
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._tool in {"vrm", "load", "part"}:
            x, y = self._model_pos(event)
            pad = self.pad_at(x, y)
            if pad is not None:
                self.pad_clicked.emit(pad)
            else:
                self.copper_clicked.emit(x, y, self.layer_at(x, y))
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._tool == "measure":
            x, y = self._model_pos(event)
            if self._measure_anchor is None:
                # The finished line stays until the second click replaces it, or Delete removes it.
                self._measure_anchor = (x, y)
                self._show_measure_preview((x, y), (x, y))
            else:
                start = self._measure_anchor
                self._measure_anchor = None
                self._clear_measure_preview()
                self.measure_clicked.emit(start[0], start[1], x, y)
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._tool == "navigate":
            x, y = self._model_pos(event)
            hit = self.component_at(x, y)
            if hit is not None:
                self.component_picked.emit(hit)
                event.accept()
                return
            if self._selected is not None:
                self.component_picked.emit(None)
            net = self.net_at(x, y)
            if net:
                self.net_picked.emit(net)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _forget_port_preview_items(self) -> None:
        self._port_mark = None
        self._port_hot = None
        self._port_line = None
        self._port_halo = None
        self._port_sheet = None

    def _clear_port_preview(self) -> None:
        self._port_anchor = None
        for item in (self._port_mark, self._port_hot, self._port_line, self._port_halo, self._port_sheet):
            if item is not None and item.scene() is not None:
                item.scene().removeItem(item)
        self._forget_port_preview_items()

    def _show_port_preview(self, anchor: tuple[float, float], end: tuple[float, float]) -> None:
        if self._port_mark is None:
            self._port_mark = _screen_dot(anchor[0], anchor[1], 5.5, QColor("#ffd45a"), QColor("#2a2108"))
            self._port_mark.setZValue(82)
            self._port_mark.setData(0, "port-preview")
            _pass_clicks(self._port_mark)
            self.scene().addItem(self._port_mark)
            self._port_halo = QGraphicsLineItem()
            self._port_halo.setPen(_cosmetic(QColor("#1a1404"), 5))
            self._port_halo.setZValue(80)
            self._port_halo.setData(0, "port-preview")
            _pass_clicks(self._port_halo)
            self.scene().addItem(self._port_halo)
            self._port_line = QGraphicsLineItem()
            self._port_line.setPen(_cosmetic(QColor("#ffd45a"), 2))
            self._port_line.setZValue(81)
            self._port_line.setData(0, "port-preview-line")
            _pass_clicks(self._port_line)
            self.scene().addItem(self._port_line)
            self._port_hot = _screen_dot(end[0], end[1], 4.0, QColor(255, 212, 90, 50), QColor("#ffd45a"))
            self._port_hot.setZValue(82)
            self._port_hot.setData(0, "port-preview")
            _pass_clicks(self._port_hot)
            self.scene().addItem(self._port_hot)
            half = SHEET_MM / 2.0
            self._port_sheet = QGraphicsRectItem(_scene_rect(anchor[0] - half, anchor[1] - half, anchor[0] + half, anchor[1] + half))
            self._port_sheet.setPen(_cosmetic(QColor("#ffd45a"), 1))
            self._port_sheet.setBrush(QBrush(QColor(255, 212, 90, 70)))
            self._port_sheet.setZValue(79)
            self._port_sheet.setData(0, "port-preview")
            _pass_clicks(self._port_sheet)
            self.scene().addItem(self._port_sheet)
        self._port_mark.setPos(anchor[0], anchor[1])
        self._port_hot.setPos(end[0], end[1])
        self._port_halo.setLine(anchor[0], anchor[1], end[0], end[1])
        self._port_line.setLine(anchor[0], anchor[1], end[0], end[1])

    def selected(self) -> tuple | None:
        return self._selected

    def set_selected(self, which: tuple | None) -> None:
        if which == self._selected:
            return
        self._selected = which
        self._redraw_selection()

    def component_at(self, x: float, y: float) -> tuple | None:
        """The port or measurement under a click, or None. The pick is in screen pixels."""
        if self.viewport().width() < 2 or self.viewport().height() < 2:
            return None
        px, py = self._pixel_xy(x, y)
        best: tuple | None = None
        best_d = _PICK_PX
        for index, port in enumerate(self.ports):
            dist = self._port_pick_distance(x, y, px, py, port)
            if dist <= best_d:
                best = ("port", index)
                best_d = dist
        for index, (path, _label) in enumerate(self._measurements):
            if len(path) < 2:
                continue
            dist = self._path_pick_distance(px, py, path)
            if dist <= best_d:
                best = ("measure", index)
                best_d = dist
        return best

    def _pixel_xy(self, x: float, y: float) -> tuple[float, float]:
        point = self.mapFromScene(self.model_to_scene(x, y))
        return (float(point.x()), float(point.y()))

    def _port_pick_distance(self, x: float, y: float, px: float, py: float, port: PortBox) -> float:
        if port.marks:
            (ax, ay), (bx, by) = port.marks
        else:
            ax, ay = port.start[0], port.start[1]
            bx, by = port.stop[0], port.stop[1]
        dist = self._dist_segment(px, py, ax, ay, bx, by)
        x0, y0 = port.start[0], port.start[1]
        x1, y1 = port.stop[0], port.stop[1]
        if abs(x1 - x0) > 0.02 and abs(y1 - y0) > 0.02:
            left, right = min(x0, x1), max(x0, x1)
            top, bottom = min(y0, y1), max(y0, y1)
            if left <= x <= right and top <= y <= bottom:
                return 0.0
            dist = min(
                dist,
                self._dist_segment(px, py, left, top, right, top),
                self._dist_segment(px, py, right, top, right, bottom),
                self._dist_segment(px, py, right, bottom, left, bottom),
                self._dist_segment(px, py, left, bottom, left, top),
            )
        return dist

    def _path_pick_distance(self, px: float, py: float, path: list[tuple[float, float]]) -> float:
        dist = min(
            self._dist_segment(px, py, path[index][0], path[index][1], path[index + 1][0], path[index + 1][1])
            for index in range(len(path) - 1)
        )
        mid = path[len(path) // 2]
        anchor = self._pixel_xy(mid[0], mid[1])
        return min(dist, math.hypot(px - anchor[0], py - anchor[1]))

    def _dist_segment(self, px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
        a = self._pixel_xy(ax, ay)
        b = self._pixel_xy(bx, by)
        vx, vy = b[0] - a[0], b[1] - a[1]
        length2 = vx * vx + vy * vy
        if length2 < 1e-6:
            return math.hypot(px - a[0], py - a[1])
        t = ((px - a[0]) * vx + (py - a[1]) * vy) / length2
        t = min(max(t, 0.0), 1.0)
        return math.hypot(px - (a[0] + vx * t), py - (a[1] + vy * t))

    def _redraw_selection(self) -> None:
        if self.board is None:
            self._draw_measurement()
            return
        self.rebuild()

    def measurements(self) -> list[tuple[list[tuple[float, float]], str]]:
        return self._measurements

    def add_measurement(self, path: list[tuple[float, float]], label: str) -> None:
        self._measurements.append((list(path), label))
        self._selected = ("measure", len(self._measurements) - 1)
        self._redraw_selection()

    def clear_measurements(self) -> None:
        self._measurements = []
        if self._selected is not None and self._selected[0] == "measure":
            self._selected = None
        self._redraw_selection()

    def remove_measurement(self, index: int) -> None:
        if index < 0 or index >= len(self._measurements):
            return
        del self._measurements[index]
        selected = self._selected
        if selected is not None and selected[0] == "measure":
            chosen = int(selected[1])
            if chosen == index:
                self._selected = None
            elif chosen > index:
                self._selected = ("measure", chosen - 1)
        self._redraw_selection()

    def set_probe(self, marks: list[tuple[float, float, str]] | None) -> None:
        self._probe_marks = list(marks or [])
        self._draw_probe()

    def _draw_measurement(self) -> None:
        for item in self._measure_items:
            if item.scene() is not None:
                item.scene().removeItem(item)
        self._measure_items = []
        for index, (path, label) in enumerate(self._measurements):
            if len(path) < 2:
                continue
            painter = QPainterPath()
            painter.moveTo(path[0][0], path[0][1])
            for x, y in path[1:]:
                painter.lineTo(x, y)
            chosen = self._selected == ("measure", index)
            color = QColor("#ffd45a") if chosen else QColor("#f4f1e8")
            item = QGraphicsPathItem(painter)
            item.setPen(_cosmetic(color, 3 if chosen else 2))
            item.setZValue(84)
            item.setData(1, ("measure", index))
            if chosen:
                item.setData(2, "selected")
            _pass_clicks(item)
            self.scene().addItem(item)
            self._measure_items.append(item)
            if not label:
                continue
            mid = path[len(path) // 2]
            text = QGraphicsSimpleTextItem(label)
            text.setBrush(QBrush(color))
            text.setData(1, ("measure", index))
            text.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            text.setPos(mid[0], mid[1])
            text.setZValue(85)
            _pass_clicks(text)
            self.scene().addItem(text)
            self._measure_items.append(text)

    def _draw_probe(self) -> None:
        for item in self._probe_items:
            if item.scene() is not None:
                item.scene().removeItem(item)
        self._probe_items = []
        for x, y, caption in self._probe_marks:
            dot = _screen_dot(x, y, 6.0, QColor("#ffd45a"), QColor("#2a2108"))
            dot.setZValue(88)
            _pass_clicks(dot)
            self.scene().addItem(dot)
            self._probe_items.append(dot)
            text = QGraphicsSimpleTextItem(caption)
            text.setBrush(QBrush(QColor("#ffd45a")))
            text.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            text.setPos(x, y)
            text.setZValue(89)
            _pass_clicks(text)
            self.scene().addItem(text)
            self._probe_items.append(text)

    def _clear_measure_preview(self) -> None:
        for item in self._measure_preview:
            if item.scene() is not None:
                item.scene().removeItem(item)
        self._measure_preview = []

    def _show_measure_preview(self, anchor: tuple[float, float], end: tuple[float, float]) -> None:
        if not self._measure_preview:
            line = QGraphicsLineItem()
            line.setPen(_cosmetic(QColor("#f4f1e8"), 2))
            line.setZValue(84)
            _pass_clicks(line)
            self.scene().addItem(line)
            dot = _screen_dot(anchor[0], anchor[1], 4.5, QColor("#f4f1e8"), QColor("#20241f"))
            dot.setZValue(85)
            _pass_clicks(dot)
            self.scene().addItem(dot)
            self._measure_preview = [line, dot]
        self._measure_preview[0].setLine(anchor[0], anchor[1], end[0], end[1])
        self._measure_preview[1].setPos(anchor[0], anchor[1])

    def _update_rubber(self, end: tuple[float, float]) -> None:
        rect = _scene_rect(self._drag_start[0], self._drag_start[1], end[0], end[1])
        if self._rubber is None:
            self._rubber = QGraphicsRectItem(rect)
            self._rubber.setPen(QPen(QColor("#7dffb3"), 0))
            self._rubber.setBrush(QBrush(QColor(125, 255, 179, 40)))
            self._rubber.setZValue(70)
            self.scene().addItem(self._rubber)
        else:
            self._rubber.setRect(rect)
