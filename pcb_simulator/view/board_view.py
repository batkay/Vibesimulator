"""Board view. Scene axes match the KiCad file and the default canvas: +X right, +Y down."""

from __future__ import annotations

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

_COLORS = {
    "F.Cu": "#FF4D4D",
    "In1.Cu": "#FFD24D",
    "In2.Cu": "#4DFF6A",
    "In3.Cu": "#4DFFFF",
    "In4.Cu": "#C44DFF",
    "B.Cu": "#4D7DFF",
}
_FALLBACK = ("#FF9A4D", "#B8FF4D", "#4DFFD2", "#4DA6FF", "#FF4DA6", "#E6E64D")


def layer_color(name: str, index: int) -> QColor:
    color = QColor(_COLORS.get(name, _FALLBACK[index % len(_FALLBACK)]))
    color.setAlpha(175)
    return color


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
    """Tools: navigate (hover and highlight), crop (drag a rectangle), port (click)."""

    status_text = Signal(str)
    crop_rect = Signal(float, float, float, float)
    pad_clicked = Signal(object)
    copper_clicked = Signal(float, float, str)
    net_picked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setBackgroundBrush(QColor("#20241f"))
        self.setMouseTracking(True)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setDragMode(QGraphicsView.NoDrag)
        self.board: Board | None = None
        self.ports: list[PortBox] = []
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
        self._copper_tree = None
        self._copper_geoms: list = []
        self._copper_meta: list[tuple[str, str]] = []
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
        self.setCursor(Qt.CrossCursor if tool in {"crop", "port"} else Qt.ArrowCursor)

    def set_two_click(self, enabled: bool) -> None:
        """Two-click placement draws the first point, then a line that follows the cursor."""
        self._two_click_mode = enabled
        self._clear_port_preview()

    def set_board(self, board: Board) -> None:
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
        self._rubber = None
        self._dragging = False
        self._forget_port_preview_items()
        self.scene().clear()
        board = self.board
        self._index_hits(board)
        if board is None:
            self._port_anchor = None
            return
        self._draw_copper(board)
        self._draw_vias(board)
        self._draw_outline(board)
        self._draw_region()
        if self.mesh is not None and len(self.mesh.x) >= 2 and len(self.mesh.y) >= 2:
            self.scene().addItem(_MeshItem(self.mesh))
        self._draw_ports()
        if anchor is not None and self._two_click_mode:
            self._port_anchor = anchor
            self._show_port_preview(anchor, anchor)

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
        for port in self.ports:
            x0, y0, _z0 = port.start
            x1, y1, _z1 = port.stop
            if port.marks:
                (mx0, my0), (mx1, my1) = port.marks
            else:
                mx0, my0, mx1, my1 = x0, y0, x1, y1
            if abs(x1 - x0) > 0.02 and abs(y1 - y0) > 0.02:
                sheet = QGraphicsRectItem(_scene_rect(x0, y0, x1, y1))
                sheet.setPen(_cosmetic(QColor("#d6ffe6"), 2))
                sheet.setBrush(QBrush(QColor(125, 255, 179, 70)))
                sheet.setZValue(60)
                sheet.setData(0, "port")
                _pass_clicks(sheet)
                self.scene().addItem(sheet)
            halo = QGraphicsLineItem(mx0, my0, mx1, my1)
            halo.setPen(_cosmetic(QColor("#06210f"), 5))
            halo.setZValue(60)
            halo.setData(0, "port")
            _pass_clicks(halo)
            self.scene().addItem(halo)
            line = QGraphicsLineItem(mx0, my0, mx1, my1)
            line.setPen(_cosmetic(QColor("#d6ffe6"), 2))
            line.setZValue(61)
            line.setData(0, "port")
            _pass_clicks(line)
            self.scene().addItem(line)
            for x, y in ((mx0, my0), (mx1, my1)):
                dot = _screen_dot(x, y, 4.5, QColor("#7dffb3"), QColor("#06210f"))
                dot.setZValue(62)
                dot.setData(0, "port")
                _pass_clicks(dot)
                self.scene().addItem(dot)
            label = QGraphicsSimpleTextItem(port.name if not port.net else f"{port.name}  {port.net}")
            label.setBrush(QBrush(QColor("#e8ffe8")))
            label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            _pass_clicks(label)
            label.setPos((mx0 + mx1) / 2, (my0 + my1) / 2)
            label.setZValue(63)
            self.scene().addItem(label)

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
            meta.append((poly.layer, poly.net))
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
            layer, net = self._copper_meta[index]
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

    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton:
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
        x, y = model
        net = self.net_at(x, y)
        layer = self.layer_at(x, y) if net or self.board else ""
        if net or layer:
            self.status_text.emit(f"{layer}  {net or '(no net)'}   {x:.3f}, {y:.3f} mm")
        else:
            self.status_text.emit(f"{x:.3f}, {y:.3f} mm")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton and self._pan:
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
                if self._port_anchor is None:
                    self._port_anchor = (x, y)
                    self._show_port_preview((x, y), (x, y))
                else:
                    self._clear_port_preview()
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
        if event.button() == Qt.LeftButton and self._tool == "navigate":
            x, y = self._model_pos(event)
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

    def _clear_port_preview(self) -> None:
        self._port_anchor = None
        for item in (self._port_mark, self._port_hot, self._port_line, self._port_halo):
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
        self._port_mark.setPos(anchor[0], anchor[1])
        self._port_hot.setPos(end[0], end[1])
        self._port_halo.setLine(anchor[0], anchor[1], end[0], end[1])
        self._port_line.setLine(anchor[0], anchor[1], end[0], end[1])

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
