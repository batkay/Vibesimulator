"""Top shelf: File, Place, Components, View, Board, Run, and Settings. One panel height pushes the board down."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from .flow_layout import FlowHost, FlowLayout
from .info_tip import NoteWithInfo, info_button, note_with_info, with_info


def assemble_shelf(window) -> None:
    """Build the shelf from controls the window already created, plus the PDN fields."""
    _add_pdn_fields(window)
    window._place_stack = QStackedWidget()
    for page in (
        _note_page(
            "Drag with the right button to pan. Scroll to zoom. "
            "Click a port or a measurement, then Delete removes it. Click copper to highlight its net."
        ),
        _port_page(window),
        _vrm_page(window),
        _load_page(window),
        _part_page(window),
        _note_page("Drag a rectangle on the board. Crop, whole outline, the margin, and Auto crop are on the Run tab."),
    ):
        window._place_stack.addWidget(page)

    place = QWidget()
    place.setMinimumWidth(0)
    place_layout = QVBoxLayout(place)
    place_layout.setContentsMargins(6, 6, 6, 6)
    place_layout.addWidget(_tool_row(window))
    place_layout.addWidget(window._place_stack)
    place_layout.addStretch(1)

    shelf = _Shelf()
    shelf.add_page(_file_page(window), "File")
    shelf.add_page(place, "Place")
    shelf.add_page(_components_page(window), "Components")
    shelf.add_page(_view_page(window), "View")
    shelf.add_page(_board_page(window), "Board")
    shelf.add_page(_run_page(window), "Run")
    shelf.add_page(_settings_page(window), "Settings")
    for index in range(shelf.count()):
        if shelf.tabText(index) == "Place":
            shelf.setCurrentIndex(index)
    shelf.remember_open_tab()
    window._shelf = shelf

    window.view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    window.results.setMinimumWidth(200)
    window.results.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    layers = _layers_side(window)
    window._layers_panel = layers
    splitter = QSplitter()
    splitter.addWidget(window.results)
    splitter.addWidget(window.view)
    splitter.addWidget(layers)
    splitter.setStretchFactor(0, 1)
    splitter.setStretchFactor(1, 3)
    splitter.setStretchFactor(2, 0)
    splitter.setChildrenCollapsible(False)
    splitter.splitterMoved.connect(window._on_splitter_moved)
    window._splitter = splitter

    central = QWidget()
    outer = QVBoxLayout(central)
    outer.setContentsMargins(4, 0, 4, 0)
    outer.setSpacing(2)
    outer.addWidget(shelf)
    outer.addWidget(splitter, 1)
    window.setCentralWidget(central)
    shelf.attach(central)
    window._place_stack.currentChanged.connect(lambda _index: shelf._place_panel())
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, window._log_dock())
    window._select_tool("navigate")


class _Shelf(QWidget):
    """Tab row plus one page. Every tab is the same height, and that height is in the layout."""

    def __init__(self) -> None:
        super().__init__()
        self._open = True
        self._shown = -1
        self._placing = False
        self._host: QWidget | None = None
        self._bar = QTabBar()
        self._bar.setDocumentMode(True)
        self._bar.setExpanding(False)
        self._bar.setDrawBase(True)
        self._bar.tabBarClicked.connect(self._on_tab_clicked)
        self._stack = QStackedWidget()
        self._panel = QFrame()
        self._panel.setObjectName("shelfPanel")
        self._panel.setFrameShape(QFrame.Shape.StyledPanel)
        self._panel.setAutoFillBackground(True)
        self._panel.setStyleSheet("QFrame#shelfPanel { background-color: palette(window); border: 1px solid palette(mid); }")
        self._scroll = QScrollArea()
        self._scroll.setObjectName("shelfScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setWidget(self._stack)
        # The stack's size hint is its tallest page. Ignored lets a short tab use the
        # ribbon height, and the minimum set in _fit_page_scroll is what makes Place scroll.
        self._stack.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        panel_layout = QVBoxLayout(self._panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.addWidget(self._scroll)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._bar)
        layout.addWidget(self._panel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def add_page(self, page: QWidget, title: str) -> None:
        page.setMinimumWidth(0)
        self._stack.addWidget(page)
        self._bar.addTab(title)

    def attach(self, host: QWidget) -> None:
        self._host = host
        self._place_panel()

    def count(self) -> int:
        return self._bar.count()

    def tabText(self, index: int) -> str:
        return self._bar.tabText(index)

    def currentIndex(self) -> int:
        return self._bar.currentIndex()

    def setCurrentIndex(self, index: int) -> None:
        """Switch pages without toggling the panel. A tab click is what opens and closes it."""
        self._bar.setCurrentIndex(index)
        self._stack.setCurrentIndex(index)
        self._place_panel()

    def widget(self, index: int) -> QWidget | None:
        return self._stack.widget(index)

    def tabBar(self) -> QTabBar:
        return self._bar

    def remember_open_tab(self) -> None:
        self._shown = self.currentIndex()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_panel()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._place_panel()

    def _on_tab_clicked(self, index: int) -> None:
        if index < 0:
            return
        self._open = not (index == self._shown and self._open)
        self._shown = index
        self._stack.setCurrentIndex(index)
        self._place_panel()

    def _place_panel(self) -> None:
        if self._placing:
            return
        self._placing = True
        try:
            self._place_panel_now()
        finally:
            self._placing = False

    def _place_panel_now(self) -> None:
        bar_h = max(self._bar.sizeHint().height(), self._bar.height(), 1)
        if not self._open:
            self._panel.hide()
            self._panel.setFixedHeight(0)
            self.setFixedHeight(bar_h)
            self._fit_page_scroll()
            return
        width = self.width()
        if width < 40 and self._host is not None:
            width = self._host.width() - 8
        if width < 40:
            return
        height = self._uniform_height(width)
        host = self._host
        if host is not None and host.height() > bar_h + 200:
            # Keep a strip of the board visible under the ribbon.
            height = min(height, max(120, host.height() - bar_h - 160))
        if self._panel.isVisible() and self._panel.height() == height and self.height() == bar_h + height:
            self._fit_page_scroll()
            return
        self._panel.setFixedHeight(height)
        self._panel.show()
        self.setFixedHeight(bar_h + height)
        self._fit_page_scroll()

    def _fit_page_scroll(self) -> None:
        """Let a tall Place page scroll inside the shared ribbon. Shorter tabs do not.

        The stack reports the tallest page as its size hint. A short tab is clamped
        to the ribbon so that hint does not grow a scrollbar. Place is left at its
        own height when that is taller than the ribbon.
        """
        if not self._open or self._panel.isHidden():
            if self._stack.minimumHeight() != 0:
                self._stack.setMinimumHeight(0)
            if self._stack.maximumHeight() != _QWIDGETSIZE_MAX:
                self._stack.setMaximumHeight(_QWIDGETSIZE_MAX)
            return
        view_w = self._scroll.viewport().width()
        if view_w < 40:
            view_w = max(1, self.width() - 24)
        if view_w < 40:
            return
        content = _content_height(self._stack.currentWidget(), view_w, every_page=False)
        view_h = self._scroll.viewport().height()
        if view_h < 20:
            view_h = max(0, self._panel.height() - 2 * self._panel.frameWidth() - 2)
        if content > view_h + 2:
            if self._stack.maximumHeight() != _QWIDGETSIZE_MAX:
                self._stack.setMaximumHeight(_QWIDGETSIZE_MAX)
            if self._stack.minimumHeight() != content:
                self._stack.setMinimumHeight(content)
            return
        if self._stack.minimumHeight() != 0:
            self._stack.setMinimumHeight(0)
        cap = max(view_h, 1)
        if self._stack.maximumHeight() != cap:
            self._stack.setMaximumHeight(cap)

    def _uniform_height(self, width: int) -> int:
        """One height for every tab, from the tallest tab other than Place.

        Place holds the tool pages. That page scrolls inside the panel, so it
        does not leave an empty band under the other tabs.
        """
        frame = self._panel.frameWidth()
        margins = self._panel.layout().contentsMargins()
        inner = max(1, width - margins.left() - margins.right() - 2 * frame)
        tallest = self._bar.height()
        for index in range(self._stack.count()):
            if self._bar.tabText(index) == "Place":
                continue
            page = self._stack.widget(index)
            if page is not None:
                page.ensurePolished()
            tallest = max(tallest, _content_height(page, inner, every_page=True))
        return tallest + margins.top() + margins.bottom() + 2 * frame + 8

    def sizeHint(self) -> QSize:
        bar = self._bar.sizeHint()
        extra = self._panel.height() if self._open and not self._panel.isHidden() else 0
        return QSize(max(bar.width(), 1), max(bar.height(), 1) + max(extra, 0))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


_QWIDGETSIZE_MAX = 16_777_215


def _content_height(widget: QWidget | None, width: int, every_page: bool = False) -> int:
    """Height of the visible controls at this width, with no extra stretch."""
    if widget is None or width <= 0:
        return 0
    if isinstance(widget, QStackedWidget):
        lay = widget.layout()
        margins = lay.contentsMargins() if lay is not None else widget.contentsMargins()
        inner = max(1, width - margins.left() - margins.right())
        if every_page and widget.count():
            body = max(_content_height(widget.widget(index), inner, True) for index in range(widget.count()))
        else:
            body = _content_height(widget.currentWidget(), inner, every_page)
        return body + margins.top() + margins.bottom()
    if isinstance(widget, (FlowHost, NoteWithInfo)) or (isinstance(widget, QLabel) and widget.wordWrap()):
        height = widget.heightForWidth(max(1, width))
        if height < 0:
            height = widget.sizeHint().height()
        return _clamp_widget(widget, height)
    if type(widget) is QWidget and widget.layout() is not None:
        return _clamp_widget(widget, _layout_height(widget.layout(), width, every_page))
    height = max(widget.sizeHint().height(), widget.minimumSizeHint().height())
    return _clamp_widget(widget, height)


def _clamp_widget(widget: QWidget, height: int) -> int:
    cap = widget.maximumHeight()
    if 0 < cap < _QWIDGETSIZE_MAX:
        height = min(height, cap)
    floor = widget.minimumHeight()
    if 0 < floor < _QWIDGETSIZE_MAX:
        height = max(height, min(floor, cap if 0 < cap < _QWIDGETSIZE_MAX else floor))
    return max(0, height)


def _layout_height(layout, width: int, every_page: bool = False) -> int:
    if isinstance(layout, FlowLayout):
        return layout.heightForWidth(width)
    margins = layout.contentsMargins()
    inner = max(1, width - margins.left() - margins.right())
    if isinstance(layout, QVBoxLayout):
        return _box_height(layout, inner, vertical=True, every_page=every_page) + margins.top() + margins.bottom()
    if isinstance(layout, QHBoxLayout):
        return _box_height(layout, inner, vertical=False, every_page=every_page) + margins.top() + margins.bottom()
    if layout.hasHeightForWidth():
        height = layout.heightForWidth(width)
        if height > 0:
            return height
    return max(0, layout.sizeHint().height())


def _box_height(layout, inner: int, vertical: bool, every_page: bool = False) -> int:
    space = _gap(layout, vertical)
    if vertical:
        heights = []
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item is None or _is_stretch(item):
                continue
            height = _item_height(item, inner, every_page)
            if height is None:
                continue
            heights.append(height)
        if not heights:
            return 0
        return sum(heights) + space * (len(heights) - 1)
    tallest = 0
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item is None or _is_stretch(item):
            continue
        hint_w = item.sizeHint().width()
        child_w = hint_w if hint_w > 1 else inner
        height = _item_height(item, child_w, every_page)
        if height is not None:
            tallest = max(tallest, height)
    return tallest


def _item_height(item, width: int, every_page: bool = False) -> int | None:
    widget = item.widget()
    if widget is not None:
        # A stack page that is not showing is hidden. The uniform ribbon still has to fit it.
        if widget.isHidden() and not every_page:
            return None
        return _content_height(widget, width, every_page)
    sub = item.layout()
    if sub is not None:
        return _layout_height(sub, width)
    return max(0, item.sizeHint().height())


def _is_stretch(item) -> bool:
    spacer = item.spacerItem()
    if spacer is None:
        return False
    return spacer.sizeHint().height() == 0 and bool(spacer.expandingDirections() & Qt.Orientation.Vertical)


def _gap(layout, vertical: bool) -> int:
    space = layout.spacing()
    if space >= 0:
        return space
    parent = layout.parentWidget()
    if parent is None:
        return 6
    metric = (
        QStyle.PixelMetric.PM_LayoutVerticalSpacing
        if vertical
        else QStyle.PixelMetric.PM_LayoutHorizontalSpacing
    )
    value = parent.style().pixelMetric(metric)
    return 6 if value < 0 else value


def _add_pdn_fields(window) -> None:
    window.vrm_v = _spin(0.0, 48.0, 3.3, 3, " V")
    window.vrm_r = _spin(0.0, 100.0, 0.0, 4, " Ω")
    window.vrm_l = _spin(0.0, 1000.0, 0.0, 3, " nH")
    window.vrm_list = _flex_list()
    window.load_i = _spin(0.0, 100.0, 0.1, 3, " A")
    window.load_list = _flex_list()
    window.part_name = QLineEdit()
    window.part_name.setPlaceholderText("name")
    window.part_name.setMinimumWidth(0)
    window.part_r = _spin(0.0, 1.0e7, 0.0, 3, " Ω")
    window.part_l = _spin(0.0, 1.0e6, 0.0, 3, " nH")
    window.part_c = _spin(0.0, 1.0e6, 0.0, 3, " nF")
    window.part_list = _flex_list()
    window.conductivity = QLineEdit("5.8e7")
    window.conductivity.setMinimumWidth(0)
    window.plating = _spin(0.001, 0.5, 0.025, 3, " mm")
    window.pitch = _spin(0.2, 5.0, 0.5, 2, " mm")
    window.power_net = window.reference.__class__()
    from pcb_simulator.app import _flex_combo

    _flex_combo(window.power_net)
    window.ac_power = window.reference.__class__()
    window.ac_ground = window.reference.__class__()
    _flex_combo(window.ac_power)
    _flex_combo(window.ac_ground)
    window.pdn_btn = QPushButton("PDN")
    window.pdn_btn.clicked.connect(window.run_pdn)
    window.vrm_v.valueChanged.connect(window._on_vrm_fields)
    window.vrm_r.valueChanged.connect(window._on_vrm_fields)
    window.vrm_l.valueChanged.connect(window._on_vrm_fields)
    window.load_i.valueChanged.connect(window._on_load_current)
    window.part_list.currentRowChanged.connect(window._on_part_selected)
    window.load_list.currentRowChanged.connect(window._on_load_selected)


def _spin(low: float, high: float, value: float, decimals: int, suffix: str) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(low, high)
    box.setDecimals(decimals)
    box.setValue(value)
    box.setSuffix(suffix)
    box.setMinimumWidth(0)
    return box


def _flex_list() -> QListWidget:
    from pcb_simulator.app import _clip_list

    widget = QListWidget()
    widget.setMinimumHeight(80)
    _clip_list(widget)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return widget


def _tool_row(window) -> FlowHost:
    window._tool_group = QButtonGroup(window)
    window._tool_group.setExclusive(True)
    window._tool_buttons = {}
    host = FlowHost()
    flow = host.layout()
    for label, tool in (
        ("Pan / select", "navigate"),
        ("Port", "port"),
        ("VRM", "vrm"),
        ("Load", "load"),
        ("Part", "part"),
        ("Draw crop", "crop"),
    ):
        button = QPushButton(label)
        button.setCheckable(True)
        button.clicked.connect(lambda _checked=False, name=tool: window._set_tool(name))
        window._tool_group.addButton(button)
        window._tool_buttons[tool] = button
        flow.addWidget(button)
    clear = QPushButton("Clear crop")
    clear.setToolTip("Remove the crop rectangle. Crop and whole outline are on the Run tab.")
    clear.clicked.connect(window._clear_rect)
    flow.addWidget(clear)
    return host


def _port_page(window) -> QWidget:
    host = FlowHost()
    flow = host.layout()
    click = QPushButton("Click a pad")
    click.clicked.connect(lambda: window._set_tool("port"))
    flow.addWidget(_row_button(click, info_button(
        "Click a pad",
        "The first click places the port on that copper. The second click names the reference net for that port. "
        "If that net is already under the first click, the sheet stays there. Otherwise it runs toward the click. "
        "The sheet is 0.2 mm across, and narrower when a neighbor is closer than that. "
        "The list opens from Components.",
    )))
    two = QPushButton("Two clicks")
    two.clicked.connect(window._arm_two_click)
    flow.addWidget(_row_button(two, info_button(
        "Two clicks",
        "The first click places the port on that one net. The sheet is 0.2 mm across, and narrower when a neighbor is closer than that. "
        "The second click names the reference net. If that net is already under the first click, the sheet stays there. "
        "Otherwise it runs toward the click. It reaches that copper and no other net.",
    )))
    every = QPushButton("Port every pad")
    every.clicked.connect(window.port_every_pad)
    flow.addWidget(_row_button(every, info_button(
        "Port every pad",
        "Puts a vertical port on every pad that is not the reference net. "
        "That reference is the one from the last port you placed, or the board ground net before that. Each port is its own solve.",
    )))
    page = QWidget()
    page.setMinimumWidth(0)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(note_with_info(
        _fit("The first click is the signal. The second click names the reference. Components lists the ports."),
        "Place a port",
        "The first click places the port on that copper. The second click names the reference net. "
        "Resistance, delete, and differential pairs open from the Components tab.",
    ))
    layout.addWidget(host)
    return page


def _components_page(window) -> QWidget:
    """One button per placed kind. Each opens that list in its own window."""
    window._ports_dialog = _component_dialog(window, "Ports", _ports_body(window), 560, 480)
    window._pairs_dialog = _component_dialog(window, "Pairs", _pairs_body(window), 520, 400)
    window._vrm_dialog = _component_dialog(
        window, "VRM", _delete_list(window.vrm_list, "Delete VRM", window._delete_vrm), 480, 360
    )
    window._load_dialog = _component_dialog(
        window, "Load", _delete_list(window.load_list, "Delete load", window._delete_load), 480, 360
    )
    window._part_dialog = _component_dialog(
        window, "Part", _delete_list(window.part_list, "Delete part", window._delete_part), 480, 360
    )
    window._component_dialogs = (
        window._ports_dialog,
        window._pairs_dialog,
        window._vrm_dialog,
        window._load_dialog,
        window._part_dialog,
    )
    page = QWidget()
    page.setMinimumWidth(0)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(6, 6, 6, 6)
    layout.addWidget(note_with_info(
        _fit("Each button opens the list of that component. Place still puts them on the board."),
        "Components",
        "Ports, differential pairs, the VRM, loads, and parts each open in their own window. "
        "Select a row to edit it. Delete removes that one. "
        "Voltage, current, and part values are typed on Place before the click.",
    ))
    buttons = FlowHost()
    for label, dialog, title, text in (
        (
            "Ports…",
            window._ports_dialog,
            "Port list",
            "Every port you placed. Select one to change its resistance or delete it. "
            "With none selected, Port R is the resistance of the next port.",
        ),
        (
            "Pairs…",
            window._pairs_dialog,
            "Pair list",
            "Differential pairs of two ports. Adding a pair uses the S matrix already computed and does not start a new solve.",
        ),
        (
            "VRM…",
            window._vrm_dialog,
            "VRM list",
            "The VRM you placed. Delete removes it. Voltage, series R, and series L are typed on Place.",
        ),
        (
            "Load…",
            window._load_dialog,
            "Load list",
            "Every load. Select one and its current shows on Place. Delete removes that load.",
        ),
        (
            "Part…",
            window._part_dialog,
            "Part list",
            "Every RLC part. Select one and its name, R, L, and C show on Place. Delete removes that part.",
        ),
    ):
        button = QPushButton(label)
        button.clicked.connect(lambda _checked=False, item=dialog: _show_side_dialog(item))
        buttons.layout().addWidget(_row_button(button, info_button(title, text)))
    layout.addWidget(buttons)
    layout.addStretch(1)
    return page


def _ports_body(window) -> QWidget:
    body = QWidget()
    body.setMinimumWidth(0)
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    fields = FlowHost()
    fields.layout().addWidget(_flow_field("Port R", with_info(
        window.port_r,
        "Port R",
        "Termination of the lumped port, the same role as a VNA or TDR. It is not the trace impedance. "
        "With a port selected, this changes that port. With none selected, the next port you place uses this value.",
    )))
    delete = QPushButton("Delete port")
    delete.clicked.connect(window._delete_port)
    fields.layout().addWidget(delete)
    layout.addWidget(fields)
    layout.addWidget(window.port_list, 1)
    return body


def _pairs_body(window) -> QWidget:
    body = QWidget()
    body.setMinimumWidth(0)
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    pairs = FlowHost()
    pairs.layout().addWidget(window.pair_pos)
    pairs.layout().addWidget(with_info(
        window.pair_neg,
        "Differential pair",
        "Positive leg, then negative leg. This uses the S matrix already computed and does not rerun the solver.",
    ))
    add_pair = QPushButton("Add pair")
    add_pair.setToolTip("Add the differential pair selected above. This does not rerun the solver.")
    add_pair.clicked.connect(window._on_add_pair)
    delete_pair = QPushButton("Delete pair")
    delete_pair.clicked.connect(window._delete_pair)
    pairs.layout().addWidget(add_pair)
    pairs.layout().addWidget(delete_pair)
    layout.addWidget(pairs)
    layout.addWidget(window.pair_list, 1)
    return body


def _delete_list(widget: QListWidget, label: str, slot) -> QWidget:
    body = QWidget()
    body.setMinimumWidth(0)
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    button = QPushButton(label)
    button.clicked.connect(slot)
    layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
    layout.addWidget(widget, 1)
    return body


def _component_dialog(window, title: str, body: QWidget, width: int, height: int) -> QDialog:
    for widget in body.findChildren(QListWidget):
        widget.setMinimumHeight(80)
        widget.setMaximumHeight(_QWIDGETSIZE_MAX)
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return _side_dialog(window, title, body, width, height)


def _vrm_page(window) -> QWidget:
    host = FlowHost()
    flow = host.layout()
    flow.addWidget(_flow_field("Voltage", with_info(
        window.vrm_v,
        "VRM voltage",
        "Held between the power click and the ground click. Type the margined output, not the name of the rail. "
        "The ground click is held at 0 V.",
    )))
    flow.addWidget(_flow_field("Series R", with_info(
        window.vrm_r,
        "VRM R",
        "Series resistance of the VRM in the AC sweep. Zero resistance and zero inductance is a short. The DC solve still holds the ideal voltage.",
    )))
    flow.addWidget(_flow_field("Series L", with_info(
        window.vrm_l,
        "VRM L",
        "Series inductance of the VRM in the AC sweep. It sets the slope of |Z| below the capacitor resonance.",
    )))
    page = QWidget()
    page.setMinimumWidth(0)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(note_with_info(
        _fit("Click the power pad, then the ground pad. Type the voltage first, including margin."),
        "VRM",
        "Click the power pad, then the ground pad or ground copper, the same way a two-click port picks both ends. "
        "The voltage is held between those clicks. A second VRM replaces the first. The S-parameter run ignores it. "
        "The list opens from Components.",
    ))
    layout.addWidget(host)
    return page


def _load_page(window) -> QWidget:
    host = FlowHost()
    host.layout().addWidget(_flow_field("Current", with_info(
        window.load_i,
        "Load current",
        "DC current that leaves the power pad and returns at the ground click. A VRM alone has no current, so its drop is zero.",
    )))
    page = QWidget()
    page.setMinimumWidth(0)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(note_with_info(
        _fit("Click the power pad, then the ground pad. The current returns at the second click."),
        "Load",
        "Click the pad where the current leaves, then the ground pad or ground copper. "
        "The two pads do not have to sit on top of each other. The S-parameter run ignores the load. "
        "The list opens from Components.",
    ))
    layout.addWidget(host)
    return page


def _part_page(window) -> QWidget:
    host = FlowHost()
    flow = host.layout()
    flow.addWidget(_flow_field("Name", window.part_name))
    flow.addWidget(_flow_field("R", with_info(
        window.part_r,
        "Part R",
        "Ohms. Zero leaves the resistor out. A capacitor's R is ESR and does not conduct at DC. A series resistor is R only.",
    )))
    flow.addWidget(_flow_field("L", with_info(
        window.part_l,
        "Part L",
        "Nanohenries. Zero leaves the inductor out. On a decap this is the mounting inductance.",
    )))
    flow.addWidget(_flow_field("C", with_info(
        window.part_c,
        "Part C",
        "Nanofarads. Zero leaves the capacitor out. A part with C is open at DC. "
        "The part does not add or remove copper, and a KiCad value string is not used.",
    )))
    page = QWidget()
    page.setMinimumWidth(0)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(note_with_info(
        _fit("A two-pad footprint places across both pads in one click. Any other footprint takes two clicks."),
        "Part",
        "A two-pad footprint, such as R1 pads 1 and 2, places the part across those pads in one click. "
        "Anything else takes two clicks. Blank or zero leaves that element out. "
        "Where the pads are already joined by a pour, the part sits in parallel. The S-parameter run ignores it. "
        "The list opens from Components.",
    ))
    layout.addWidget(host)
    return page


def _view_page(window) -> QWidget:
    window._nets_dialog = _side_dialog(window, "Nets", window.net_list, 320, 480)
    page = QWidget()
    page.setMinimumWidth(0)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(6, 6, 6, 6)
    layout.addWidget(note_with_info(
        _fit("Layers is on the right. Recenter fits the board. Measure reads a length. Nets… chooses what is simulated."),
        "Checked nets",
        "Checked nets are simulated. Each port's reference net is always kept. With nothing checked, every net in the region is kept. "
        "Layers lists every copper layer. Uncheck one to hide it on the board.",
    ))
    row = QHBoxLayout()
    layers = QPushButton("Layers")
    layers.setCheckable(True)
    layers.setChecked(True)
    layers.clicked.connect(lambda checked=False: window._toggle_layers_panel(bool(checked)))
    window._layers_button = layers
    row.addWidget(layers)
    row.addWidget(info_button(
        "Layers",
        "Opens the layer list on the right of the board. Uncheck a layer to hide it. Edge.Cuts is the board outline.",
    ))
    recenter = QPushButton("Recenter")
    recenter.clicked.connect(window.view.zoom_fit)
    row.addWidget(recenter)
    row.addWidget(info_button(
        "Recenter",
        "Fits the whole board in the window and puts it back in the center. The zoom you had is replaced by that fit.",
    ))
    measure = QPushButton("Measure")
    measure.setCheckable(True)
    measure.clicked.connect(lambda _checked=False: window._set_tool("measure"))
    window._tool_group.addButton(measure)
    window._tool_buttons["measure"] = measure
    row.addWidget(measure)
    row.addWidget(info_button(
        "Measure",
        "Click two points. On a trace, the length follows the copper. On a pour, or on a port's reference net, it is the straight line. "
        "Each line stays. Delete removes the selected one. Pan / select, then click, picks one later.",
    ))
    nets = QPushButton("Nets…")
    nets.clicked.connect(lambda: _show_side_dialog(window._nets_dialog))
    row.addWidget(nets)
    row.addWidget(info_button(
        "Nets",
        "Checked nets are simulated. Each port's reference net is always kept. With nothing checked, every net in the region is kept.",
    ))
    row.addStretch(1)
    layout.addLayout(row)
    layout.addStretch(1)
    return page


def _board_page(window) -> QWidget:
    window._stack_dialog = _side_dialog(window, "Stackup", window.stack_table, 640, 420)
    page = QWidget()
    page.setMinimumWidth(0)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(6, 6, 6, 6)
    layout.addWidget(note_with_info(
        _fit("Copper thickness is stored. The S-parameter mesh does not use it. The PDN grid does. A port's reference is its second click."),
        "Stackup",
        "Copper thickness is stored and is not meshed, so the field solve misses sidewall capacitance. "
        "The PDN grid uses that thickness as the foil. εr and dielectric height set the plane capacitance. "
        "tanδ is the loss tangent at 10 GHz. The job stores Debye poles from 1 MHz to 200 GHz. "
        "The field solve uses them when this openEMS has the Debye updater. Otherwise the layer stays lossless. "
        "Blank or 0 is lossless either way. "
        "Each port's reference is the second click on Place, and that net stays in the crop.",
    ))
    stack = QPushButton("Stackup…")
    stack.clicked.connect(lambda: _show_side_dialog(window._stack_dialog))
    layout.addWidget(stack, 0, Qt.AlignmentFlag.AlignLeft)
    layout.addStretch(1)
    return page


def _show_dialog(dialog: QDialog) -> None:
    _fit_dialog(dialog)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


def _show_side_dialog(dialog: QDialog) -> None:
    """Show a list dialog at the size it was given. Do not shrink it to a short ribbon."""
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


def _side_dialog(window, title: str, body: QWidget, width: int, height: int) -> QDialog:
    body.setMaximumHeight(_QWIDGETSIZE_MAX)
    body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    dialog = QDialog(window)
    dialog.setWindowTitle(title)
    dialog.setModal(False)
    layout = QVBoxLayout(dialog)
    layout.addWidget(body, 1)
    close = QPushButton("Close")
    close.clicked.connect(dialog.hide)
    layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
    dialog.resize(width, height)
    return dialog


def _layers_side(window) -> QWidget:
    """Layer checks beside the board. Shown until View → Layers is turned off."""
    panel = QWidget()
    panel.setObjectName("layersSide")
    panel.setMinimumWidth(160)
    outer = QVBoxLayout(panel)
    outer.setContentsMargins(8, 8, 8, 8)
    outer.setSpacing(4)
    title = QLabel("Layers")
    outer.addWidget(title)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    host = QWidget()
    host.setMinimumWidth(0)
    window.layer_box = QVBoxLayout(host)
    window.layer_box.setContentsMargins(0, 0, 0, 0)
    scroll.setWidget(host)
    outer.addWidget(scroll, 1)
    return panel


def _dialog(window, title: str, body: QWidget, run_button: QPushButton, width: int = 520) -> QDialog:
    dialog = QDialog(window)
    dialog.setWindowTitle(title)
    dialog.setModal(False)
    layout = QVBoxLayout(dialog)
    layout.addWidget(body)
    row = QHBoxLayout()
    row.addWidget(run_button, 1)
    cancel = QPushButton("Cancel")
    cancel.setEnabled(window.cancel_btn.isEnabled())
    cancel.clicked.connect(lambda _checked=False: window.cancel_simulation())
    window._extra_cancels.append(cancel)
    row.addWidget(cancel)
    close = QPushButton("Close")
    close.clicked.connect(dialog.hide)
    row.addWidget(close)
    layout.addLayout(row)
    dialog._fit_body = body
    dialog._fit_width = width
    _fit_dialog(dialog)
    return dialog


def _fit_dialog(dialog: QDialog) -> None:
    """Size a run dialog to its controls at the width it will actually use."""
    body = getattr(dialog, "_fit_body", None)
    width = getattr(dialog, "_fit_width", 0)
    layout = dialog.layout()
    if body is None or layout is None or width < 40:
        return
    body.ensurePolished()
    margins = layout.contentsMargins()
    inner = max(1, width - margins.left() - margins.right())
    content = _content_height(body, inner)
    row = layout.itemAt(layout.count() - 1)
    row_h = _layout_height(row.layout(), inner) if row is not None and row.layout() is not None else 0
    height = content + row_h + _gap(layout, True) + margins.top() + margins.bottom() + 6
    parent = dialog.parentWidget()
    if parent is not None and parent.height() > 240:
        height = min(height, parent.height() - 48)
    dialog.resize(width, max(height, 120))


def _s_dialog(window) -> QDialog:
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    solve = QFormLayout()
    solve.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
    solve.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    solve.addRow(_fit("f max"), with_info(
        window.f_max,
        "f max",
        "Highest frequency of the source and of the plots. The TDR rise scales as 1 / f max, "
        "so 10 GHz is about half the edge of 5 GHz. On the ohms plot the 10–90% time is about 0.23 ns at 5 GHz, "
        "about 20 mm on the distance axis at εeff 2.75. The v(t) tab shows the Gaussian source. "
        "Integrating that pulse gives a step of about 0.17 ns at 5 GHz. "
        "The launch edge stays a few samples wide at any f max, because the rise time shrinks along with the sample spacing. "
        "Samples along the rest of the trace get closer as f max goes up. "
        "The mesh does not change. Open space stays at a tenth of a wavelength at 5 GHz. "
        "tanδ on the stackup is the value at 10 GHz. The field solve uses Debye poles for it when this openEMS has that updater.",
    ))
    solve.addRow(_fit("Frequency points"), with_info(
        window.n_freq,
        "Frequency points",
        "How many frequencies S is sampled at after the timestep loop. This barely changes the solve time. "
        "The TDR record is about this count divided by f max long, so fewer points hide a long line. "
        "The rise time follows f max.",
    ))
    solve.addRow(_fit("Cells / feature"), with_info(
        window.cells_per_feature,
        "Cells / feature",
        "Copper lattice, 0.4 mm divided by this count, and never finer than 0.05 mm. "
        "1 is 0.4 mm. 2 is 0.2 mm. 4 is 0.1 mm. Open space stays at a tenth of a wavelength at 5 GHz.",
    ))
    solve.addRow(with_info(
        window.fine_mesh,
        "Fine mesh",
        "Uses a 0.1 mm copper pitch, which is cells / feature 4. "
        "Off leaves the 0.2 mm signal mesh. "
        "The finer pitch is a longer run: the cells are smaller and each timestep is shorter. "
        "It is for a fast machine and a small crop. It does not turn on the old exact-edge mesh.",
    ))
    layout.addLayout(solve)
    layout.addWidget(_fit(
        "The solver Python is on the Settings tab. openEMS is not bundled, and this app does not download it. "
        "Boundary, Stop, and Port nets are on the Run tab."
    ))
    layout.addWidget(window.preflight)
    preview = QPushButton("Preview mesh")
    preview.clicked.connect(window.preview_mesh)
    layout.addWidget(preview)
    layout.addWidget(info_button(
        "S parameters",
        "Runs the FDTD solve and fills the S matrix. Placed VRMs, loads, and parts are ignored. Ctrl+R starts this run.",
    ), 0, Qt.AlignmentFlag.AlignLeft)
    return _dialog(window, "S parameters", body, window.run_btn)


def _pdn_dialog(window) -> QDialog:
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    fields = FlowHost()
    flow = fields.layout()
    flow.addWidget(_flow_field("Conductivity", with_info(
        window.conductivity,
        "Conductivity",
        "Siemens per metre for every copper cell. The default is 5.8e7, annealed copper. "
        "It is uniform, and there is no temperature rise.",
    )))
    flow.addWidget(_flow_field("Via plating", with_info(
        window.plating,
        "Via plating",
        "Plating thickness used for via resistance. The area is π × plating × (drill − plating), and the length is the distance between the sheets.",
    )))
    flow.addWidget(_flow_field("Grid pitch", with_info(
        window.pitch,
        "Grid pitch",
        "Cell size of the DC sheet grid, floored at 0.2 mm. A cell is metal when its square meets that net's copper, "
        "so a trace thinner than the pitch still occupies a cell. This is not the FDTD lattice.",
    )))
    flow.addWidget(_flow_field("Power net", with_info(
        window.power_net,
        "Power net",
        "The rail for the DC drop and the AC sweep. The return is the ground click. "
        "When more than one name looks like a rail, this stays blank until you choose.",
    )))
    flow.addWidget(_flow_field("AC power layer", with_info(
        window.ac_power,
        "AC power layer",
        "The AC sweep uses this one power layer. Auto picks the power layer with the most copper. Other layers stay in the DC drop only.",
    )))
    flow.addWidget(_flow_field("AC ground layer", with_info(
        window.ac_ground,
        "AC ground layer",
        "The AC sweep uses this one reference layer. Auto picks the reference layer nearest the power layer.",
    )))
    flow.addWidget(_flow_field("f max", with_info(
        window.pdn_f_max,
        "PDN f max",
        "Highest frequency of the field impedance. The curve starts at this value divided by the frequency points. "
        "It does not change the copper pitch. The S-parameter f max is separate.",
    )))
    flow.addWidget(_flow_field("Frequency points", with_info(
        window.pdn_n_freq,
        "PDN frequency points",
        "How many frequencies the field curve is sampled at. This does not add timesteps. "
        "The first point is f max divided by this count.",
    )))
    flow.addWidget(with_info(
        window.pdn_signal_mesh,
        "Signal mesh",
        "Runs the planes on the 0.2 mm signal mesh, with four cells through each dielectric and a 0.05 mm floor. "
        "Off uses the 1 mm plane mesh and two cells through each dielectric. "
        "The signal mesh on a full pour is a long run. One load is one full solve.",
    ))
    layout.addWidget(fields)
    layout.addWidget(_fit(
        "The drop table is the sheet solve at 0 Hz. The impedance curve is an openEMS run of the same copper. "
        "Energy limit, max timesteps, and threads are on the Run tab. "
        "The voltage stays on the VRM clicks. Vias use the drill radius. This does not delete the S-parameter results."
    ))
    layout.addWidget(info_button(
        "PDN",
        "Solves the DC drop on the sheet grid, then starts the field run when no S-parameter solve is active. "
        "The field curve starts one step above DC. Each load is its own excitation. "
        "A missing copper path names the load. Above about 200,000 nodes the sheet solve warns and stops.",
    ), 0, Qt.AlignmentFlag.AlignLeft)
    return _dialog(window, "PDN", body, window.pdn_btn, width=720)


def _region_box(window) -> QWidget:
    """Crop controls share one parent so the two radios stay exclusive."""
    host = QWidget()
    host.setMinimumWidth(0)
    outer = QVBoxLayout(host)
    outer.setContentsMargins(0, 0, 0, 0)
    radios = QWidget()
    radios.setMinimumWidth(0)
    row = QHBoxLayout(radios)
    row.setContentsMargins(0, 0, 0, 0)
    row.addLayout(_beside_layout(window.mode_crop, info_button(
        "Crop",
        "The mesh is the rectangle you draw on Place, plus the margin, plus an air border. "
        "This is the usual way to keep a run small. Auto crop sets that rectangle from the port nets. "
        "The S-parameter run and the PDN field run use this same region.",
    )))
    row.addLayout(_beside_layout(window.mode_outline, info_button(
        "Whole outline",
        "Meshes the whole board outline. With no rectangle and no checked nets, crop mode does this too.",
    )))
    outer.addWidget(radios)
    outer.addWidget(_labeled("Margin", with_info(
        window.margin,
        "Margin",
        "Extra board kept around the crop, clipped to the outline. "
        "It holds the ground plane and dielectric next to the trace. "
        "The absorbing boundary sits farther out than this.",
    )))
    auto = QPushButton("Auto crop")
    auto.clicked.connect(window._auto_crop)
    outer.addWidget(_row_button(auto, info_button(
        "Auto crop",
        "Sets the crop to the copper of the nets you placed ports on, plus the margin. "
        "Each port's reference net is left out of the box, because its pour is the whole board. That pour is still kept inside the box. "
        "With no ports, the checked nets are used. A port on another part of the board widens the box. "
        "Other copper inside the box stays.",
    )), 0, Qt.AlignmentFlag.AlignLeft)
    return host


def _run_page(window) -> QWidget:
    window._s_dialog = _s_dialog(window)
    window._pdn_dialog = _pdn_dialog(window)
    page = QWidget()
    page.setMinimumWidth(0)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(6, 6, 6, 6)
    layout.addWidget(_region_box(window))
    buttons = FlowHost()
    s_open = QPushButton("S parameters…")
    s_open.clicked.connect(lambda: _show_dialog(window._s_dialog))
    pdn_open = QPushButton("PDN…")
    pdn_open.clicked.connect(lambda: _show_dialog(window._pdn_dialog))
    results = QPushButton("Results")
    results.setCheckable(True)
    results.setChecked(True)
    results.clicked.connect(lambda checked=False: window._toggle_results_panel(bool(checked)))
    window._results_button = results
    buttons.layout().addWidget(s_open)
    buttons.layout().addWidget(pdn_open)
    buttons.layout().addWidget(window.cancel_btn)
    buttons.layout().addWidget(_row_button(results, info_button(
        "Results",
        "Shows or hides the plots on the left of the board. Hiding them leaves the curves loaded.",
    )))
    layout.addWidget(buttons)
    shared = FlowHost()
    shared.layout().addWidget(_flow_field("Energy", with_info(
        window.end_criteria,
        "Energy limit",
        "Stop when the energy left in the volume has fallen by this factor. "
        "A larger number (1e-3) stops sooner and cuts off late reflections. 1e-4 keeps the tail. "
        "The S-parameter run and the PDN field run share this.",
    )))
    shared.layout().addWidget(_flow_field("Max timesteps", with_info(
        window.nr_ts,
        "Max timesteps",
        "Hard cap on steps. Lowering it does not make a step faster. "
        "A run that hits the cap is marked as not decayed, and late reflections are missing. "
        "Crossing can stop the S-parameter run sooner. This number stays the ceiling. "
        "The S-parameter run and the PDN field run share this.",
    )))
    shared.layout().addWidget(_flow_field("Threads", with_info(
        window.threads,
        "Threads",
        "auto uses every core for the timestep loop. The loop often levels off around 4–8 cores. "
        "The mesh and the result stay the same. Both runs share this.",
    )))
    shared.layout().addWidget(_flow_field("Boundary", with_info(
        window.boundary,
        "Boundary",
        "MUR is two air cells on each side of the board. PML is eight absorber cells plus those two air cells. "
        "The S-parameter default is MUR, which shortens the air box. The PDN field run uses PML.",
    )))
    shared.layout().addWidget(_flow_field("Stop", with_info(
        window.stop_mode,
        "Stop",
        "Crossing stops the S-parameter run after the wave has gone to the end of the longest trace and come back, then waits a short time so the open at that end can rise, plus the length of the source pulse. "
        "It stays at or under Max timesteps. The energy limit can stop a run that has already died out. "
        "Energy uses Max timesteps as the only step cap. The PDN field run uses Energy. "
        "Hitting Max timesteps before that return is marked as not decayed.",
    )))
    shared.layout().addWidget(with_info(
        window.signal_only,
        "Port nets",
        "With no nets checked, the S-parameter run keeps the nets you placed ports on and each port's reference. "
        "Reference vias farther than 1.5 mm from that signal copper are left out, which keeps stitching vias off the copper lattice. "
        "With no rectangle, the region follows the port nets plus the margin. Nets you check are kept. "
        "The PDN run uses the power net and its reference.",
    ))
    layout.addWidget(shared)
    layout.addWidget(_fit(
        "Crop and whole outline apply to both runs. Draw the rectangle on Place. "
        "Boundary, Stop, and Port nets apply to S parameters. The PDN field run keeps PML, the energy limit, and the typed step cap. "
        "S parameters… and PDN… open the settings for that run. Cancel is in that window and on this tab."
    ))
    layout.addStretch(1)
    return page

def _file_page(window) -> QWidget:
    host = FlowHost()
    flow = host.layout()
    for action in window._file_actions:
        button = QPushButton(action.text())
        button.setMinimumWidth(0)
        button.clicked.connect(lambda _checked=False, item=action: item.trigger())
        flow.addWidget(button)
    page = QWidget()
    page.setMinimumWidth(0)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(6, 6, 6, 6)
    window.file_save_state = _fit(
        "Open a board or a project. Save writes the .pcbsim.json beside the board. The KiCad file is not modified."
    )
    layout.addWidget(window.file_save_state)
    layout.addWidget(host)
    layout.addStretch(1)
    return page


def _settings_page(window) -> QWidget:
    button = QPushButton(window._solver_action.text())
    button.setMinimumWidth(0)
    button.clicked.connect(lambda _checked=False: window._solver_action.trigger())
    page = QWidget()
    page.setMinimumWidth(0)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(6, 6, 6, 6)
    layout.addWidget(_fit(
        "Solver Python is a setting for this computer. "
        "Leave it empty to use the interpreter that launched the app. "
        "The dialog also holds the cell-count warnings. openEMS is not bundled."
    ))
    layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
    layout.addStretch(1)
    return page


def _note_page(text: str) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.addWidget(_fit(text))
    layout.addStretch(1)
    return page


def _flow_field(text: str, control: QWidget) -> QWidget:
    """Labeled editor in a wrapping row.

    The wrapping row sizes each field from its size hint. An Ignored policy
    drops the editor out of that hint, so the box is given no width.
    """
    control.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    return _labeled(text, control)


def _labeled(text: str, control) -> QWidget:
    host = QWidget()
    host.setMinimumWidth(0)
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)
    label = QLabel(text)
    row.addWidget(label)
    if isinstance(control, QWidget):
        row.addWidget(control, 1)
    return host


def _row_button(button: QPushButton, info: QPushButton) -> QWidget:
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(2)
    row.addWidget(button)
    row.addWidget(info)
    return host


def _beside_layout(control, button) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(2)
    control.setMinimumWidth(0)
    row.addWidget(control, 1)
    row.addWidget(button)
    return row


def _fit(text: str):
    from pcb_simulator.app import FitLabel

    return FitLabel(text)
