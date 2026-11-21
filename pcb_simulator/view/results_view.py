"""S, Z, and TDR plots. Renormalizing the display does not rerun the solver."""

from __future__ import annotations

import matplotlib

matplotlib.use("QtAgg")

from weakref import WeakKeyDictionary

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.ticker import AutoLocator, LogLocator, MaxNLocator, NullLocator
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .info_tip import info_button
from ..calc.differential import differential_s, mixed_mode_networks
from ..calc.network import network, renormalize
from ..calc.s_parameters import curve_label
from ..calc.tdr import default_eps_eff, distance_m, tdr_impedance, tdr_ohm_limits, tdr_view_window
from ..calc.z_parameters import z_matrix

DECAY_NOTICE = (
    "The field did not decay to the energy limit. "
    "The late part of the TDR is unfinished, so the plot ends once the trace is past."
)

_HOVER_PX = 16


class _ChartWindow(QDialog):
    """Non-modal window that holds the chart until it is docked again."""

    def __init__(self, view: "ResultsView"):
        super().__init__(view)
        self._view = view
        self._closing = False
        self.setObjectName("chartWindow")
        self.setModal(False)
        self.setWindowTitle("Chart")
        self.setMinimumSize(640, 480)
        self.resize(900, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.host = QVBoxLayout()
        layout.addLayout(self.host, 1)

    def closeEvent(self, event) -> None:
        # The window close puts the chart back. hide() does not re-enter this.
        if not self._closing:
            self._closing = True
            try:
                self._view._dock_chart()
            finally:
                self._closing = False
        event.accept()


class ResultsView(QWidget):
    display_z0_changed = Signal(float)
    eps_eff_changed = Signal(float)
    export_csv_requested = Signal()
    export_pdn_requested = Signal()
    overlay_changed = Signal(str)
    tdr_probe = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw = None
        self._names: list[str] = []
        self._pairs: list = []
        self._excitations: list[dict] = []
        self._decayed = True
        self._show_s: set[tuple[int, int]] = set()
        self._show_diff: set[str] = {"Sdd", "Scc"}
        self._pdn = None
        self._show_pdn: set[str] = {"parts", "bare", "target"}
        self._pdn_load = ""
        self._si_banner = ""
        self._pdn_banner = ""
        self.decay_notice = ""
        self.figure = Figure(figsize=(5.2, 4.0), layout="none")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumHeight(160)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        self._popped = False
        self._hover_text = ""
        self._hover_key = None
        self._hover_marker = None
        self._probe_token = None
        self._press = None
        self._pins = {}
        self.canvas.mpl_connect("motion_notify_event", self._on_hover)
        self.canvas.mpl_connect("figure_leave_event", self._leave_hover)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        home = self.toolbar._actions.get("home")
        if home is not None:
            home.setToolTip("Restore the view from when this chart was drawn.")
        zoom = self.toolbar._actions.get("zoom")
        if zoom is not None:
            zoom.setToolTip(
                "Drag a rectangle on the chart to zoom in. The axes then fit the samples in that box. "
                "Home puts that view back. Right-drag zooms out while this button is on."
            )
        self._hook_toolbar_zoom()
        self._clear_points = QPushButton("Clear points")
        self._clear_points.setObjectName("clearPoints")
        self._clear_points.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._clear_points.setToolTip("Remove every point clicked onto this chart.")
        self._clear_points.clicked.connect(self._clear_pins)
        self.toolbar.addWidget(self._clear_points)
        self._pop_button = QPushButton("Pop out")
        self._pop_button.setObjectName("popChart")
        self._pop_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._pop_button.setToolTip("Open this chart in its own window. Dock, or close that window, puts it back.")
        self._pop_button.clicked.connect(self._toggle_chart)
        self._chart_bar = QHBoxLayout()
        self._chart_bar.setContentsMargins(0, 0, 0, 0)
        self._chart_bar.addWidget(self.toolbar, 1)
        self._chart_bar.addWidget(self._pop_button)
        self._chart_note = QLabel("The chart is in its own window.")
        self._chart_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._chart_note.setWordWrap(True)
        self._chart_note.hide()
        self._chart = _ChartWindow(self)
        self.tabs = QTabBar()
        for title in ("Magnitude", "Phase", "Smith", "Z", "TDR", "v(t)", "PDN"):
            self.tabs.addTab(title)
        self.tabs.currentChanged.connect(self.redraw)
        self.port = QComboBox()
        self.port.currentIndexChanged.connect(self._on_port_changed)
        self.z0 = QDoubleSpinBox()
        self.z0.setRange(1.0, 500.0)
        self.z0.setDecimals(1)
        self.z0.setValue(50.0)
        self.z0.setSuffix(" Ω")
        self.z0.setToolTip(
            "Display reference for the single-ended plots. Differential curves use twice this value. "
            "Changing it renormalizes the plots and does not rerun the field solver."
        )
        self.z0.valueChanged.connect(self._on_z0)
        self.eps = QDoubleSpinBox()
        self.eps.setRange(1.0, 30.0)
        self.eps.setDecimals(3)
        self.eps.setValue(default_eps_eff(4.5))
        self.eps.setToolTip(
            "Effective permittivity for the TDR distance axis and for the mark on the board. "
            "Both are a constant-velocity estimate. Hover a TDR sample to put that distance on the trace."
        )
        self.eps.valueChanged.connect(self._on_eps)
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("color: #8a5a00;")
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port"))
        port_row.addWidget(self.port, 1)
        scale_row = QVBoxLayout()
        scale_row.setContentsMargins(0, 0, 0, 0)
        scale_row.setSpacing(2)
        z_row = QHBoxLayout()
        z_row.setContentsMargins(0, 0, 0, 0)
        z_row.setSpacing(2)
        z_row.addWidget(QLabel("Display Z0"))
        z_row.addWidget(self.z0, 1)
        z_row.addWidget(
            info_button(
                "Display Z0",
                "Redraws the plots from the saved S matrix. Differential curves use twice this value. "
                "It does not rerun the solver.",
            )
        )
        e_row = QHBoxLayout()
        e_row.setContentsMargins(0, 0, 0, 0)
        e_row.setSpacing(2)
        e_row.addWidget(QLabel("εeff"))
        e_row.addWidget(self.eps, 1)
        e_row.addWidget(
            info_button(
                "εeff",
                "Scales the TDR distance axis only. Ohms and S stay as simulated.",
            )
        )
        scale_row.addLayout(z_row)
        scale_row.addLayout(e_row)
        self.terms = QListWidget()
        self.terms.setObjectName("termList")
        self.terms.setMinimumHeight(36)
        self.terms.setMaximumHeight(72)
        self.terms.setMinimumWidth(0)
        self.terms.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.terms.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.terms.itemChanged.connect(self._on_term)
        term_row = QHBoxLayout()
        term_row.setContentsMargins(0, 0, 0, 0)
        all_btn = QPushButton("All")
        all_btn.setToolTip("Show every S parameter.")
        all_btn.clicked.connect(self._check_all)
        refl_btn = QPushButton("Reflections")
        refl_btn.setToolTip("Show S11, S22, and the other reflections.")
        refl_btn.clicked.connect(self._check_reflections)
        csv_btn = QPushButton("CSV")
        csv_btn.setToolTip("Save every S, Z, TDR, and port waveform.")
        csv_btn.clicked.connect(self.export_csv_requested.emit)
        term_row.addWidget(all_btn)
        term_row.addWidget(refl_btn)
        term_row.addWidget(csv_btn)
        term_row.addWidget(
            info_button(
                "Curves",
                "S11 is the reflection at port 1. S21 is the wave leaving port 2 when port 1 is driven. "
                "Reflections start checked. All turns the rest on. Nothing here reruns the solver. "
                "CSV writes every S, Z, TDR, and port waveform.",
            )
        )
        term_row.addStretch(1)
        self._si_controls = QWidget()
        si_layout = QVBoxLayout(self._si_controls)
        si_layout.setContentsMargins(0, 0, 0, 0)
        si_layout.addLayout(port_row)
        si_layout.addLayout(scale_row)
        si_layout.addWidget(self.terms)
        si_layout.addLayout(term_row)
        self._build_pdn_controls()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.banner)
        layout.addWidget(self.tabs)
        layout.addWidget(self._si_controls)
        layout.addWidget(self._pdn_controls)
        layout.addLayout(self._chart_bar)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self._chart_note, 1)
        self._pdn_controls.hide()
        self._say("Run a simulation to plot S, Z, and TDR.")

    def _build_pdn_controls(self) -> None:
        self.pdn_table = QTableWidget(0, 4)
        self.pdn_table.setHorizontalHeaderLabels(["Load", "Loop mV", "Power mV", "Ground mV"])
        header = self.pdn_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(24)
        self.pdn_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pdn_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.pdn_table.setMinimumHeight(48)
        self.pdn_table.setMaximumHeight(96)
        self.pdn_table.setMinimumWidth(0)
        self.pdn_load = QComboBox()
        self.pdn_load.currentTextChanged.connect(self._on_pdn_load)
        self.pdn_terms = QListWidget()
        self.pdn_terms.setMinimumHeight(0)
        self.pdn_terms.setMaximumHeight(72)
        self.pdn_terms.setMinimumWidth(0)
        self.pdn_terms.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pdn_terms.itemChanged.connect(self._on_pdn_term)
        self.pdn_terms.hide()
        self._overlay_voltage = QPushButton("Voltage")
        self._overlay_voltage.setCheckable(True)
        self._overlay_voltage.clicked.connect(lambda: self._set_overlay("voltage"))
        self._overlay_density = QPushButton("Current density")
        self._overlay_density.setCheckable(True)
        self._overlay_density.clicked.connect(lambda: self._set_overlay("density"))
        self._overlay_off = QPushButton("Overlay off")
        self._overlay_off.setCheckable(True)
        self._overlay_off.setChecked(True)
        self._overlay_off.clicked.connect(lambda: self._set_overlay(""))
        pdn_csv = QPushButton("CSV")
        pdn_csv.setToolTip("Save the drop table and every impedance curve.")
        pdn_csv.clicked.connect(self.export_pdn_requested.emit)
        load_row = QHBoxLayout()
        load_row.addWidget(QLabel("Load"))
        load_row.addWidget(self.pdn_load, 1)
        curve_row = QHBoxLayout()
        curve_row.addWidget(pdn_csv)
        curve_row.addWidget(
            info_button(
                "PDN curves",
                "The curve is the field impedance, from the first FDTD frequency up to the PDN f max. "
                "The table is the sheet solve at 0 Hz, with the typed voltage held at the VRM clicks. "
                "CSV writes the sheet sweep and the field curve.",
            )
        )
        curve_row.addStretch(1)
        overlay_row = QHBoxLayout()
        overlay_row.addWidget(self._overlay_voltage)
        overlay_row.addWidget(self._overlay_density)
        overlay_row.addWidget(self._overlay_off)
        overlay_row.addWidget(
            info_button(
                "PDN overlay",
                "Voltage colors the power-net cells. Red is the lower voltage. "
                "Current density is |I| / (thickness × pitch) on each sheet resistor. "
                "The S-parameter plots are the other tabs.",
            )
        )
        overlay_row.addStretch(1)
        self._pdn_controls = QWidget()
        layout = QVBoxLayout(self._pdn_controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.pdn_table)
        layout.addLayout(load_row)
        layout.addWidget(self.pdn_terms)
        layout.addLayout(curve_row)
        layout.addLayout(overlay_row)

    def show_results(self, results: dict, eps_eff: float) -> None:
        self._raw = network(results["freq"], results["S"], results["z0"])
        self._names = list(results["port_names"])
        self._excitations = list(results.get("excitations") or [])
        self._decayed = bool(results.get("decayed", True))
        count = self._raw.s.shape[1]
        self._show_s = {(i, i) for i in range(count)}
        self._show_diff = {"Sdd", "Scc"}
        self._rebuild_choices()
        self._fill_terms()
        self.eps.blockSignals(True)
        self.eps.setValue(float(eps_eff))
        self.eps.blockSignals(False)
        z0 = np.asarray(results["z0"], dtype=float).reshape(-1)
        if z0.size:
            self.z0.blockSignals(True)
            self.z0.setValue(float(np.real(z0[0])))
            self.z0.blockSignals(False)
        self._si_banner = ""
        self.decay_notice = "" if self._decayed else DECAY_NOTICE
        self.redraw()

    def set_pairs(self, pairs) -> None:
        """Pairs are DiffPair values. Adding one does not require a new solve."""
        self._pairs = list(pairs)
        self._rebuild_choices()
        self._fill_terms()
        if self._raw is not None:
            self.redraw()

    def _rebuild_choices(self) -> None:
        previous = self.port.currentData()
        self.port.blockSignals(True)
        self.port.clear()
        if self._names:
            for index, name in enumerate(self._names):
                self.port.addItem(name, ("se", index))
        elif not self._pairs:
            self.port.addItem("P1", ("se", 0))
        for index, pair in enumerate(self._pairs):
            self.port.addItem(f"{pair.name}  {pair.positive}/{pair.negative}", ("diff", index))
        if previous is not None:
            for index in range(self.port.count()):
                if self.port.itemData(index) == previous:
                    self.port.setCurrentIndex(index)
                    break
        self.port.blockSignals(False)

    def _current_choice(self) -> tuple[str, int]:
        data = self.port.currentData()
        if isinstance(data, tuple) and len(data) == 2:
            return str(data[0]), int(data[1])
        return "se", max(self.port.currentIndex(), 0)

    def _resolved_pair(self, index: int):
        if index < 0 or index >= len(self._pairs):
            return None
        pair = self._pairs[index]
        if pair.positive not in self._names or pair.negative not in self._names:
            return None
        return pair, self._names.index(pair.positive), self._names.index(pair.negative)

    def clear(self) -> None:
        self._raw = None
        self._excitations = []
        self._show_s = set()
        self._pdn = None
        self._si_banner = ""
        self._pdn_banner = ""
        self.decay_notice = ""
        self.banner.setText("")
        self.terms.blockSignals(True)
        self.terms.clear()
        self.terms.blockSignals(False)
        self.pdn_table.setRowCount(0)
        self.pdn_terms.blockSignals(True)
        self.pdn_terms.clear()
        self.pdn_terms.blockSignals(False)
        self.pdn_terms.hide()
        self._say("Run a simulation to plot S, Z, and TDR.")

    def show_pdn(self, result, switch: bool = True) -> None:
        self._pdn = result
        self._show_pdn = set()
        if result.z_target is not None:
            self._show_pdn.add("target")
        self._fill_pdn_table()
        self._fill_pdn_terms()
        notes = list(result.warnings)
        if not result.ok and result.message:
            notes.insert(0, result.message)
        self._pdn_banner = " ".join(notes)
        if switch:
            for index in range(self.tabs.count()):
                if self.tabs.tabText(index) == "PDN":
                    self.tabs.setCurrentIndex(index)
                    break
            self._set_overlay("voltage")
        self.redraw()

    def _fill_pdn_table(self) -> None:
        drops = [] if self._pdn is None else self._pdn.drops
        self.pdn_table.setRowCount(len(drops))
        for row, drop in enumerate(drops):
            values = [
                str(drop["name"]),
                _mv(drop.get("loop_mv")),
                _mv(drop.get("power_mv")),
                _mv(drop.get("ground_mv")),
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.pdn_table.setItem(row, column, item)
        names = list(self._pdn.impedance) if self._pdn is not None else []
        previous = self._pdn_load or (names[0] if names else "")
        self.pdn_load.blockSignals(True)
        self.pdn_load.clear()
        self.pdn_load.addItems(names)
        if previous in names:
            self.pdn_load.setCurrentText(previous)
        self._pdn_load = self.pdn_load.currentText()
        self.pdn_load.blockSignals(False)

    def _fill_pdn_terms(self) -> None:
        rows = []
        if self._pdn is not None and self._pdn.z_target is not None:
            rows.append(("target", "Target"))
        self.pdn_terms.blockSignals(True)
        self.pdn_terms.clear()
        for key, text in rows:
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setData(Qt.UserRole, key)
            item.setCheckState(Qt.Checked if key in self._show_pdn else Qt.Unchecked)
            self.pdn_terms.addItem(item)
        self.pdn_terms.blockSignals(False)
        self.pdn_terms.setVisible(bool(rows))

    def _on_pdn_load(self, name: str) -> None:
        self._pdn_load = name
        if self._tab_title() == "PDN":
            self.redraw()

    def _on_pdn_term(self, item: QListWidgetItem) -> None:
        key = str(item.data(Qt.UserRole))
        if item.checkState() == Qt.Checked:
            self._show_pdn.add(key)
        else:
            self._show_pdn.discard(key)
        if self._tab_title() == "PDN":
            self.redraw()

    def _set_overlay(self, mode: str) -> None:
        self._overlay_voltage.setChecked(mode == "voltage")
        self._overlay_density.setChecked(mode == "density")
        self._overlay_off.setChecked(mode == "")
        self.overlay_changed.emit(mode)

    def _tab_title(self) -> str:
        if self.tabs.count() == 0:
            return ""
        return self.tabs.tabText(self.tabs.currentIndex())

    def _apply_chrome(self, title: str) -> None:
        pdn = title == "PDN"
        self._si_controls.setVisible(not pdn)
        self._pdn_controls.setVisible(pdn)
        self.banner.setText(self._pdn_banner if pdn else self._si_banner)
        if self._popped:
            self._chart.setWindowTitle(title or "Chart")

    def _on_port_changed(self, *_args) -> None:
        self._fill_terms()
        self.redraw()

    def _fill_terms(self) -> None:
        self.terms.blockSignals(True)
        self.terms.clear()
        kind, _which = self._current_choice()
        if self._raw is not None and kind == "diff":
            for key, text in (("Sdd", "Sdd  differential reflection"), ("Scc", "Scc  common-mode reflection")):
                self._add_term(text, key, key in self._show_diff)
        elif self._raw is not None:
            count = self._raw.s.shape[1]
            for j in range(count):
                for i in range(count):
                    self._add_term(curve_label(i, j, self._names), (i, j), (i, j) in self._show_s)
        self.terms.blockSignals(False)

    def _add_term(self, text: str, key, checked: bool) -> None:
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setData(Qt.UserRole, key)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        if isinstance(key, tuple):
            into = self._names[key[0]] if key[0] < len(self._names) else f"P{key[0] + 1}"
            driven = self._names[key[1]] if key[1] < len(self._names) else f"P{key[1] + 1}"
            if key[0] == key[1]:
                item.setToolTip(f"S{key[0] + 1}{key[1] + 1} is the reflection at {into}.")
            else:
                item.setToolTip(f"S{key[0] + 1}{key[1] + 1} is the wave leaving {into} when {driven} is driven.")
        self.terms.addItem(item)

    def _on_term(self, item: QListWidgetItem) -> None:
        key = item.data(Qt.UserRole)
        checked = item.checkState() == Qt.Checked
        if isinstance(key, tuple):
            if checked:
                self._show_s.add((int(key[0]), int(key[1])))
            else:
                self._show_s.discard((int(key[0]), int(key[1])))
        elif checked:
            self._show_diff.add(str(key))
        else:
            self._show_diff.discard(str(key))
        self.redraw()

    def _check_all(self) -> None:
        kind, _which = self._current_choice()
        if kind == "diff":
            self._show_diff = {"Sdd", "Scc"}
        elif self._raw is not None:
            count = self._raw.s.shape[1]
            self._show_s = {(i, j) for i in range(count) for j in range(count)}
        self._fill_terms()
        self.redraw()

    def _check_reflections(self) -> None:
        kind, _which = self._current_choice()
        if kind == "diff":
            self._show_diff = {"Sdd"}
        elif self._raw is not None:
            count = self._raw.s.shape[1]
            self._show_s = {(i, i) for i in range(count)}
        self._fill_terms()
        self.redraw()

    def _want(self, i: int, j: int) -> bool:
        return (i, j) in self._show_s

    def _want_diff(self, name: str) -> bool:
        return name in self._show_diff

    def simulated_network(self):
        """Network at the port impedance the solver used. Touchstone export uses this."""
        return self._raw

    def displayed_network(self):
        if self._raw is None:
            return None
        target = float(self.z0.value())
        current = float(np.real(np.asarray(self._raw.z0).reshape(-1)[0]))
        if abs(target - current) < 1e-6:
            return self._raw
        return renormalize(self._raw, target)

    def _on_z0(self, _value: float) -> None:
        self.display_z0_changed.emit(float(self.z0.value()))
        self.redraw()

    def _on_eps(self, _value: float) -> None:
        self.eps_eff_changed.emit(float(self.eps.value()))
        if self.tabs.tabText(self.tabs.currentIndex()) == "TDR":
            self.redraw()

    def _engine(self, labeled: bool) -> None:
        """Use constrained layout only when the canvas is tall enough for axis labels.

        A short figure makes constrained layout collapse the axes and warn.
        """
        use = labeled and self.canvas.height() >= 120
        self.figure.set_layout_engine("constrained" if use else "none")

    def _toggle_chart(self, *_args) -> None:
        if self._popped:
            self._dock_chart()
        else:
            self._pop_chart()

    def _pop_chart(self) -> None:
        if self._popped:
            return
        self._popped = True
        self._chart.setWindowTitle(self._tab_title() or "Chart")
        self.layout().removeWidget(self.canvas)
        self._chart_bar.removeWidget(self.toolbar)
        self._chart.host.addWidget(self.toolbar)
        self._chart.host.addWidget(self.canvas, 1)
        self.toolbar.show()
        self.canvas.show()
        self._chart_note.show()
        self._pop_button.setText("Dock")
        self._pop_button.setToolTip("Put the chart back in the results pane.")
        self._chart.show()
        self._chart.raise_()
        self.canvas.draw_idle()

    def _dock_chart(self) -> None:
        chart = getattr(self, "_chart", None)
        if chart is None:
            return
        if not self._popped:
            chart.hide()
            return
        self._popped = False
        chart.host.removeWidget(self.toolbar)
        chart.host.removeWidget(self.canvas)
        self._chart_bar.insertWidget(0, self.toolbar, 1)
        layout = self.layout()
        note_at = layout.indexOf(self._chart_note)
        if note_at < 0:
            layout.addWidget(self.canvas, 1)
        else:
            layout.insertWidget(note_at, self.canvas, 1)
        self._chart_note.hide()
        self._pop_button.setText("Pop out")
        self._pop_button.setToolTip("Open this chart in its own window. Dock, or close that window, puts it back.")
        self.toolbar.show()
        self.canvas.show()
        chart.hide()
        self.canvas.draw_idle()

    def _data(self, ax, *args, **kwargs):
        """Plot one curve and mark it so hover can find it. Grid lines stay unmarked."""
        line, = ax.plot(*args, **kwargs)
        line.set_gid("data")
        return line

    def _drop_hover(self) -> None:
        self._hover_text = ""
        self._hover_key = None
        self._hover_marker = None
        QToolTip.hideText()
        self._sync_probe(None)

    def _sync_probe(self, time_ns: float | None) -> None:
        token = None
        if time_ns is not None and self._tab_title() == "TDR":
            token = round(float(time_ns), 6)
        if token == self._probe_token:
            return
        self._probe_token = token
        if token is None:
            self.tdr_probe.emit(None)
            return
        choice, index = self._current_choice()
        self.tdr_probe.emit({"time_s": float(time_ns) * 1e-9, "choice": choice, "index": index})

    def _leave_hover(self, *_args) -> None:
        if self._press is not None and self._press[3] == "":
            self.toolbar.remove_rubberband()
        self._press = None
        marker = self._hover_marker
        had = bool(self._hover_text) or marker is not None
        self._hover_text = ""
        self._hover_key = None
        self._hover_marker = None
        if marker is not None:
            try:
                marker.remove()
            except ValueError:
                pass
        if had:
            QToolTip.hideText()
            if marker is not None:
                self.canvas.draw_idle()
        self._sync_probe(None)

    def _on_press(self, event) -> None:
        self._press = None
        if getattr(event, "button", None) != 1 or event.x is None or event.y is None or event.inaxes is None:
            return
        if not event.inaxes.get_navigate():
            return
        self._press = (float(event.x), float(event.y), event.inaxes, str(self.toolbar.mode))

    def _on_release(self, event) -> None:
        press = self._press
        self._press = None
        if press is None or event.x is None or event.y is None:
            return
        x0, y0, ax, mode = press
        moved = abs(event.x - x0) >= 5 or abs(event.y - y0) >= 5
        if mode:
            if not moved and getattr(event, "button", None) == 1:
                self._toggle_pin(event)
            return
        self.toolbar.remove_rubberband()
        if getattr(event, "button", None) != 1:
            return
        if not moved:
            self._toggle_pin(event)
            return
        self._zoom_to_rect(ax, x0, y0, float(event.x), float(event.y))

    def _drag_zoom_band(self, event) -> None:
        if self._press is None or event.x is None or event.y is None:
            return
        x0, y0, _ax, _mode = self._press
        if abs(event.x - x0) < 5 and abs(event.y - y0) < 5:
            return
        self.toolbar.draw_rubberband(event, x0, y0, float(event.x), float(event.y))

    def _zoom_to_rect(self, ax, x0: float, y0: float, x1: float, y1: float) -> None:
        """Zoom to a dragged rectangle and record it so Home can restore the view."""
        stack = self.toolbar._nav_stack
        if len(stack) == 0:
            self.toolbar.push_current()
        axes = []
        for item in (ax, *ax._twinned_axes.get_siblings(ax)):
            if item not in axes:
                axes.append(item)
        bbox = (x0, y0, x1, y1)
        try:
            for index, item in enumerate(axes):
                twinx = any(item.get_shared_x_axes().joined(item, prev) for prev in axes[:index])
                twiny = any(item.get_shared_y_axes().joined(item, prev) for prev in axes[:index])
                item._set_view_from_bbox(bbox, "in", None, twinx, twiny)
        except (ValueError, OverflowError):
            return
        for item in axes:
            self._rescale_visible(item)
        self.toolbar.push_current()
        self.canvas.draw_idle()

    def _hook_toolbar_zoom(self) -> None:
        """The magnifying-glass zoom uses the same rescale as a drag on the chart."""
        toolbar = self.toolbar
        original = toolbar.release_zoom

        def release_zoom(event, _original=original):
            before = {ax: (ax.get_xlim(), ax.get_ylim()) for ax in self.figure.axes}
            _original(event)
            zoomed = False
            for ax, (xlim, ylim) in before.items():
                if ax.get_xlim() != xlim or ax.get_ylim() != ylim:
                    zoomed = True
                    self._rescale_visible(ax)
            if zoomed:
                self._replace_current_view()
                self.canvas.draw_idle()

        toolbar.release_zoom = release_zoom

    def _rescale_visible(self, ax) -> bool:
        """Fit both axes to the samples inside the current window and refresh the ticks."""
        lines = [line for line in ax.get_lines() if line.get_gid() == "data"]
        if not lines:
            return False
        x0, x1 = sorted(float(value) for value in ax.get_xlim())
        y0, y1 = sorted(float(value) for value in ax.get_ylim())
        picked_x = []
        picked_y = []
        x_log = ax.get_xscale() == "log"
        y_log = ax.get_yscale() == "log"
        for line in lines:
            xs = np.asarray(line.get_xdata(), dtype=float).reshape(-1)
            ys = np.asarray(line.get_ydata(), dtype=float).reshape(-1)
            count = min(xs.size, ys.size)
            if count == 0:
                continue
            xs = xs[:count]
            ys = ys[:count]
            inside = (xs >= x0) & (xs <= x1) & (ys >= y0) & (ys <= y1) & np.isfinite(xs) & np.isfinite(ys)
            if x_log:
                inside &= xs > 0
            if y_log:
                inside &= ys > 0
            if not np.any(inside):
                continue
            picked_x.append(xs[inside])
            picked_y.append(ys[inside])
        self._retick(ax)
        if not picked_x:
            return False
        x_lo, x_hi = _padded_bounds(float(np.min(np.concatenate(picked_x))), float(np.max(np.concatenate(picked_x))), ax.get_xscale())
        y_lo, y_hi = _padded_bounds(float(np.min(np.concatenate(picked_y))), float(np.max(np.concatenate(picked_y))), ax.get_yscale())
        if ax.get_aspect() == "equal" and not x_log and not y_log:
            x_lo, x_hi, y_lo, y_hi = _square_bounds(x_lo, x_hi, y_lo, y_hi)
        ax.set_xlim(x_lo, x_hi)
        ax.set_ylim(y_lo, y_hi)
        return True

    def _retick(self, ax) -> None:
        _retick_axis(ax.xaxis, ax.get_xscale())
        _retick_axis(ax.yaxis, ax.get_yscale())

    def _replace_current_view(self) -> None:
        toolbar = self.toolbar
        stack = toolbar._nav_stack
        view = WeakKeyDictionary(
            {ax: (ax._get_view(), (ax.get_position(True).frozen(), ax.get_position().frozen()))
             for ax in self.figure.axes})
        if len(stack) == 0:
            stack.push(view)
        else:
            stack._elements[stack._pos] = view
        toolbar.set_history_buttons()

    def _toggle_pin(self, event) -> None:
        hit = self._nearest_sample(event)
        if hit is None:
            return
        ax, line, index, x, y = hit
        key = (id(line), index)
        if key in self._pins:
            self._remove_pin(key)
        else:
            self._add_pin(key, ax, line, x, y)
        self.canvas.draw_idle()

    def _add_pin(self, key, ax, line, x: float, y: float) -> None:
        color = line.get_color()
        marker = Line2D(
            [x],
            [y],
            marker="o",
            markersize=6,
            linestyle="none",
            color=color,
            markeredgecolor="white",
            markeredgewidth=0.8,
            zorder=6,
        )
        marker.set_gid("pin")
        marker.set_label("_pin")
        ax.add_line(marker)
        note = ax.annotate(
            self._format_hover(ax, line, x, y),
            xy=(x, y),
            xytext=(8, 8),
            textcoords="offset points",
            fontsize=8,
            color="#1c1c1c",
            arrowprops={"arrowstyle": "-", "color": color, "lw": 0.6},
            bbox={"boxstyle": "round,pad=0.25", "fc": "#fff8e7", "ec": color, "lw": 0.6},
            zorder=6,
            annotation_clip=True,
        )
        self._pins[key] = (marker, note)

    def _remove_pin(self, key) -> None:
        for artist in self._pins.pop(key, ()):
            try:
                artist.remove()
            except ValueError:
                pass

    def _clear_pins(self, *_args) -> None:
        pins = list(self._pins.values())
        self._pins = {}
        for artists in pins:
            for artist in artists:
                try:
                    artist.remove()
                except ValueError:
                    pass
        if pins:
            self.canvas.draw_idle()

    def _reset_navigation(self) -> None:
        self._pins = {}
        self._press = None
        stack = getattr(getattr(self, "toolbar", None), "_nav_stack", None)
        if stack is not None:
            stack.clear()

    def _remember_view(self) -> None:
        stack = getattr(getattr(self, "toolbar", None), "_nav_stack", None)
        if stack is not None and len(stack) == 0:
            self.toolbar.push_current()

    def _on_hover(self, event) -> None:
        if getattr(self.toolbar, "_zoom_info", None) is not None or getattr(self.toolbar, "_pan_info", None) is not None:
            return
        if self._press is not None and self._press[3] == "":
            self._drag_zoom_band(event)
            return
        hit = self._nearest_sample(event)
        if hit is None:
            self._leave_hover()
            return
        ax, line, index, x, y = hit
        self._sync_probe(x if self._tab_title() == "TDR" else None)
        self._hover_text = self._format_hover(ax, line, x, y)
        key = (id(line), index)
        if key != self._hover_key:
            self._hover_key = key
            self._move_hover_marker(ax, x, y, line.get_color())
            self.canvas.draw_idle()
        QToolTip.showText(QCursor.pos(), self._hover_text, self.canvas)

    def _nearest_sample(self, event):
        if event is None or event.x is None or event.y is None or event.inaxes is None:
            return None
        best = None
        best_d = float(_HOVER_PX * _HOVER_PX)
        for ax in self.figure.axes:
            for line in ax.get_lines():
                if line.get_gid() != "data":
                    continue
                xs = np.asarray(line.get_xdata(), dtype=float).reshape(-1)
                ys = np.asarray(line.get_ydata(), dtype=float).reshape(-1)
                count = min(xs.size, ys.size)
                if count == 0:
                    continue
                xs = xs[:count]
                ys = ys[:count]
                finite = np.isfinite(xs) & np.isfinite(ys)
                if not np.any(finite):
                    continue
                try:
                    displayed = line.get_transform().transform(np.column_stack((xs, ys)))
                except (ValueError, OverflowError):
                    continue
                distance = (displayed[:, 0] - event.x) ** 2 + (displayed[:, 1] - event.y) ** 2
                distance[~finite] = np.inf
                index = int(np.argmin(distance))
                if not np.isfinite(distance[index]) or distance[index] > best_d:
                    continue
                best_d = float(distance[index])
                best = (ax, line, index, float(xs[index]), float(ys[index]))
        return best

    def _format_hover(self, ax, line, x: float, y: float) -> str:
        label = line.get_label() or ""
        if label.startswith("_"):
            label = ""
        if not label:
            label = ax.get_title() or ""
        head = f"{label}\n" if label else ""
        if self._tab_title() == "TDR":
            eps = float(self.eps.value())
            mm = float(distance_m([x * 1e-9], eps)[0]) * 1e3
            where = "before the port" if x < 0 else f"{mm:.2f} mm along the trace"
            return f"{head}{x:.4g} ns   {y:.4g} Ω\n{where}"
        if self._tab_title() == "Smith":
            radius = float(np.hypot(x, y))
            angle = float(np.degrees(np.arctan2(y, x)))
            return f"{head}Re {x:.4g}   Im {y:.4g}\n|S| {radius:.4g}   ∠ {angle:.1f}°"
        xlabel = ax.get_xlabel() or "x"
        ylabel = ax.get_ylabel() or "y"
        return f"{head}{xlabel}  {x:.4g}\n{ylabel}  {y:.4g}"

    def _move_hover_marker(self, ax, x: float, y: float, color) -> None:
        marker = self._hover_marker
        if marker is not None:
            try:
                marker.remove()
            except ValueError:
                pass
        marker = Line2D(
            [x],
            [y],
            marker="o",
            markersize=7,
            linestyle="none",
            color=color,
            markeredgecolor="white",
            markeredgewidth=0.8,
            zorder=5,
        )
        marker.set_gid("hover")
        marker.set_label("_hover")
        ax.add_line(marker)
        self._hover_marker = marker

    def redraw(self, *_args) -> None:
        # A TDR redraw drops the chart marker. Put the same sample back so the
        # board mark follows εeff instead of disappearing with the axes.
        hovering = self._probe_token
        self._drop_hover()
        self._reset_navigation()
        try:
            self._redraw_current()
        finally:
            self._remember_view()
            if hovering is not None and self._tab_title() == "TDR":
                self._sync_probe(hovering)

    def _redraw_current(self) -> None:
        title = self._tab_title()
        self._apply_chrome(title)
        if title == "PDN":
            self._draw_pdn()
            return
        ntwk = self.displayed_network()
        if ntwk is None:
            self._say("Run a simulation to plot S, Z, and TDR.")
            return
        kind, which = self._current_choice()
        pair = self._resolved_pair(which) if kind == "diff" else None
        if kind == "diff" and pair is None:
            self._say("This pair's ports are not in the loaded results.")
            return
        self._engine(True)
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        index = which
        freq_ghz = np.asarray(ntwk.f, dtype=float) / 1e9
        try:
            if title == "Magnitude":
                if pair is None:
                    self._magnitude(ax, ntwk, freq_ghz)
                else:
                    self._differential_spectrum(ax, ntwk, freq_ghz, pair, degrees=False)
            elif title == "Phase":
                if pair is None:
                    self._phase(ax, ntwk, freq_ghz)
                else:
                    self._differential_spectrum(ax, ntwk, freq_ghz, pair, degrees=True)
            elif title == "Smith":
                self.figure.clear()
                if pair is None:
                    self._smith(index)
                else:
                    self._smith_differential(pair)
            elif title == "Z":
                if pair is None:
                    self._impedance(ax, ntwk, freq_ghz, index)
                else:
                    self._differential_z(ax, freq_ghz, pair)
            elif title == "TDR":
                if pair is None:
                    self._tdr(ax, ntwk, index)
                else:
                    self._tdr_differential(ax, pair)
            else:
                if pair is None:
                    self._voltage(ax, index)
                else:
                    self._engine(False)
                    ax.text(
                        0.5,
                        0.5,
                        "This tab is the single-ended port waveforms.\nDifferential impedance is on the TDR and Z tabs.",
                        ha="center",
                        va="center",
                    )
                    ax.set_axis_off()
        except Exception as exc:
            self._engine(False)
            self.figure.clear()
            ax = self.figure.add_subplot(111)
            ax.text(0.5, 0.5, str(exc), ha="center", va="center", wrap=True)
            ax.set_axis_off()
        self.canvas.draw_idle()

    def _draw_pdn(self) -> None:
        result = self._pdn
        self._engine(True)
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        field = None if result is None else result.field_impedance
        field_freq = np.asarray([] if result is None or result.field_freq_hz is None else result.field_freq_hz, dtype=float)
        if result is None or (field_freq.size == 0 and not (result.drops or [])):
            self._engine(False)
            ax.text(0.5, 0.5, "Run PDN to plot impedance and the DC drop.", ha="center", va="center")
            ax.set_axis_off()
            self.canvas.draw_idle()
            return
        name = self._pdn_load or (next(iter(field or result.impedance), ""))
        if field and name in field and field_freq.size:
            for line in ax.loglog(field_freq, np.maximum(np.abs(field[name]), 1e-12), label=name):
                line.set_gid("data")
        elif field_freq.size == 0:
            self._engine(False)
            ax.text(
                0.5,
                0.5,
                "The DC drop is in the table.\nThe impedance curve is the field run.",
                ha="center",
                va="center",
            )
            ax.set_axis_off()
            self.canvas.draw_idle()
            return
        if "target" in self._show_pdn and result.z_target is not None and result.z_target > 0:
            target = ax.axhline(result.z_target, color="#c45c26", linestyle="--", label="Target")
            target.set_gid("data")
        self._style_or_empty(ax, "Frequency (Hz)", "|Z| (ohm)")
        if ax.get_lines():
            ax.set_xscale("log")
            ax.set_yscale("log")
        self.canvas.draw_idle()

    def _magnitude(self, ax, ntwk, freq_ghz) -> None:
        s = np.asarray(ntwk.s)
        n = s.shape[1]
        for i in range(n):
            for j in range(n):
                if not self._want(i, j):
                    continue
                mag = 20 * np.log10(np.maximum(np.abs(s[:, i, j]), 1e-15))
                self._data(ax, freq_ghz, mag, label=f"|S{i + 1}{j + 1}|")
        self._style_or_empty(ax, "Frequency (GHz)", "Magnitude (dB)")

    def _phase(self, ax, ntwk, freq_ghz) -> None:
        s = np.asarray(ntwk.s)
        n = s.shape[1]
        for i in range(n):
            for j in range(n):
                if not self._want(i, j):
                    continue
                self._data(ax, freq_ghz, np.angle(s[:, i, j], deg=True), label=f"S{i + 1}{j + 1}")
        self._style_or_empty(ax, "Frequency (GHz)", "Phase (degrees)")

    def _smith(self, _index: int) -> None:
        import skrf.plotting as plotting

        # plotting.smith draws on the axes it is given and returns None.
        ax = self.figure.add_subplot(111)
        plotting.smith(ax=ax, draw_labels=True)
        ntwk = self.displayed_network()
        s = np.asarray(ntwk.s)
        handles = []
        for i in range(s.shape[1]):
            for j in range(s.shape[2]):
                if not self._want(i, j):
                    continue
                curve = s[:, i, j]
                line = self._data(ax, curve.real, curve.imag, label=f"S{i + 1}{j + 1}")
                self._mark_smith_end(ax, curve, line.get_color())
                handles.append(line)
        if handles:
            ax.legend(handles=handles, fontsize=8)

    def _impedance(self, ax, ntwk, freq_ghz, _index: int) -> None:
        z = z_matrix(ntwk)
        n = z.shape[1]
        for i in range(n):
            for j in range(n):
                if not self._want(i, j):
                    continue
                self._data(ax, freq_ghz, np.real(z[:, i, j]), label=f"Re Z{i + 1}{j + 1}")
                self._data(ax, freq_ghz, np.imag(z[:, i, j]), linestyle="--", label=f"Im Z{i + 1}{j + 1}")
        self._style_or_empty(ax, "Frequency (GHz)", "Impedance (Ω)")

    def _tdr(self, ax, ntwk, index: int) -> None:
        time_s, impedance = tdr_impedance(ntwk, index)
        time_ns = np.asarray(time_s) * 1e9
        self._data(ax, time_ns, impedance, color="#0b6e4f")
        t0, t1 = tdr_view_window(time_s, impedance)
        ax.set_xlim(t0 * 1e9, t1 * 1e9)
        visible = (np.asarray(time_s) >= t0) & (np.asarray(time_s) <= t1)
        shown = impedance[visible] if np.any(visible) else impedance
        ax.set_ylim(*tdr_ohm_limits(shown))
        ax.set_xlabel("Time (ns)")
        ax.set_ylabel("Impedance (Ω)")
        ax.grid(True, alpha=0.3)
        eps = float(self.eps.value())

        def to_mm(t_ns):
            return distance_m(np.asarray(t_ns, dtype=float) * 1e-9, eps) * 1e3

        def to_ns(dist_mm):
            velocity = 299_792_458.0 / np.sqrt(eps)
            return np.asarray(dist_mm, dtype=float) * 1e-3 * 2 / velocity * 1e9

        top = ax.secondary_xaxis("top", functions=(to_mm, to_ns))
        top.set_xlabel("Distance (mm), constant-velocity estimate")
        ax.set_title(self._names[index] if index < len(self._names) else "")

    def _style_axes(self, ax, xlabel: str, ylabel: str) -> None:
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)
        if ax.get_lines():
            ax.legend(fontsize=8)

    def _style_or_empty(self, ax, xlabel: str, ylabel: str) -> None:
        if not ax.get_lines():
            self._engine(False)
            ax.text(0.5, 0.5, "Check a curve to show it.", ha="center", va="center")
            ax.set_axis_off()
            return
        self._style_axes(ax, xlabel, ylabel)

    def _differential_spectrum(self, ax, ntwk, freq_ghz, resolved, degrees: bool) -> None:
        _pair, positive, negative = resolved
        sdd, scc = differential_s(ntwk.s, positive, negative)
        if degrees:
            if self._want_diff("Sdd"):
                self._data(ax, freq_ghz, np.angle(sdd, deg=True), label="Sdd")
            if self._want_diff("Scc"):
                self._data(ax, freq_ghz, np.angle(scc, deg=True), label="Scc")
            self._style_or_empty(ax, "Frequency (GHz)", "Phase (degrees)")
        else:
            if self._want_diff("Sdd"):
                self._data(ax, freq_ghz, 20 * np.log10(np.maximum(np.abs(sdd), 1e-15)), label="|Sdd|")
            if self._want_diff("Scc"):
                self._data(ax, freq_ghz, 20 * np.log10(np.maximum(np.abs(scc), 1e-15)), label="|Scc|")
            self._style_or_empty(ax, "Frequency (GHz)", "Magnitude (dB)")

    def _smith_differential(self, resolved) -> None:
        import skrf.plotting as plotting

        pair, positive, negative = resolved
        ax = self.figure.add_subplot(111)
        plotting.smith(ax=ax, draw_labels=True)
        ntwk = self.displayed_network()
        sdd, scc = differential_s(ntwk.s, positive, negative)
        handles = []
        if self._want_diff("Sdd"):
            line = self._data(ax, np.real(sdd), np.imag(sdd), label=f"Sdd {pair.name}")
            self._mark_smith_end(ax, sdd, line.get_color())
            handles.append(line)
        if self._want_diff("Scc"):
            line = self._data(ax, np.real(scc), np.imag(scc), label=f"Scc {pair.name}")
            self._mark_smith_end(ax, scc, line.get_color())
            handles.append(line)
        if handles:
            ax.legend(handles=handles, fontsize=8)

    def _mark_smith_end(self, ax, curve, color: str) -> None:
        """Dot the last finite frequency so the end of the sweep is visible."""
        values = np.asarray(curve)
        finite = np.isfinite(values.real) & np.isfinite(values.imag)
        if not np.any(finite):
            return
        last = int(np.flatnonzero(finite)[-1])
        ax.plot(values.real[last], values.imag[last], marker="o", color=color, markersize=4, linestyle="none")

    def _differential_z(self, ax, freq_ghz, resolved) -> None:
        _pair, positive, negative = resolved
        diff, common = mixed_mode_networks(self.displayed_network(), positive, negative)
        if self._want_diff("Sdd"):
            z = z_matrix(diff)[:, 0, 0]
            self._data(ax, freq_ghz, np.real(z), label="Re Zdiff")
            self._data(ax, freq_ghz, np.imag(z), linestyle="--", label="Im Zdiff")
        if self._want_diff("Scc"):
            zc = z_matrix(common)[:, 0, 0]
            self._data(ax, freq_ghz, np.real(zc), label="Re Zcc")
            self._data(ax, freq_ghz, np.imag(zc), linestyle="--", label="Im Zcc")
        self._style_or_empty(ax, "Frequency (GHz)", "Impedance (Ω)")

    def _tdr_differential(self, ax, resolved) -> None:
        pair, positive, negative = resolved
        ntwk = self.displayed_network()
        diff, common = mixed_mode_networks(ntwk, positive, negative)
        time_s, z_diff = tdr_impedance(diff, 0)
        _t_common, z_common = tdr_impedance(common, 0)
        time_ns = np.asarray(time_s) * 1e9
        if self._want_diff("Sdd"):
            self._data(ax, time_ns, z_diff, color="#0b6e4f", label="Zdiff")
        if self._want_diff("Scc"):
            self._data(ax, time_ns, z_common, color="#1d4e89", label="Zcommon")
        if not ax.get_lines():
            self._engine(False)
            ax.text(0.5, 0.5, "Check a curve to show it.", ha="center", va="center")
            ax.set_axis_off()
            return
        t0, t1 = tdr_view_window(time_s, z_diff)
        ax.set_xlim(t0 * 1e9, t1 * 1e9)
        visible = (np.asarray(time_s) >= t0) & (np.asarray(time_s) <= t1)
        parts = []
        if self._want_diff("Sdd"):
            parts.append(z_diff[visible] if np.any(visible) else z_diff)
        if self._want_diff("Scc"):
            parts.append(z_common[visible] if np.any(visible) else z_common)
        shown = np.concatenate(parts) if parts else z_diff
        ax.set_ylim(*tdr_ohm_limits(shown))
        ax.set_xlabel("Time (ns)")
        ax.set_ylabel("Impedance (Ω)")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
        eps = float(self.eps.value())

        def to_mm(t_ns):
            return distance_m(np.asarray(t_ns, dtype=float) * 1e-9, eps) * 1e3

        def to_ns(dist_mm):
            velocity = 299_792_458.0 / np.sqrt(eps)
            return np.asarray(dist_mm, dtype=float) * 1e-3 * 2 / velocity * 1e9

        top = ax.secondary_xaxis("top", functions=(to_mm, to_ns))
        top.set_xlabel("Distance (mm), constant-velocity estimate")
        ax.set_title(f"{pair.name}  {pair.positive}/{pair.negative}")

    def _voltage(self, ax, index: int) -> None:
        if not self._excitations:
            self._engine(False)
            ax.text(0.5, 0.5, "No time-domain samples in this result.", ha="center", va="center")
            ax.set_axis_off()
            return
        index = min(index, len(self._excitations) - 1)
        sample = self._excitations[index]
        time_ns = np.asarray(sample["t"], dtype=float) * 1e9
        voltage = np.asarray(sample["v"], dtype=float)
        if voltage.ndim == 1:
            self._data(ax, time_ns, voltage, label=self._names[index] if index < len(self._names) else "v")
        else:
            for row in range(voltage.shape[0]):
                name = self._names[row] if row < len(self._names) else f"P{row + 1}"
                self._data(ax, time_ns[: voltage.shape[1]], voltage[row], label=name)
        ax.set_xlabel("Time (ns)")
        ax.set_ylabel("Voltage (V)")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
        source = self._names[index] if index < len(self._names) else str(index + 1)
        ax.set_title(f"Excitation at {source}")

    def _say(self, text: str) -> None:
        self._drop_hover()
        self._reset_navigation()
        self._engine(False)
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.text(0.5, 0.5, text, ha="center", va="center")
        ax.set_axis_off()
        self.canvas.draw_idle()
        self._remember_view()


def _padded_bounds(lo: float, hi: float, scale: str) -> tuple[float, float]:
    if scale == "log":
        lo = max(lo, np.finfo(float).tiny)
        hi = max(hi, lo)
        log_lo = float(np.log10(lo))
        log_hi = float(np.log10(hi))
        span = log_hi - log_lo
        pad = 0.08 * span if span > 0 else 0.05
        return 10 ** (log_lo - pad), 10 ** (log_hi + pad)
    span = hi - lo
    pad = 0.08 * span if span > 0 else max(abs(lo), abs(hi), 1.0) * 0.05
    return lo - pad, hi + pad


def _square_bounds(x_lo: float, x_hi: float, y_lo: float, y_hi: float):
    x_span = x_hi - x_lo
    y_span = y_hi - y_lo
    if x_span <= 0 or y_span <= 0 or abs(x_span - y_span) <= 1e-15 * max(x_span, y_span, 1.0):
        return x_lo, x_hi, y_lo, y_hi
    if x_span < y_span:
        mid = (x_lo + x_hi) / 2
        half = y_span / 2
        return mid - half, mid + half, y_lo, y_hi
    mid = (y_lo + y_hi) / 2
    half = x_span / 2
    return x_lo, x_hi, mid - half, mid + half


def _retick_axis(axis, scale: str) -> None:
    locator = axis.get_major_locator()
    if isinstance(locator, NullLocator):
        return
    if scale == "log":
        axis.set_major_locator(LogLocator())
    elif isinstance(locator, (AutoLocator, MaxNLocator, LogLocator)):
        axis.set_major_locator(AutoLocator())


def _mv(value) -> str:
    if value is None:
        return "open"
    return f"{float(value):.3f}"
