"""Desktop window: open a board, attach lumped ports, run openEMS."""

from __future__ import annotations

import json
import math
import os
import pickle
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, QSettings, QSize, Qt, QThread, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QShortcut, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QPlainTextEdit,
    QDialog,
    QDialogButtonBox,
    QApplication,
)

from .model.board import assign_stackup_z, default_reference_net
from .model.crop import CropSettings, auto_crop_nets, bounds_of_nets, crop_board, region_polygon, region_ring
from .model.load import BOARD_FILTER, load_board
from .model.mesh import Mesh, MeshSettings, build_mesh
from .model.ports import port_at_click, vertical_port_on_pad
from .pdn.circuit import PdnPart, PdnTerminal, _fail, build_grid, solve_pdn
from .pdn.em import field_impedance
from .pdn.io import load_result, pdn_dir, pdn_em_dir, save_result, write_csv as write_pdn_csv
from .sim.job import load_job, pdn_mesh_settings
from .project import (
    DiffPair,
    PdnPartRecord,
    PdnTerminalRecord,
    Project,
    apply_stackup,
    board_path_for,
    load_project,
    project_fingerprint,
    save_project,
    stackup_payload,
    suggest_power_net,
)

from .calc.csv_export import write_csv
from .calc.differential import differential_export
from .calc.load import load_results
from .calc.tdr import default_eps_eff, distance_m
from .model.trace_path import clear_trace_cache, trace_point, trace_route
from .calc.touchstone import write_touchstone
from .sim.progress import describe_prep, describe_sim, prep_failure_message
from .view.board_view import BoardView
from .view.info_tip import info_button, note_with_info, with_info
from .view.results_view import ResultsView

WARN_CELLS_DEFAULT = 2_000_000
CONFIRM_CELLS_DEFAULT = 8_000_000

class FitLabel(QLabel):
    """Note that wraps inside the pane. The minimum width is the longest word, not the whole line."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)

    def minimumSizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        words = [part for part in self.text().replace("\n", " ").split(" ") if part]
        width = max((metrics.horizontalAdvance(word) for word in words), default=0)
        return QSize(width, metrics.lineSpacing())

    def sizeHint(self) -> QSize:
        return self.minimumSizeHint()


def _flex_width(widget) -> None:
    """Let a widget shrink to the pane. Its text elides instead of widening the column."""
    widget.setMinimumWidth(0)
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, widget.sizePolicy().verticalPolicy())


def _flex_combo(combo: QComboBox) -> None:
    """Size the closed combo to the pane. The popup still shows the full item text."""
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(8)
    _flex_width(combo)


def _beside(control, button) -> QHBoxLayout:
    """Control with a ? on the right. The control keeps the group box as its parent."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(2)
    control.setMinimumWidth(0)
    control.setSizePolicy(QSizePolicy.Policy.Ignored, control.sizePolicy().verticalPolicy())
    row.addWidget(control, 1)
    row.addWidget(button)
    return row


def _clip_list(widget: QListWidget) -> None:
    """Elide long net and port names instead of scrolling the list sideways."""
    widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    widget.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    _flex_width(widget)


SOLVER_HELP = (
    "openEMS is not bundled with this app. Install it into a Python environment "
    "and choose that interpreter. The window and the solver do not have to use the same Python.\n\n"
    "    python -m pip install openEMS CSXCAD\n\n"
    "Wheels are built for specific Python versions, so use the version the wheel supports. "
    "Test import runs import openEMS and import CSXCAD in the interpreter above. "
    "This app does not download openEMS."
)


def project_file_for(board: Path) -> Path:
    """Project file sits beside the board. An ODB++ folder uses the folder name, not a file inside it."""
    name = board.name
    lower = name.lower()
    if board.is_dir():
        return board.with_name(name + ".pcbsim.json")
    for suffix in (".kicad_pcb", ".tar.gz", ".ipc2581"):
        if lower.endswith(suffix):
            return board.with_name(name[: -len(suffix)] + ".pcbsim.json")
    return board.with_name(board.stem + ".pcbsim.json")


def _settings() -> QSettings:
    return QSettings("pcb-simulator", "pcb-simulator")


class SolverDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Solver Python")
        self.setMinimumWidth(520)
        settings = _settings()
        self.path = QLineEdit(str(settings.value("solver_python", "") or ""))
        self.path.setPlaceholderText(r"C:\Path\to\python.exe")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        self.warn = QSpinBox()
        self.warn.setRange(10_000, 100_000_000)
        self.warn.setSingleStep(100_000)
        self.warn.setValue(int(settings.value("warn_cells", WARN_CELLS_DEFAULT)))
        self.confirm = QSpinBox()
        self.confirm.setRange(10_000, 200_000_000)
        self.confirm.setSingleStep(100_000)
        self.confirm.setValue(int(settings.value("confirm_cells", CONFIRM_CELLS_DEFAULT)))
        self.result = QLabel("")
        self.result.setWordWrap(True)
        test = QPushButton("Test import")
        test.clicked.connect(self._test)
        form = QFormLayout()
        form.addRow("Interpreter", row)
        form.addRow("Warn above (cells)", self.warn)
        form.addRow("Confirm above (cells)", self.confirm)
        help_label = QLabel(SOLVER_HELP)
        help_label.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(help_label)
        layout.addLayout(form)
        layout.addWidget(test)
        layout.addWidget(self.result)

        layout.addWidget(buttons)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Solver Python", self.path.text(), "Python (python.exe python)")
        if path:
            self.path.setText(path)

    def _test(self) -> None:
        program = self.path.text().strip()
        if not program:
            self.result.setText("Choose a Python interpreter first.")
            return
        proc = QProcess(self)
        proc.start(program, ["-c", "import openEMS, CSXCAD; print('openEMS import ok')"])
        if not proc.waitForStarted(5000):
            self.result.setText("Could not start that interpreter.")
            return
        proc.waitForFinished(20000)
        out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
        err = bytes(proc.readAllStandardError()).decode("utf-8", "replace")
        if proc.exitCode() == 0:
            self.result.setText(out.strip() or "openEMS import ok")
        else:
            self.result.setText((err or out or "import failed").strip())

    def _save(self) -> None:
        settings = _settings()
        settings.setValue("solver_python", self.path.text().strip())
        settings.setValue("warn_cells", self.warn.value())
        settings.setValue("confirm_cells", self.confirm.value())
        self.accept()


class _PdnThread(QThread):
    """DC drop and AC sweep off the GUI thread. It does not start openEMS."""

    finished_ok = Signal(object)

    def __init__(self, board, crop, power_net, reference, pitch, conductivity, plating, power_layer, ground_layer, vrm, loads, parts, ripple, step):
        super().__init__()
        self._job = (
            board,
            crop,
            power_net,
            reference,
            pitch,
            conductivity,
            plating,
            power_layer,
            ground_layer,
            vrm,
            loads,
            parts,
            ripple,
            step,
        )

    def run(self) -> None:
        board, crop, power_net, reference, pitch, conductivity, plating, power_layer, ground_layer, vrm, loads, parts, ripple, step = self._job
        try:
            grid = build_grid(board, crop, power_net, pitch, conductivity, plating, power_layer, ground_layer)
            result = solve_pdn(
                grid,
                power_net,
                reference,
                vrm,
                loads,
                parts,
                ripple_mv=ripple,
                step_a=step,
            )
        except Exception as exc:
            result = _fail(str(exc), [])
        self.finished_ok.emit(result)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PCB Simulator")
        self.resize(1400, 860)
        self.board = None
        self.board_path: Path | None = None
        self.project = Project()
        self.mesh: Mesh | None = None
        self._process: QProcess | None = None
        self._prep: QProcess | None = None
        self._prep_request: Path | None = None
        self._prep_cancelled = False
        self._prep_text = ""
        self._prep_buf = ""
        self._stdout_buf = ""
        self._job_dir: Path | None = None
        self._two_click: tuple[float, float, str] | None = None
        self._part_anchor = None
        self._terminal_anchor = None
        self._pdn_thread: QThread | None = None
        self._pdn_result = None
        self._job_kind = "si"
        self._want_field = False
        self._filling = False
        self._saved_fingerprint: str | None = None
        self._save_quiet = False
        self._checking_save = False
        self.save_state = QLabel("")
        self.save_state.setToolTip(
            "Whether the .pcbsim.json beside the board matches this window. "
            "Ctrl+S writes that file. The KiCad file is not modified."
        )
        self.statusBar().addPermanentWidget(self.save_state)
        self._splitter: QSplitter | None = None
        self._splitter_dragged = False
        self._applying_sizes = False
        self._results_open = True
        self._layers_open = True
        self._solver_dock = None
        self._log_sized = False
        self._board_log_lines = 0
        self._probe_spec = None

        self.view = BoardView()
        self.results = ResultsView()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.preflight = FitLabel("Open a KiCad board, an IPC-2581 file, or an ODB++ folder or archive.")
        self._build_commands()
        self._build_panels()
        self.view.status_text.connect(self.statusBar().showMessage)
        self.view.crop_rect.connect(self._on_crop_rect)
        self.view.pad_clicked.connect(self._on_pad_clicked)
        self.view.copper_clicked.connect(self._on_copper_clicked)
        self.view.net_picked.connect(self._highlight_net)
        self.view.measure_clicked.connect(self._on_measure)
        self.view.component_picked.connect(self._on_component_picked)
        self.results.tdr_probe.connect(self._on_tdr_probe)
        self._delete_shortcuts = []
        for widget in (self.view, self.port_list):
            shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), widget)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self._delete_selected)
            self._delete_shortcuts.append(shortcut)
        self.results.eps_eff_changed.connect(self._on_eps_eff)
        self.results.export_csv_requested.connect(self.export_csv)
        self.results.export_pdn_requested.connect(self.export_pdn_csv)
        self.results.overlay_changed.connect(self._on_overlay)
        self._show_save_status(None)
        self.statusBar().showMessage("Open a KiCad, IPC-2581, or ODB++ board to start.")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_pane_sizes()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        dock = self._solver_dock
        if dock is not None and not self._log_sized:
            self._log_sized = True
            self.resizeDocks([dock], [88], Qt.Orientation.Vertical)

    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        if not self._applying_sizes:
            self._splitter_dragged = True

    def _apply_pane_sizes(self) -> None:
        """Results on the left, the board in the center, layers on the right when that pane is open."""
        splitter = self._splitter
        if splitter is None or splitter.count() != 3 or self._splitter_dragged or self._applying_sizes:
            return
        width = splitter.width()
        if width < 100:
            return
        layers = getattr(self, "_layers_panel", None)
        layers_open = self._layers_open
        results_open = self._results_open
        visible = int(results_open) + 1 + int(layers_open)
        handles = splitter.handleWidth() * max(visible - 1, 0)
        usable = max(width - handles, 1)
        side = min(220, max(160, usable // 5)) if layers_open else 0
        rest = max(usable - side, 1)
        if results_open:
            results_w = max(200, int(rest * 0.25))
            if results_w >= rest:
                results_w = max(rest // 4, 1)
            board = rest - results_w
        else:
            results_w = 0
            board = rest
        self._applying_sizes = True
        splitter.setSizes([results_w, board, side])
        self._applying_sizes = False

    def _toggle_results_panel(self, show: bool) -> None:
        """Run → Results shows or hides the plots on the left of the board."""
        panel = self.results
        splitter = self._splitter
        if splitter is None or splitter.count() != 3:
            return
        self._results_open = bool(show)
        sizes = list(splitter.sizes())
        if show:
            panel.setMinimumWidth(200)
            panel.show()
            if self._splitter_dragged:
                take = min(max(200, sizes[1] // 4), sizes[1])
                self._applying_sizes = True
                splitter.setSizes([take, sizes[1] - take, sizes[2]])
                self._applying_sizes = False
            else:
                self._apply_pane_sizes()
            splitter.setCollapsible(0, False)
        else:
            returned = sizes[0]
            splitter.setCollapsible(0, True)
            panel.setMinimumWidth(0)
            panel.hide()
            if self._splitter_dragged:
                self._applying_sizes = True
                splitter.setSizes([0, sizes[1] + returned, sizes[2]])
                self._applying_sizes = False
            else:
                self._apply_pane_sizes()
        button = getattr(self, "_results_button", None)
        if button is not None and button.isChecked() != show:
            button.blockSignals(True)
            button.setChecked(show)
            button.blockSignals(False)

    def _toggle_layers_panel(self, show: bool) -> None:
        """View → Layers shows or hides the layer list on the right of the board."""
        panel = getattr(self, "_layers_panel", None)
        splitter = self._splitter
        if panel is None or splitter is None or splitter.count() != 3:
            return
        self._layers_open = bool(show)
        sizes = list(splitter.sizes())
        if show:
            panel.setMinimumWidth(160)
            panel.show()
            if self._splitter_dragged:
                take = min(220, max(160, sizes[1] - 80))
                board = max(sizes[1] - take, 0)
                self._applying_sizes = True
                splitter.setSizes([sizes[0], board, sizes[1] - board])
                self._applying_sizes = False
            else:
                self._apply_pane_sizes()
            splitter.setCollapsible(2, False)
        else:
            returned = sizes[2]
            splitter.setCollapsible(2, True)
            panel.setMinimumWidth(0)
            panel.hide()
            if self._splitter_dragged:
                self._applying_sizes = True
                splitter.setSizes([sizes[0], sizes[1] + returned, 0])
                self._applying_sizes = False
            else:
                self._apply_pane_sizes()
        button = getattr(self, "_layers_button", None)
        if button is not None and button.isChecked() != show:
            button.blockSignals(True)
            button.setChecked(show)
            button.blockSignals(False)

    def _build_commands(self) -> None:
        """Shortcuts only. The shelf tabs are the one row of commands, so the menu bar stays hidden."""
        self.menuBar().hide()

        def command(text: str, slot, shortcut=None) -> QAction:
            item = QAction(text, self)
            item.triggered.connect(slot)
            if shortcut is not None:
                item.setShortcut(shortcut)
                self.addAction(item)
            return item

        self._file_actions = [
            command("Open board…", self._open_board_dialog, QKeySequence.Open),
            command("Open ODB++ folder…", self._open_odb_folder_dialog),
            command("Open project…", self._open_project_dialog),
            command("Save project", self.save_project, QKeySequence.Save),
            command("Export Touchstone…", self.export_touchstone),
            command("Export CSV…", self.export_csv),
            command("Export PDN CSV…", self.export_pdn_csv),
            command("Quit", self.close),
        ]
        command("S parameters", self.run_simulation, QKeySequence("Ctrl+R"))
        self._solver_action = command("Solver Python…", self._solver_dialog)
        escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        escape.setContext(Qt.ShortcutContext.WindowShortcut)
        escape.activated.connect(self._on_escape)
        self._escape_shortcut = escape

    def _build_panels(self) -> None:
        self.layer_box = QVBoxLayout()
        self.net_list = QListWidget()
        self.net_list.itemChanged.connect(self._on_nets_changed)
        self.net_list.itemClicked.connect(self._on_net_clicked)
        _clip_list(self.net_list)
        self.reference = QComboBox()
        self.reference.currentTextChanged.connect(self._on_reference)
        _flex_combo(self.reference)
        self.stack_table = QTableWidget(0, 5)
        self.stack_table.setHorizontalHeaderLabels(["Layer", "Kind", "mm", "εr", "tanδ"])
        header = self.stack_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(24)
        header_tips = (
            "Layer name",
            "Copper or dielectric",
            "Thickness in millimetres. Copper thickness is stored and is not meshed.",
            "Relative permittivity",
            "Loss tangent at 10 GHz. The job stores Debye poles from 1 MHz to 200 GHz. The field solve uses them when this openEMS has the Debye updater. Otherwise the layer stays lossless. Blank or 0 is lossless either way.",
        )
        for index, tip in enumerate(header_tips):
            item = self.stack_table.horizontalHeaderItem(index)
            if item is not None:
                item.setToolTip(tip)
        self.stack_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.stack_table.setMinimumHeight(120)
        _flex_width(self.stack_table)
        self.stack_table.itemChanged.connect(self._on_stackup_edited)
        self.mode_crop = QRadioButton("Crop")
        self.mode_outline = QRadioButton("Whole outline")
        self.mode_crop.setChecked(True)
        self.mode_crop.toggled.connect(self._on_mode)
        self.margin = QDoubleSpinBox()
        self.margin.setRange(0.0, 50.0)
        self.margin.setDecimals(2)
        self.margin.setValue(2.0)
        self.margin.setSuffix(" mm")
        self.margin.setToolTip(
            "Extra board kept around the crop, clipped to the outline. "
            "It holds the ground plane and dielectric next to the trace. "
            "The absorbing boundary is farther out than this; the mesher adds that air on its own."
        )
        self.margin.valueChanged.connect(self._on_margin)
        crop_btn = QPushButton("Draw crop")
        crop_btn.setToolTip("Drag a rectangle on the board. The margin is added around it.")
        crop_btn.clicked.connect(lambda: self._set_tool("crop"))
        clear_crop = QPushButton("Clear crop")
        clear_crop.setToolTip("Remove the crop rectangle.")
        clear_crop.clicked.connect(self._clear_rect)
        self.port_r = QDoubleSpinBox()
        self.port_r.setRange(1.0, 500.0)
        self.port_r.setDecimals(1)
        self.port_r.setValue(50.0)
        self.port_r.setSuffix(" Ω")
        self.port_r.setToolTip(
            "Termination of the lumped port, the same role as a VNA or TDR impedance. "
            "It is not the trace impedance. A 40 Ω trace with a 50 Ω port shows a reflection "
            "in S11 and about 40 Ω on the TDR."
        )
        self.port_r.valueChanged.connect(self._on_port_r)
        self.port_list = QListWidget()
        self.port_list.currentRowChanged.connect(self._on_port_selected)
        _clip_list(self.port_list)
        pad_btn = QPushButton("Click a pad")
        pad_btn.clicked.connect(lambda: self._set_tool("port"))
        two_btn = QPushButton("Two clicks")
        two_btn.clicked.connect(self._arm_two_click)
        all_btn = QPushButton("Port every pad")
        all_btn.clicked.connect(self.port_every_pad)
        delete_btn = QPushButton("Delete port")
        delete_btn.clicked.connect(self._delete_port)
        self.pair_pos = QComboBox()
        self.pair_neg = QComboBox()
        _flex_combo(self.pair_pos)
        _flex_combo(self.pair_neg)
        self.pair_list = QListWidget()
        _clip_list(self.pair_list)
        add_pair = QPushButton("Add pair")
        add_pair.setToolTip("Add the differential pair selected above. This does not rerun the solver.")
        add_pair.clicked.connect(self._on_add_pair)
        delete_pair = QPushButton("Delete pair")
        delete_pair.clicked.connect(self._delete_pair)
        nav_btn = QPushButton("Pan / select")
        nav_btn.clicked.connect(lambda: self._set_tool("navigate"))

        self.f_max = QDoubleSpinBox()
        self.f_max.setRange(0.05, 80.0)
        self.f_max.setDecimals(3)
        self.f_max.setValue(5.0)
        self.f_max.setSuffix(" GHz")
        self.f_max.setToolTip(
            "Highest frequency of the Gaussian source (−20 dB) and of the plots. "
            "The TDR 10–90% edge scales as 1 / f max: about 0.23 ns at 5 GHz on the ohms plot. "
            "The mesh does not change. Open space stays at a tenth of a wavelength at 5 GHz. "
            "tanδ on the stackup is the value at 10 GHz. The field solve uses Debye poles for it when this openEMS has that updater."
        )
        self.f_max.valueChanged.connect(self._on_solver_fields)
        self.n_freq = QSpinBox()
        self.n_freq.setRange(11, 5001)
        self.n_freq.setValue(401)
        self.n_freq.setToolTip(
            "How many frequencies S is sampled at after the timestep loop. This barely changes the solve time. "
            "The TDR record is about this many periods of f max long, so fewer points hide a long line. "
            "The rise time follows f max."
        )
        self.n_freq.valueChanged.connect(self._on_solver_fields)
        self.end_criteria = QDoubleSpinBox()
        self.end_criteria.setRange(1e-8, 1e-1)
        self.end_criteria.setDecimals(6)
        self.end_criteria.setValue(1e-4)
        self.end_criteria.setToolTip(
            "Stop when the energy left in the volume has fallen by this factor. "
            "A larger number (1e-3) stops sooner and cuts off late reflections. 1e-4 keeps the tail."
        )
        self.end_criteria.valueChanged.connect(self._on_solver_fields)
        self.nr_ts = QSpinBox()
        self.nr_ts.setRange(1000, 5_000_000)
        self.nr_ts.setSingleStep(10000)
        self.nr_ts.setValue(1_000_000)
        self.nr_ts.setToolTip(
            "Hard cap on timesteps. Lowering it does not make a step faster. "
            "A run that hits the cap is marked as not decayed, and late reflections are missing. "
            "A finer mesh has a shorter timestep, so the same cap covers less time. "
            "Crossing can stop the S-parameter run sooner. This number stays the ceiling."
        )
        self.nr_ts.valueChanged.connect(self._on_solver_fields)
        self.cells_per_feature = QSpinBox()
        self.cells_per_feature.setRange(1, 30)
        self.cells_per_feature.setValue(2)
        self.cells_per_feature.setToolTip(
            "Copper lattice, 0.4 mm divided by this count, and never finer than 0.05 mm. "
            "1 is 0.4 mm and the fastest. 2 is the default 0.2 mm lattice, about 0.1 mm of width error. "
            "4 is 0.1 mm. 8 and above stay at 0.05 mm. Open space stays at a tenth of a wavelength at 5 GHz."
        )
        self.cells_per_feature.valueChanged.connect(self._on_solver_fields)
        self.fine_mesh = QCheckBox("Fine mesh")
        self.fine_mesh.toggled.connect(self._on_fine_mesh)
        self.pdn_f_max = QDoubleSpinBox()
        self.pdn_f_max.setRange(0.05, 80.0)
        self.pdn_f_max.setDecimals(3)
        self.pdn_f_max.setValue(1.0)
        self.pdn_f_max.setSuffix(" GHz")
        self.pdn_f_max.valueChanged.connect(self._on_solver_fields)
        self.pdn_n_freq = QSpinBox()
        self.pdn_n_freq.setRange(11, 5001)
        self.pdn_n_freq.setValue(201)
        self.pdn_n_freq.valueChanged.connect(self._on_solver_fields)
        self.pdn_signal_mesh = QCheckBox("Signal mesh")
        self.pdn_signal_mesh.toggled.connect(self._on_solver_fields)
        self.threads = QSpinBox()
        self.threads.setRange(0, 128)
        self.threads.setSpecialValueText("auto")
        self.threads.setToolTip(
            "auto uses every core for the timestep loop. The loop often levels off around 4–8 cores. "
            "The mesh and the result stay the same."
        )
        self.threads.valueChanged.connect(self._on_solver_fields)
        self.boundary = QComboBox()
        self.boundary.addItem("MUR", "mur")
        self.boundary.addItem("PML", "pml")
        _flex_combo(self.boundary)
        self.boundary.currentIndexChanged.connect(self._on_solver_fields)
        self.stop_mode = QComboBox()
        self.stop_mode.addItem("Crossing", "crossing")
        self.stop_mode.addItem("Energy", "energy")
        _flex_combo(self.stop_mode)
        self.stop_mode.currentIndexChanged.connect(self._on_solver_fields)
        self.signal_only = QCheckBox("Port nets")
        self.signal_only.setChecked(True)
        self.signal_only.toggled.connect(self._on_signal_only)
        preview = QPushButton("Preview mesh")
        preview.clicked.connect(self.preview_mesh)
        self.run_btn = QPushButton("S parameters")
        self.run_btn.clicked.connect(self.run_simulation)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_simulation)
        self._extra_cancels: list[QPushButton] = []

        from .view.shelf import assemble_shelf

        assemble_shelf(self)
        self.part_r.valueChanged.connect(self._on_part_fields)
        self.part_l.valueChanged.connect(self._on_part_fields)
        self.part_c.valueChanged.connect(self._on_part_fields)
        self.part_name.editingFinished.connect(self._on_part_fields)
        self.conductivity.editingFinished.connect(self._on_solver_fields)
        self.plating.valueChanged.connect(self._on_solver_fields)
        self.pitch.valueChanged.connect(self._on_solver_fields)
        self.power_net.currentTextChanged.connect(self._on_solver_fields)
        self.ac_power.currentIndexChanged.connect(self._on_solver_fields)
        self.ac_ground.currentIndexChanged.connect(self._on_solver_fields)

    def _log_dock(self):
        from PySide6.QtWidgets import QDockWidget

        dock = QDockWidget("Solver log", self)
        self.log.setMinimumHeight(48)
        dock.setWidget(self.log)
        self._solver_dock = dock
        return dock

    def _placement_failed(self, reason: str) -> None:
        """Status bar, solver log, and the terminal the app was launched from."""
        text = reason.strip() or "That could not be placed."
        print(text, file=sys.stderr, flush=True)
        self._log_line(text, "error")
        self.statusBar().showMessage(text)

    def _log_line(self, text: str, kind: str = "solver") -> None:
        """Append one log entry. Board notes are amber, app lines blue, problems red."""
        lines = [line for line in str(text).splitlines() if line.strip()]
        if not lines:
            return
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = self._log_format(kind)
        for line in lines:
            if self.log.document().characterCount() > 1:
                cursor.insertBlock()
            cursor.insertText(line, fmt)
        self.log.setTextCursor(cursor)
        self.log.ensureCursorVisible()

    def _prepend_log(self, text: str, kind: str) -> int:
        """Insert lines at the top of the solver log. Returns how many blocks were added."""
        lines = [line for line in str(text).splitlines() if line.strip()]
        if not lines:
            return 0
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        payload = "\n".join(lines)
        if self.log.toPlainText().strip():
            payload += "\n"
        cursor.insertText(payload, self._log_format(kind))
        self.log.moveCursor(QTextCursor.MoveOperation.Start)
        return len(lines)

    def _log_format(self, kind: str) -> QTextCharFormat:
        fmt = QTextCharFormat()
        color = {"board": "#8a5a00", "app": "#1f4e79", "error": "#9b2c2c"}.get(kind)
        if color:
            fmt.setForeground(QColor(color))
        return fmt

    def _clear_board_log(self) -> None:
        count = self._board_log_lines
        self._board_log_lines = 0
        if count <= 0:
            return
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.movePosition(QTextCursor.MoveOperation.NextBlock, QTextCursor.MoveMode.KeepAnchor, count)
        cursor.removeSelectedText()

    def _group(self, title: str, body) -> QGroupBox:
        box = QGroupBox(title)
        if isinstance(body, QVBoxLayout) or isinstance(body, QFormLayout) or isinstance(body, QHBoxLayout):
            box.setLayout(body)
        else:
            layout = QVBoxLayout(box)
            layout.addLayout(body)
        return box

    def _set_tool(self, tool: str) -> None:
        self._two_click = None
        self._part_anchor = None
        self._terminal_anchor = None
        self.view.set_tool(tool)
        self.view.set_two_click(False)
        self._select_tool(tool)
        if tool == "port":
            self._two_click = ("armed",)
            self.view.set_two_click(True)
        messages = {
            "navigate": "Click a port or a measurement, then Delete removes it. Click copper to highlight its net.",
            "crop": "Drag a rectangle on the board. The margin is added around it.",
            "port": "Click the signal copper. The port is placed on that click. Then click the reference for that port.",
            "vrm": "Click the power pad, then the ground pad. Voltage is the number on this page, including margin.",
            "load": "Click the power pad, then the ground pad. Current is the number on this page.",
            "part": "Click a pad to place the RLC. A two-pad footprint spans both pads.",
            "measure": "Click two points. Each line stays. On a trace, the length follows the copper. A pour is only the straight distance. Delete removes the selected line.",
        }
        text = messages.get(tool, "Drag with the right button to pan. Scroll to zoom toward the cursor.")
        if tool != "navigate":
            text += " Escape returns to pan. A point already down is cleared first."
        self.statusBar().showMessage(text)

    def _gesture_open(self) -> bool:
        """True while a second click, or a crop drag, would finish something already started."""
        if self._terminal_anchor is not None or self._part_anchor is not None:
            return True
        view = self.view
        if view._dragging or view._rubber is not None or view._port_anchor is not None or view._measure_anchor is not None:
            return True
        return isinstance(self._two_click, tuple) and len(self._two_click) == 3

    def _cancel_gesture(self) -> None:
        """Drop the half-placed port, source, or crop. The tool stays armed."""
        self._terminal_anchor = None
        self._part_anchor = None
        keep_two_click = self.view._tool == "port" and (self.view._two_click_mode or self._two_click is not None)
        if self._two_click is not None:
            self._two_click = ("armed",)
        self.view.cancel_gesture()
        self.view.set_two_click(keep_two_click)

    def _on_escape(self) -> None:
        if self._gesture_open():
            self._cancel_gesture()
            self.statusBar().showMessage("Stopped that placement. Escape again returns to pan.")
            return
        if self.view._tool != "navigate":
            self._set_tool("navigate")

    def _select_tool(self, tool: str) -> None:
        button = getattr(self, "_tool_buttons", {}).get(tool)
        if button is not None:
            button.setChecked(True)
        order = ("navigate", "port", "vrm", "load", "part", "crop")
        stack = getattr(self, "_place_stack", None)
        if stack is None:
            return
        if tool in order:
            stack.setCurrentIndex(order.index(tool))
        elif tool == "measure":
            stack.setCurrentIndex(0)

    def _arm_two_click(self) -> None:
        self._set_tool("port")
        self.view.set_two_click(True)
        self._two_click = ("armed",)
        self.statusBar().showMessage(
            "Click the signal net. The port is placed on that click. "
            "Then click the reference net. Escape clears this point. Escape again returns to pan."
        )

    def closeEvent(self, event) -> None:
        results = getattr(self, "results", None)
        if results is not None:
            results._dock_chart()
        for dialog in (
            getattr(self, "_s_dialog", None),
            getattr(self, "_pdn_dialog", None),
            getattr(self, "_nets_dialog", None),
            getattr(self, "_stack_dialog", None),
            *getattr(self, "_component_dialogs", ()),
        ):
            if dialog is not None:
                dialog.hide()
        prep = self._prep
        if prep is not None:
            try:
                prep.finished.disconnect(self._on_prepared)
            except (RuntimeError, TypeError):
                pass
            if prep.state() != QProcess.ProcessState.NotRunning:
                prep.kill()
        if self._process is not None:
            self._process.kill()
        thread = self._pdn_thread
        if thread is not None and thread.isRunning():
            thread.wait(3000)
        super().closeEvent(event)

    def _prep_busy(self) -> bool:
        prep = self._prep
        return prep is not None and prep.state() != QProcess.ProcessState.NotRunning

    def _open_board_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open board", "", BOARD_FILTER)
        if path:
            self.open_board(path)

    def _open_odb_folder_dialog(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Open ODB++ folder", "")
        if path:
            self.open_board(path)

    def _open_project_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open project", "", "PCB simulator project (*.pcbsim.json)")
        if path:
            self.open_project(path)

    def open_board(self, path: str | Path) -> None:
        if self._prep_busy():
            self.statusBar().showMessage("Wait for the mesh build to finish before opening another board.")
            return
        path = Path(path)
        try:
            board = load_board(path)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Could not open board", str(exc))
            return
        self._save_quiet = True
        try:
            self._load_opened_board(path, board)
        finally:
            self._save_quiet = False
        self._remember_saved_project()
        self._refresh_save_status()

    def _load_opened_board(self, path: Path, board) -> None:
        project_path = project_file_for(path)
        if project_path.is_file():
            project = load_project(project_path)
            apply_stackup(board.stackup, project.stackup)
            assign_stackup_z(board.stackup)
        else:
            project = Project(
                board=path.name,
                reference_net=default_reference_net(board.net_names()),
                mode="crop",
                eps_eff=default_eps_eff(board.top_eps()),
            )
            project.stackup = stackup_payload(board.stackup)
        clear_trace_cache()
        self._probe_spec = None
        self.view.clear_measurements()
        self.view.set_probe([])
        self.board = board
        self.board_path = path
        self.project = project
        self.mesh = None
        self.results.clear()
        self.view.set_overlay(None)
        self._pdn_result = None
        self._two_click = ("armed",) if self.view._tool == "port" else None
        self._part_anchor = None
        self._terminal_anchor = None
        self.view.set_two_click(self.view._tool == "port")
        self.setWindowTitle(f"PCB Simulator — {path.name}")
        self._fill_from_project()
        self.view.set_board(board)
        self.view.set_ports(project.ports)
        self._refresh_region()
        self._log_board_notes()
        self._show_saved_results()
        self._show_saved_pdn()

    def open_project(self, path: str | Path) -> None:
        if self._prep_busy():
            self.statusBar().showMessage("Wait for the mesh build to finish before opening another board.")
            return
        path = Path(path)
        try:
            project = load_project(path)
            board_path = board_path_for(path, project)
            if not board_path.exists():
                raise FileNotFoundError(f"Board file not found: {board_path}")
            board = load_board(board_path)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Could not open project", str(exc))
            return
        self._save_quiet = True
        try:
            self._load_opened_project(board_path, board, project)
        finally:
            self._save_quiet = False
        self._remember_saved_project()
        self._refresh_save_status()

    def _load_opened_project(self, board_path: Path, board, project: Project) -> None:
        apply_stackup(board.stackup, project.stackup)
        assign_stackup_z(board.stackup)
        clear_trace_cache()
        self._probe_spec = None
        self.view.clear_measurements()
        self.view.set_probe([])
        self.board = board
        self.board_path = board_path
        self.project = project
        self.mesh = None
        self.results.clear()
        self.view.set_overlay(None)
        self._pdn_result = None
        self._part_anchor = None
        self._terminal_anchor = None
        self.setWindowTitle(f"PCB Simulator — {board_path.name}")
        self._fill_from_project()
        self.view.set_board(board)
        self.view.set_ports(project.ports)
        self._refresh_region()
        self._log_board_notes()
        self._show_saved_results()
        self._show_saved_pdn()

    def _show_saved_results(self) -> None:
        """Show the last finished run, so a relaunch does not throw away the plot."""
        if self.board_path is None:
            return
        path = self.board_path.parent / ".sim" / "current" / "results.npz"
        if not path.is_file():
            return
        try:
            results = load_results(path)
        except (OSError, ValueError, KeyError):
            return
        self._job_dir = path.parent
        eps = self.project.eps_eff or default_eps_eff(self.board.top_eps() if self.board else 4.5)
        self.results.show_results(results, eps)
        self._announce_decay()
        self.statusBar().showMessage("Showing the last saved results.")

    def _show_saved_pdn(self) -> None:
        """Reload a finished PDN next to the S-parameter results. A launch with no board stays empty."""
        if self.board_path is None:
            return
        result = load_result(pdn_dir(self.board_path))
        if result is None:
            return
        self._pdn_result = result
        self.results.show_pdn(result, switch=False)

    def save_project(self) -> None:
        if self.board is None or self.board_path is None:
            return
        self._pull_widgets()
        self._stamp_board_identity()
        path = project_file_for(self.board_path)
        save_project(path, self.project)
        self._saved_fingerprint = project_fingerprint(self.project)
        self._refresh_save_status()
        self.statusBar().showMessage(f"Saved {path.name}")

    def _stamp_board_identity(self) -> None:
        """Save writes the board file name and the stackup table, even when those widgets did not move."""
        if self.board is None or self.board_path is None:
            return
        self.project.board = self.board_path.name
        self.project.stackup = stackup_payload(self.board.stackup)

    def _remember_saved_project(self) -> None:
        """Treat the window, after it has loaded, as the saved copy when a project file is already there."""
        if self.board_path is None or not project_file_for(self.board_path).is_file():
            self._saved_fingerprint = None
            return
        self._pull_widgets()
        self._stamp_board_identity()
        self._saved_fingerprint = project_fingerprint(self.project)

    def _refresh_save_status(self) -> None:
        if self._save_quiet or self._filling or self._checking_save:
            return
        if self.board is None or self.board_path is None:
            self._show_save_status(None)
            return
        self._checking_save = True
        try:
            self._pull_widgets()
            self._stamp_board_identity()
            current = project_fingerprint(self.project)
        finally:
            self._checking_save = False
        saved = self._saved_fingerprint
        self._show_save_status("unsaved" if saved is None or current != saved else "saved")

    def _show_save_status(self, state: str | None) -> None:
        if state == "saved" and self.board_path is not None:
            title = f"PCB Simulator — {self.board_path.name}"
            short = "Saved"
            note = "Saved. Ctrl+S writes the .pcbsim.json beside the board. The KiCad file is not modified."
        elif state == "unsaved" and self.board_path is not None:
            title = f"PCB Simulator — {self.board_path.name}*"
            short = "Unsaved"
            note = "Unsaved changes. Ctrl+S writes the .pcbsim.json beside the board. The KiCad file is not modified."
        else:
            title = "PCB Simulator"
            short = ""
            note = "Open a board or a project. Save writes the .pcbsim.json beside the board. The KiCad file is not modified."
        self.setWindowTitle(title)
        self.save_state.setText(short)
        label = getattr(self, "file_save_state", None)
        if label is not None:
            label.setText(note)

    def _log_board_notes(self) -> None:
        """Put the board's own notes at the top of the solver log."""
        self._clear_board_log()
        if self.board is None:
            return
        if self.board.assumed_stackup:
            notes = [
                "This board has no stackup. Assuming 1.6 mm FR-4 (εr 4.5, tanδ 0.02) and 35 µm copper. "
                "Edit the table if that is not the stack you want to simulate."
            ]
        else:
            notes = list(self.board.warnings[:4])
        if not notes:
            return
        self._board_log_lines = self._prepend_log("\n".join(notes), "board")

    def _fill_from_project(self) -> None:
        self._filling = True
        project = self.project
        board = self.board
        self.mode_crop.setChecked(project.mode != "outline")
        self.mode_outline.setChecked(project.mode == "outline")
        self.margin.setValue(project.margin_mm)
        self.f_max.setValue(project.f_max_hz / 1e9)
        self.n_freq.setValue(project.n_freq)
        self.end_criteria.setValue(project.end_criteria)
        self.nr_ts.setValue(project.nr_ts)
        self.cells_per_feature.setValue(project.cells_per_feature)
        self.fine_mesh.setChecked(project.cells_per_feature == 4)
        self.pdn_f_max.setValue(project.pdn_f_max_hz / 1e9)
        self.pdn_n_freq.setValue(project.pdn_n_freq)
        self.pdn_signal_mesh.setChecked(bool(project.pdn_signal_mesh))
        self.threads.setValue(0 if not project.threads else int(project.threads))
        boundary = self.boundary.findData(project.boundary)
        self.boundary.setCurrentIndex(boundary if boundary >= 0 else 0)
        stop = self.stop_mode.findData(project.stop)
        self.stop_mode.setCurrentIndex(stop if stop >= 0 else 0)
        self.signal_only.setChecked(bool(project.signal_only))
        self._rebuild_layers()
        self._rebuild_nets()
        self._rebuild_stack()
        self._rebuild_ports()
        if project.eps_eff:
            self.results.eps.setValue(float(project.eps_eff))
        elif board is not None:
            self.results.eps.setValue(default_eps_eff(board.top_eps()))
        self.conductivity.setText(f"{project.conductivity:.6g}")
        self.plating.setValue(project.plating_mm)
        self.pitch.setValue(max(0.2, float(project.pitch_mm)))
        if project.vrm is not None:
            self.vrm_v.setValue(project.vrm.volts)
            self.vrm_r.setValue(project.vrm.r_ohm)
            self.vrm_l.setValue(project.vrm.l_nh)
        self._rebuild_ac_layers()
        self._filling = False
        self._refresh_pdn_lists()

    def _rebuild_layers(self) -> None:
        while self.layer_box.count():
            item = self.layer_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if self.board is None:
            shelf = getattr(self, "_shelf", None)
            if shelf is not None:
                shelf._place_panel()
            return
        for name in self.board.copper_names_top_to_bottom():
            box = QCheckBox(name)
            box.setChecked(True)
            _flex_width(box)
            box.toggled.connect(self._on_layers)
            self.layer_box.addWidget(box)
        edge = QCheckBox("Edge.Cuts")
        edge.setChecked(True)
        _flex_width(edge)
        edge.toggled.connect(self._on_layers)
        self.layer_box.addWidget(edge)
        self.layer_box.addStretch(1)
        shelf = getattr(self, "_shelf", None)
        if shelf is not None:
            shelf._place_panel()

    def _rebuild_nets(self) -> None:
        self.net_list.blockSignals(True)
        self.net_list.clear()
        names = self.board.net_names() if self.board else []
        selected = set(self.project.selected_nets)
        for name in names:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if name in selected else Qt.Unchecked)
            self.net_list.addItem(item)
        if names and self.project.reference_net not in names:
            self.project.reference_net = default_reference_net(names)
        self.reference.blockSignals(True)
        self.reference.clear()
        self.reference.addItems(names)
        if self.project.reference_net in names:
            self.reference.setCurrentText(self.project.reference_net)
        self.reference.blockSignals(False)
        self.power_net.blockSignals(True)
        self.power_net.clear()
        self.power_net.addItem("")
        self.power_net.addItems(names)
        chosen = self.project.power_net if self.project.power_net in names else suggest_power_net(names)
        if chosen:
            self.power_net.setCurrentText(chosen)
            self.project.power_net = chosen
        self.power_net.blockSignals(False)
        self.net_list.blockSignals(False)

    def _rebuild_ac_layers(self) -> None:
        names = self.board.copper_names_top_to_bottom() if self.board else []
        for combo, saved in (
            (self.ac_power, self.project.ac_power_layer),
            (self.ac_ground, self.project.ac_ground_layer),
        ):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Auto", "")
            for name in names:
                combo.addItem(name, name)
            index = combo.findData(saved)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    def _rebuild_stack(self) -> None:
        self.stack_table.blockSignals(True)
        layers = self.board.stackup if self.board else []
        self.stack_table.setRowCount(len(layers))
        for row, layer in enumerate(layers):
            name = QTableWidgetItem(layer.name)
            name.setFlags(name.flags() & ~Qt.ItemIsEditable)
            kind = QTableWidgetItem(layer.kind)
            kind.setFlags(kind.flags() & ~Qt.ItemIsEditable)
            thickness = QTableWidgetItem(f"{layer.thickness_mm:.4g}")
            eps = QTableWidgetItem("" if layer.eps_r is None else f"{layer.eps_r:.4g}")
            tand = QTableWidgetItem("" if layer.loss_tangent is None else f"{layer.loss_tangent:.4g}")
            if layer.kind != "dielectric":
                eps.setFlags(eps.flags() & ~Qt.ItemIsEditable)
                tand.setFlags(tand.flags() & ~Qt.ItemIsEditable)
            self.stack_table.setItem(row, 0, name)
            self.stack_table.setItem(row, 1, kind)
            self.stack_table.setItem(row, 2, thickness)
            self.stack_table.setItem(row, 3, eps)
            self.stack_table.setItem(row, 4, tand)
        self.stack_table.blockSignals(False)

    def _rebuild_ports(self) -> None:
        row = self.port_list.currentRow()
        self.port_list.blockSignals(True)
        self.port_list.clear()
        for port in self.project.ports:
            net = port.net or "no net"
            ohms = f"{port.r_ohm:.1f} Ω"
            pad = f"{port.footprint}.{port.pad}" if port.footprint else ""
            if pad and port.reference_net:
                text = f"{port.name}   {pad}   {net} to {port.reference_net}   {ohms}"
            elif pad:
                text = f"{port.name}   {pad}   {net}   {ohms}"
            elif port.reference_net:
                text = f"{port.name}   {net} to {port.reference_net}   {ohms}"
            else:
                text = f"{port.name}   {net}   {ohms}"
            self.port_list.addItem(text)
        self.port_list.blockSignals(False)
        if 0 <= row < self.port_list.count():
            self.port_list.setCurrentRow(row)
        self._refresh_pair_widgets()

    def _refresh_pair_widgets(self) -> None:
        names = [port.name for port in self.project.ports]
        self.project.pairs = [
            pair for pair in self.project.pairs if pair.positive in names and pair.negative in names
        ]
        for combo in (self.pair_pos, self.pair_neg):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(names)
            if current in names:
                combo.setCurrentText(current)
            combo.blockSignals(False)
        if self.pair_neg.count() > 1 and self.pair_neg.currentText() == self.pair_pos.currentText():
            positive = self.pair_pos.currentText()
            for index in range(self.pair_neg.count()):
                if self.pair_neg.itemText(index) != positive:
                    self.pair_neg.setCurrentIndex(index)
                    break
        row = self.pair_list.currentRow()
        self.pair_list.clear()
        for pair in self.project.pairs:
            self.pair_list.addItem(f"{pair.name}   {pair.positive} − {pair.negative}")
        if 0 <= row < self.pair_list.count():
            self.pair_list.setCurrentRow(row)
        self.results.set_pairs(self.project.pairs)
        self._refresh_save_status()

    def add_differential_pair(self, positive: str, negative: str) -> str | None:
        """Record a pair of existing ports. Returns an error message, or None when it was added."""
        if not positive or not negative:
            return "Choose both legs of the pair."
        if positive == negative:
            return "A differential pair needs two different ports."
        names = {port.name for port in self.project.ports}
        if positive not in names or negative not in names:
            return "Both legs have to be ports on this board."
        for pair in self.project.pairs:
            if {pair.positive, pair.negative} == {positive, negative}:
                return f"{pair.name} already uses {positive} and {negative}."
        used = {pair.name for pair in self.project.pairs}
        number = 1
        while f"D{number}" in used:
            number += 1
        self.project.pairs.append(DiffPair(f"D{number}", positive, negative))
        self._refresh_pair_widgets()
        return None

    def _on_add_pair(self) -> None:
        message = self.add_differential_pair(self.pair_pos.currentText(), self.pair_neg.currentText())
        if message:
            self.statusBar().showMessage(message)
            return
        pair = self.project.pairs[-1]
        self.statusBar().showMessage(
            f"Added {pair.name}. Differential impedance uses the S matrix from the ports; it does not start a new solve."
        )

    def _delete_pair(self) -> None:
        row = self.pair_list.currentRow()
        if row < 0 or row >= len(self.project.pairs):
            return
        name = self.project.pairs[row].name
        del self.project.pairs[row]
        self._refresh_pair_widgets()
        self.statusBar().showMessage(f"Removed {name}.")

    def _pull_widgets(self) -> None:
        self.project.mode = "outline" if self.mode_outline.isChecked() else "crop"
        self.project.margin_mm = float(self.margin.value())
        self.project.f_max_hz = float(self.f_max.value()) * 1e9
        self.project.n_freq = int(self.n_freq.value())
        self.project.end_criteria = float(self.end_criteria.value())
        self.project.nr_ts = int(self.nr_ts.value())
        self.project.cells_per_feature = int(self.cells_per_feature.value())
        self.project.pdn_f_max_hz = float(self.pdn_f_max.value()) * 1e9
        self.project.pdn_n_freq = int(self.pdn_n_freq.value())
        self.project.pdn_signal_mesh = self.pdn_signal_mesh.isChecked()
        self.project.threads = None if self.threads.value() == 0 else int(self.threads.value())
        self.project.boundary = str(self.boundary.currentData() or "mur")
        self.project.stop = str(self.stop_mode.currentData() or "crossing")
        self.project.signal_only = self.signal_only.isChecked()
        self.project.selected_nets = self._checked_nets()
        self.project.eps_eff = float(self.results.eps.value())
        self.project.conductivity = _positive_float(self.conductivity.text(), self.project.conductivity)
        self.project.plating_mm = float(self.plating.value())
        self.project.pitch_mm = float(self.pitch.value())
        self.project.power_net = self.power_net.currentText()
        self.project.ac_power_layer = str(self.ac_power.currentData() or "")
        self.project.ac_ground_layer = str(self.ac_ground.currentData() or "")

    def _checked_nets(self) -> list[str]:
        names = []
        for row in range(self.net_list.count()):
            item = self.net_list.item(row)
            if item.checkState() == Qt.Checked:
                names.append(item.text())
        return names

    def _on_layers(self) -> None:
        names = set()
        for index in range(self.layer_box.count()):
            widget = self.layer_box.itemAt(index).widget()
            if isinstance(widget, QCheckBox) and widget.isChecked() and widget.text() != "Edge.Cuts":
                names.add(widget.text())
        edge_on = True
        for index in range(self.layer_box.count()):
            widget = self.layer_box.itemAt(index).widget()
            if isinstance(widget, QCheckBox) and widget.text() == "Edge.Cuts":
                edge_on = widget.isChecked()
        self.view.show_outline = edge_on
        self.view.set_visible_layers(names)

    def _on_nets_changed(self, _item) -> None:
        if self._filling:
            return
        self.project.selected_nets = self._checked_nets()
        self._refresh_region()

    def _on_net_clicked(self, item: QListWidgetItem) -> None:
        self._highlight_net(item.text())

    def _highlight_net(self, net: str) -> None:
        self.view.set_highlight("" if self.view.highlight_net == net else net)

    def _on_reference(self, name: str) -> None:
        if self._filling or not name:
            return
        self.project.reference_net = name
        self._refresh_save_status()

    def _on_mode(self, _checked: bool) -> None:
        if self._filling:
            return
        self.project.mode = "outline" if self.mode_outline.isChecked() else "crop"
        self._refresh_region()

    def _on_margin(self, value: float) -> None:
        if self._filling:
            return
        self.project.margin_mm = float(value)
        self._refresh_region()

    def _on_fine_mesh(self, checked: bool) -> None:
        if self._filling:
            return
        if checked:
            self.cells_per_feature.setValue(4)
        elif int(self.cells_per_feature.value()) == 4:
            self.cells_per_feature.setValue(2)

    def _on_signal_only(self, _checked: bool) -> None:
        if self._filling:
            return
        if self.board is None:
            self._pull_widgets()
            self._refresh_save_status()
            return
        self._refresh_region()

    def _on_solver_fields(self, *_args) -> None:
        if self._filling:
            return
        self.fine_mesh.blockSignals(True)
        self.fine_mesh.setChecked(int(self.cells_per_feature.value()) == 4)
        self.fine_mesh.blockSignals(False)
        self._pull_widgets()
        self._refresh_save_status()

    def _on_eps_eff(self, value: float) -> None:
        self.project.eps_eff = float(value)
        self._refresh_save_status()
        if self._probe_spec is not None:
            self._on_tdr_probe(self._probe_spec)

    def _on_measure(self, x0: float, y0: float, x1: float, y1: float) -> None:
        straight = math.dist((x0, y0), (x1, y1))
        route = None
        net = ""
        if self.board is not None:
            first = self.view.trace_net_at(x0, y0)
            second = self.view.trace_net_at(x1, y1)
            reference = self.project.reference_net
            if first and first == second and first != reference:
                net = first
                route = trace_route(self.board, net, (x0, y0), (x1, y1))
        self.port_list.blockSignals(True)
        self.port_list.setCurrentRow(-1)
        self.port_list.blockSignals(False)
        if route is not None:
            length, path = route
            self.view.add_measurement(path, f"{length:.2f} mm along {net}")
            self.statusBar().showMessage(f"{length:.2f} mm along {net}. {straight:.2f} mm straight.")
            return
        self.view.add_measurement([(x0, y0), (x1, y1)], f"{straight:.2f} mm")
        self.statusBar().showMessage(f"{straight:.2f} mm straight.")

    def _on_tdr_probe(self, spec) -> None:
        self._probe_spec = spec
        if not spec or self.board is None:
            self.view.set_probe([])
            return
        time_s = float(spec.get("time_s") or 0.0)
        if time_s <= 0:
            self.view.set_probe([])
            return
        eps = float(self.results.eps.value())
        distance = float(distance_m([time_s], eps)[0]) * 1e3
        marks = []
        for port in self._probe_ports(spec):
            if not port.net:
                continue
            found = trace_point(
                self.board,
                port.net,
                ((port.start[0] + port.stop[0]) / 2, (port.start[1] + port.stop[1]) / 2),
                distance,
            )
            if found is None:
                continue
            point, clamped = found
            caption = f"{distance:.1f} mm"
            if clamped:
                caption = f"end of {port.net}"
            marks.append((point[0], point[1], caption))
        self.view.set_probe(marks)

    def _probe_ports(self, spec) -> list:
        choice = spec.get("choice")
        index = int(spec.get("index") or 0)
        if choice == "diff":
            if index < 0 or index >= len(self.project.pairs):
                return []
            pair = self.project.pairs[index]
            wanted = {pair.positive, pair.negative}
            return [port for port in self.project.ports if port.name in wanted]
        names = getattr(self.results, "_names", None) or []
        if 0 <= index < len(names):
            name = names[index]
            match = [port for port in self.project.ports if port.name == name]
            if match:
                return match
        if 0 <= index < len(self.project.ports):
            return [self.project.ports[index]]
        return []

    def _on_crop_rect(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.project.rect = (float(x0), float(y0), float(x1), float(y1))
        self.project.mode = "crop"
        self._filling = True
        self.mode_crop.setChecked(True)
        self._filling = False
        self._refresh_region()
        self.statusBar().showMessage(
            f"Crop {min(x0, x1):.2f},{min(y0, y1):.2f} to {max(x0, x1):.2f},{max(y0, y1):.2f} mm, plus {self.margin.value():.2f} mm"
        )

    def _clear_rect(self) -> None:
        self.project.rect = None
        self._refresh_region()

    def _auto_crop(self) -> None:
        if self.board is None:
            self.statusBar().showMessage("Open a board before cropping.")
            return
        self._pull_widgets()
        names = auto_crop_nets(
            [port.net for port in self.project.ports],
            self.project.selected_nets,
            self._reference_names(),
        )
        if not names:
            self.statusBar().showMessage(
                "Auto crop needs a port on a signal net, or a checked net other than the reference."
            )
            return
        extra: list[tuple[float, float]] = []
        chosen = set(names)
        for port in self.project.ports:
            if port.net not in chosen:
                continue
            extra.append((port.start[0], port.start[1]))
            extra.append((port.stop[0], port.stop[1]))
            if port.marks is not None:
                extra.extend(port.marks)
        bounds = bounds_of_nets(self.board, chosen, extra)
        if bounds is None:
            self.statusBar().showMessage(f"No copper found for {', '.join(names)}.")
            return
        x0, y0, x1, y1 = bounds
        self.project.rect = bounds
        self.project.mode = "crop"
        self._filling = True
        self.mode_crop.setChecked(True)
        self._filling = False
        self._refresh_region()
        self.statusBar().showMessage(
            f"Crop around {', '.join(names)}: {min(x0, x1):.2f},{min(y0, y1):.2f} to "
            f"{max(x0, x1):.2f},{max(y0, y1):.2f} mm, plus {self.margin.value():.2f} mm. "
            "The reference plane stays inside that box."
        )

    def _refresh_region(self) -> None:
        if self.board is None:
            return
        self._pull_widgets()
        region = region_polygon(self.board, self.project.crop_settings())
        ring = region_ring(region) if not region.is_empty else None
        self.view.set_region(ring)
        self.view.set_ports(self.project.ports)
        self._refresh_save_status()

    def _on_stackup_edited(self, _item) -> None:
        if self._filling or self.board is None:
            return
        old = {layer.name: layer.z_mm for layer in self.board.stackup if layer.kind == "copper"}
        for row, layer in enumerate(self.board.stackup):
            thickness = self.stack_table.item(row, 2)
            eps = self.stack_table.item(row, 3)
            tand = self.stack_table.item(row, 4)
            if thickness and thickness.text().strip():
                try:
                    layer.thickness_mm = float(thickness.text())
                except ValueError:
                    pass
            if layer.kind == "dielectric":
                layer.eps_r = _optional_float(eps.text() if eps else "")
                layer.loss_tangent = _optional_float(tand.text() if tand else "")
        assign_stackup_z(self.board.stackup)
        self._retarget_port_z(old)
        self.project.stackup = stackup_payload(self.board.stackup)
        if self.project.eps_eff is None:
            self.results.eps.setValue(default_eps_eff(self.board.top_eps()))
        self.view.set_ports(self.project.ports)
        self._refresh_save_status()

    def _retarget_port_z(self, old_z: dict) -> None:
        mapping: dict[float, float] = {}
        for layer in self.board.stackup:
            if layer.kind != "copper" or layer.z_mm is None:
                continue
            previous = old_z.get(layer.name)
            if previous is not None:
                mapping[round(float(previous), 6)] = float(layer.z_mm)
        for port in self.project.ports:
            start = list(port.start)
            stop = list(port.stop)
            for corner in (start, stop):
                key = round(float(corner[2]), 6)
                if key in mapping:
                    corner[2] = mapping[key]
            port.start = (start[0], start[1], start[2])
            port.stop = (stop[0], stop[1], stop[2])

    def _on_pad_clicked(self, pad) -> None:
        tool = self.view._tool
        if tool in {"vrm", "load"}:
            layer = pad.layers[0] if pad.layers else ""
            self._click_supply(tool, float(pad.center[0]), float(pad.center[1]), layer, pad.net, pad.footprint, pad.name)
            return
        if tool == "part":
            self._place_part_from_pad(pad)
            return
        if tool == "port" or self.view._two_click_mode:
            return

    def _on_copper_clicked(self, x: float, y: float, layer: str) -> None:
        if self.view._tool in {"vrm", "load"}:
            self._click_supply(self.view._tool, x, y, layer, self.view.net_at(x, y))
            return
        if self.view._tool == "part":
            self._placement_failed("Click a pad.")
            return
        if self._two_click is None:
            self._placement_failed("Choose the port tool, then click the signal copper and its reference.")
            return
        if self._two_click == ("armed",):
            net = self.view.net_at(x, y)
            if not layer or not net:
                self.view._clear_port_preview()
                self._placement_failed("Click copper for the signal net.")
                return
            self._two_click = (x, y, layer)
            self.statusBar().showMessage(f"Port on {layer}, {net}. Click the reference net.")
            return
        x0, y0, layer0 = self._two_click
        port, reason = port_at_click(
            self.board,
            (x0, y0, layer0),
            (x, y, layer),
            self.project.ports,
            r_ohm=float(self.port_r.value()),
        )
        if port is None:
            self._placement_failed(reason)
            return
        self._two_click = ("armed",)
        self.view._clear_port_preview()
        self._label_port_pad(port, x0, y0)
        self._note_port_reference(port)
        self._add_port(port)

    def _label_port_pad(self, port, x: float, y: float) -> None:
        pad = self.view.pad_at(x, y)
        if pad is None or not pad.net or pad.net != port.net:
            return
        port.footprint = pad.footprint
        port.pad = pad.name

    def _reference_names(self) -> list[str]:
        names: list[str] = []
        if self.project.reference_net:
            names.append(self.project.reference_net)
        for port in self.project.ports:
            if port.reference_net and port.reference_net not in names:
                names.append(port.reference_net)
        return names

    def _pdn_ground_placed(self) -> bool:
        vrm = self.project.vrm
        if vrm is not None and vrm.gx is not None:
            return True
        return any(load.gx is not None for load in self.project.loads)

    def _return_locked(self) -> str:
        """The return a later ground click has to match. Empty until a click chooses one.

        A project that still says GND only because that is the default does not lock the click.
        """
        port_refs = [port.reference_net for port in self.project.ports if port.reference_net]
        if not port_refs and not self._pdn_ground_placed():
            return ""
        if self.project.reference_net and (self.project.reference_net in port_refs or self._pdn_ground_placed()):
            return self.project.reference_net
        return port_refs[0] if port_refs else self.project.reference_net

    def _note_port_reference(self, port) -> None:
        if self._pdn_ground_placed() or not port.reference_net:
            return
        self.project.reference_net = port.reference_net

    def port_every_pad(self) -> None:
        if self.board is None:
            return
        ref = self.project.reference_net
        if not ref:
            self._placement_failed("Place one port and click its reference first.")
            return
        added = 0
        for pad in self.board.pads:
            if not pad.net or pad.net == ref:
                continue
            if any(port.footprint == pad.footprint and port.pad == pad.name for port in self.project.ports):
                continue
            port = vertical_port_on_pad(
                self.board, pad, ref, self.project.ports, r_ohm=float(self.port_r.value())
            )
            if port is None:
                continue
            self.project.ports.append(port)
            added += 1
        self._rebuild_ports()
        self.view.set_ports(self.project.ports)
        self.statusBar().showMessage(f"Added {added} port{'s' if added != 1 else ''}.")

    def _add_port(self, port) -> None:
        self.project.ports.append(port)
        self._rebuild_ports()
        self.view.set_ports(self.project.ports)
        self.port_list.setCurrentRow(len(self.project.ports) - 1)
        ref = f" to {port.reference_net}" if port.reference_net else ""
        self.statusBar().showMessage(f"Added {port.name} on {port.net or 'no net'}{ref}.")

    def _delete_port(self) -> None:
        row = self.port_list.currentRow()
        if row < 0 or row >= len(self.project.ports):
            return
        name = self.project.ports[row].name
        del self.project.ports[row]
        self._rebuild_ports()
        self.view.set_ports(self.project.ports)
        self.statusBar().showMessage(f"Removed {name}.")

    def _on_port_selected(self, row: int) -> None:
        if row < 0 or row >= len(self.project.ports):
            if self.view.selected() is not None and self.view.selected()[0] == "port":
                self.view.set_selected(None)
            return
        self.view.set_selected(("port", row))
        self.port_r.blockSignals(True)
        self.port_r.setValue(self.project.ports[row].r_ohm)
        self.port_r.blockSignals(False)

    def _on_component_picked(self, hit) -> None:
        if hit is None:
            self.port_list.blockSignals(True)
            self.port_list.setCurrentRow(-1)
            self.port_list.blockSignals(False)
            self.view.set_selected(None)
            return
        if hit[0] == "measure":
            index = int(hit[1])
            marks = self.view.measurements()
            if index < 0 or index >= len(marks):
                return
            self.port_list.blockSignals(True)
            self.port_list.setCurrentRow(-1)
            self.port_list.blockSignals(False)
            self.view.set_selected(("measure", index))
            self.statusBar().showMessage(f"{marks[index][1]}. Delete removes it.")
            return
        index = int(hit[1])
        if index < 0 or index >= len(self.project.ports):
            return
        self.port_list.setCurrentRow(index)
        port = self.project.ports[index]
        self.statusBar().showMessage(f"{port.name} selected. Delete removes it.")

    def _delete_selected(self) -> None:
        if self._gesture_open():
            return
        selected = self.view.selected()
        if selected is not None and selected[0] == "measure":
            index = int(selected[1])
            if 0 <= index < len(self.view.measurements()):
                self.view.remove_measurement(index)
                self.statusBar().showMessage("Removed the measurement.")
            return
        if selected is not None and selected[0] == "port":
            row = int(selected[1])
            if 0 <= row < len(self.project.ports):
                self.port_list.setCurrentRow(row)
            self._delete_port()
            return
        self._delete_port()

    def _on_port_r(self, value: float) -> None:
        row = self.port_list.currentRow()
        if row < 0 or row >= len(self.project.ports):
            return
        self.project.ports[row].r_ohm = float(value)
        self._rebuild_ports()

    def _crop(self) -> CropSettings:
        self._pull_widgets()
        return self.project.crop_settings()

    def _mesh_settings(self) -> MeshSettings:
        self._pull_widgets()
        return self.project.mesh_settings()

    def preview_mesh(self) -> Mesh | None:
        if self.board is None:
            return None
        try:
            cropped = crop_board(self.board, self._crop())
            mesh = build_mesh(self.board, cropped, list(self.project.ports), self._mesh_settings())
        except Exception as exc:
            self.preflight.setText(str(exc))
            return None
        self.mesh = mesh
        self.view.set_mesh(mesh)
        self._show_preflight(mesh, [])
        return mesh

    def _show_preflight(self, mesh: Mesh, warnings: list[str]) -> None:
        nx, ny, nz = mesh.cells
        mb = mesh.ram_bytes_rough() / 1e6
        text = f"{nx} × {ny} × {nz} = {mesh.cell_count:,} cells. Rough memory {mb:.0f} MB."
        if mesh.dx_max_mm > mesh.dx_mm * 1.05:
            text += f" Cells grade from {mesh.dx_mm:.3f} mm on copper to {mesh.dx_max_mm:.2f} mm in open space."
        elif mesh.dx_mm > 0:
            text += f" Cell size {mesh.dx_mm:.3f} mm."
        warn_at, _confirm_at = self._cell_limits()
        if mesh.cell_count > warn_at:
            text += " This is a large mesh."
        if warnings:
            text += " " + " ".join(warnings[:4])
        self.preflight.setText(text)

    def _cell_limits(self) -> tuple[int, int]:
        settings = _settings()
        return (
            int(settings.value("warn_cells", WARN_CELLS_DEFAULT)),
            int(settings.value("confirm_cells", CONFIRM_CELLS_DEFAULT)),
        )

    def _refresh_pdn_lists(self) -> None:
        load_row = self.load_list.currentRow()
        part_row = self.part_list.currentRow()
        self.vrm_list.clear()
        if self.project.vrm is not None:
            vrm = self.project.vrm
            self.vrm_list.addItem(
                f"{vrm.name}  {_terminal_where(vrm)}{vrm.net}  {vrm.volts:g} V  {vrm.r_ohm:g} Ω  {vrm.l_nh:g} nH"
            )
        self.load_list.blockSignals(True)
        self.load_list.clear()
        for load in self.project.loads:
            self.load_list.addItem(f"{load.name}  {_terminal_where(load)}{load.net}  {load.amps:g} A")
        if 0 <= load_row < self.load_list.count():
            self.load_list.setCurrentRow(load_row)
        self.load_list.blockSignals(False)
        self.part_list.blockSignals(True)
        self.part_list.clear()
        for part in self.project.parts:
            bits = [part.name]
            if part.r_ohm:
                bits.append(f"{part.r_ohm:g} Ω")
            if part.l_nh:
                bits.append(f"{part.l_nh:g} nH")
            if part.c_nf:
                bits.append(f"{part.c_nf:g} nF")
            self.part_list.addItem("  ".join(bits))
        if 0 <= part_row < self.part_list.count():
            self.part_list.setCurrentRow(part_row)
        self.part_list.blockSignals(False)
        self.view.set_pdn(self.project.vrm, self.project.loads, self.project.parts)
        self._refresh_save_status()

    def _on_vrm_fields(self, *_args) -> None:
        if self._filling or self.project.vrm is None:
            return
        self.project.vrm.volts = float(self.vrm_v.value())
        self.project.vrm.r_ohm = float(self.vrm_r.value())
        self.project.vrm.l_nh = float(self.vrm_l.value())
        self._refresh_pdn_lists()

    def _on_load_selected(self, row: int) -> None:
        if self._filling or row < 0 or row >= len(self.project.loads):
            return
        self._filling = True
        self.load_i.setValue(self.project.loads[row].amps)
        self._filling = False

    def _on_load_current(self, value: float) -> None:
        if self._filling:
            return
        row = self.load_list.currentRow()
        if row < 0 or row >= len(self.project.loads):
            return
        self.project.loads[row].amps = float(value)
        self._refresh_pdn_lists()

    def _on_part_selected(self, row: int) -> None:
        if self._filling or row < 0 or row >= len(self.project.parts):
            return
        part = self.project.parts[row]
        self._filling = True
        self.part_name.setText(part.name)
        self.part_r.setValue(0.0 if part.r_ohm is None else part.r_ohm)
        self.part_l.setValue(0.0 if part.l_nh is None else part.l_nh)
        self.part_c.setValue(0.0 if part.c_nf is None else part.c_nf)
        self._filling = False

    def _on_part_fields(self, *_args) -> None:
        if self._filling:
            return
        row = self.part_list.currentRow()
        if row < 0 or row >= len(self.project.parts):
            return
        part = self.project.parts[row]
        typed = self.part_name.text().strip()
        if typed:
            part.name = typed
        part.r_ohm = _blank_zero(self.part_r.value())
        part.l_nh = _blank_zero(self.part_l.value())
        part.c_nf = _blank_zero(self.part_c.value())
        self._refresh_pdn_lists()

    def _delete_vrm(self) -> None:
        self.project.vrm = None
        self._refresh_pdn_lists()
        self.statusBar().showMessage("Removed the VRM.")

    def _delete_load(self) -> None:
        row = self.load_list.currentRow()
        if row < 0 or row >= len(self.project.loads):
            return
        name = self.project.loads[row].name
        del self.project.loads[row]
        self._refresh_pdn_lists()
        self.statusBar().showMessage(f"Removed {name}.")

    def _delete_part(self) -> None:
        row = self.part_list.currentRow()
        if row < 0 or row >= len(self.project.parts):
            return
        name = self.project.parts[row].name
        del self.project.parts[row]
        self._refresh_pdn_lists()
        self.statusBar().showMessage(f"Removed {name}.")

    def _click_supply(self, kind: str, x: float, y: float, layer: str, net: str, footprint: str = "", pad: str = "") -> None:
        """First click is power. Second click is ground, the same way a two-click port picks both ends."""
        if self.board is None:
            return
        anchor = self._terminal_anchor
        if anchor is not None and anchor.get("kind") != kind:
            anchor = None
            self._terminal_anchor = None
            self.view.set_two_click(False)
        if anchor is None:
            self._arm_supply(kind, x, y, layer, net, footprint, pad)
            return
        if footprint and footprint == anchor["footprint"] and pad and pad == anchor["pad"]:
            self._placement_failed("Click the ground pad, not the same pad.")
            return
        if not footprint and abs(x - anchor["x"]) < 0.05 and abs(y - anchor["y"]) < 0.05:
            self._placement_failed("Click the ground, not the same point.")
            return
        if not net:
            self._placement_failed("Click the ground on copper.")
            return
        ref = self._return_locked()
        if ref and net != ref:
            self._placement_failed(f"That is {net}. Ground has to be {ref}.")
            return
        if not ref:
            self.project.reference_net = net
        self._terminal_anchor = None
        self.view.set_two_click(False)
        if kind == "vrm":
            self._commit_vrm(anchor, x, y, layer, footprint, pad)
        else:
            self._commit_load(anchor, x, y, layer, footprint, pad)

    def _arm_supply(self, kind: str, x: float, y: float, layer: str, net: str, footprint: str, pad: str) -> None:
        ref = self._return_locked()
        if ref and net == ref:
            self._placement_failed(f"That is {ref}. Click the power pad first, then the ground.")
            return
        if not net:
            self._placement_failed("Click the power pad first.")
            return
        self._terminal_anchor = {
            "kind": kind,
            "x": x,
            "y": y,
            "layer": layer,
            "net": net,
            "footprint": footprint,
            "pad": pad,
        }
        self.view.begin_span_preview(x, y)
        where = _pad_where(footprint, pad, x, y)
        if kind == "vrm":
            self.statusBar().showMessage(
                f"Power at {where} on {net}. Click the ground pad. Voltage is {self.vrm_v.value():g} V. Escape stops this."
            )
        else:
            self.statusBar().showMessage(
                f"Power at {where} on {net}. Click the ground pad. Current is {self.load_i.value():g} A. Escape stops this."
            )
        if net in [self.power_net.itemText(i) for i in range(self.power_net.count())] and not self.power_net.currentText():
            self.power_net.setCurrentText(net)
            self.project.power_net = net

    def _commit_vrm(self, power: dict, gx: float, gy: float, g_layer: str, g_footprint: str, g_pad: str) -> None:
        self.project.vrm = PdnTerminalRecord(
            name="VRM",
            x=power["x"],
            y=power["y"],
            net=power["net"],
            layer=power["layer"],
            footprint=power["footprint"],
            pad=power["pad"],
            volts=float(self.vrm_v.value()),
            amps=0.0,
            r_ohm=float(self.vrm_r.value()),
            l_nh=float(self.vrm_l.value()),
            gx=gx,
            gy=gy,
            g_layer=g_layer,
            g_footprint=g_footprint,
            g_pad=g_pad,
        )
        self._refresh_pdn_lists()
        ground = _pad_where(g_footprint, g_pad, gx, gy)
        self.statusBar().showMessage(
            f"VRM from {_pad_where(power['footprint'], power['pad'], power['x'], power['y'])} to {ground} at {self.vrm_v.value():g} V."
        )

    def _commit_load(self, power: dict, gx: float, gy: float, g_layer: str, g_footprint: str, g_pad: str) -> None:
        self.project.loads = [
            item
            for item in self.project.loads
            if not (power["footprint"] and item.footprint == power["footprint"] and item.pad == power["pad"])
        ]
        used = {item.name for item in self.project.loads}
        number = 1
        while f"I{number}" in used:
            number += 1
        self.project.loads.append(
            PdnTerminalRecord(
                name=f"I{number}",
                x=power["x"],
                y=power["y"],
                net=power["net"],
                layer=power["layer"],
                footprint=power["footprint"],
                pad=power["pad"],
                volts=0.0,
                amps=float(self.load_i.value()),
                gx=gx,
                gy=gy,
                g_layer=g_layer,
                g_footprint=g_footprint,
                g_pad=g_pad,
            )
        )
        self._refresh_pdn_lists()
        self.load_list.setCurrentRow(len(self.project.loads) - 1)
        ground = _pad_where(g_footprint, g_pad, gx, gy)
        self.statusBar().showMessage(
            f"Load I{number} from {_pad_where(power['footprint'], power['pad'], power['x'], power['y'])} to {ground}, {self.load_i.value():g} A."
        )

    def _place_part_from_pad(self, pad) -> None:
        if self.board is None:
            return
        mates = [item for item in self.board.pads if item.footprint and item.footprint == pad.footprint]
        if len(mates) == 2:
            self.view.set_two_click(False)
            self._part_anchor = None
            self._commit_part(mates[0], mates[1])
            return
        if self._part_anchor is None or _same_pad(self._part_anchor, pad):
            self._part_anchor = pad
            self.view.begin_span_preview(float(pad.center[0]), float(pad.center[1]))
            self.statusBar().showMessage(f"First pad {pad.footprint}.{pad.name}. Click the other pad.")
            return
        first = self._part_anchor
        self._part_anchor = None
        self.view.set_two_click(False)
        self._commit_part(first, pad)

    def _commit_part(self, first, second) -> None:
        r_ohm = _blank_zero(self.part_r.value())
        l_nh = _blank_zero(self.part_l.value())
        c_nf = _blank_zero(self.part_c.value())
        name = self.part_name.text().strip() or _next_part_name(self.project.parts, r_ohm, l_nh, c_nf)
        self.project.parts = [item for item in self.project.parts if item.name != name]
        self.project.parts.append(
            PdnPartRecord(
                name=name,
                ax=float(first.center[0]),
                ay=float(first.center[1]),
                net_a=first.net,
                bx=float(second.center[0]),
                by=float(second.center[1]),
                net_b=second.net,
                layer_a=first.layers[0] if first.layers else "",
                layer_b=second.layers[0] if second.layers else "",
                footprint=first.footprint if first.footprint and first.footprint == second.footprint else "",
                r_ohm=r_ohm,
                l_nh=l_nh,
                c_nf=c_nf,
            )
        )
        self._refresh_pdn_lists()
        self.statusBar().showMessage(f"Placed {name}. It does not change the copper, and the S-parameter run ignores it.")

    def _on_overlay(self, mode: str) -> None:
        result = self._pdn_result
        if not mode or result is None:
            self.view.set_overlay(None)
            return
        cells = result.voltages if mode == "voltage" else result.density
        self.view.set_overlay(cells or None)

    def run_pdn(self) -> None:
        """DC IR drop and the AC sweep on one grid. Leaves ``.sim/current`` alone."""
        if self.board is None or self.board_path is None:
            self.statusBar().showMessage("Open a board before running the PDN.")
            return
        if self._pdn_thread is not None and self._pdn_thread.isRunning():
            self.statusBar().showMessage("A PDN solve is already running.")
            return
        field_busy = self._process is not None or self._prep_busy()
        if field_busy and self._job_kind == "pdn":
            self.statusBar().showMessage("A PDN field run is already going.")
            return
        self._want_field = not field_busy
        if field_busy:
            self._log_line("An S-parameter run is still going. This click solves the DC drop only.", "app")
        self._pull_widgets()
        if self.project.vrm is None:
            self.statusBar().showMessage("Place a VRM on the power net.")
            return
        if not self.project.loads:
            self.statusBar().showMessage("Place at least one load.")
            return
        if not self.project.power_net:
            self.statusBar().showMessage("Choose the power net on the Board tab.")
            return
        if not self.project.reference_net:
            self.statusBar().showMessage("Click the VRM ground to set the reference net.")
            return
        vrm, loads, parts = self._pdn_terminals()
        self.pdn_btn.setEnabled(False)
        self._log_line("Solving the PDN grid…", "app")
        self.statusBar().showMessage("Solving the PDN grid.")
        thread = _PdnThread(
            self.board,
            self._crop(),
            self.project.power_net,
            self.project.reference_net,
            self.project.pitch_mm,
            self.project.conductivity,
            self.project.plating_mm,
            self.project.ac_power_layer,
            self.project.ac_ground_layer,
            vrm,
            loads,
            parts,
            None,
            None,
        )
        thread.finished_ok.connect(self._on_pdn_done)
        self._pdn_thread = thread
        thread.start()

    def _pdn_terminals(self):
        record = self.project.vrm
        vrm = PdnTerminal(
            record.name,
            record.x,
            record.y,
            record.net,
            layer=record.layer,
            volts=record.volts,
            amps=0.0,
            r_ohm=record.r_ohm,
            l_nh=record.l_nh,
            gx=record.gx,
            gy=record.gy,
            g_layer=record.g_layer,
        )
        loads = [
            PdnTerminal(
                item.name,
                item.x,
                item.y,
                item.net,
                layer=item.layer,
                volts=0.0,
                amps=item.amps,
                gx=item.gx,
                gy=item.gy,
                g_layer=item.g_layer,
            )
            for item in self.project.loads
        ]
        parts = [
            PdnPart(
                item.name,
                item.ax,
                item.ay,
                item.net_a,
                item.bx,
                item.by,
                item.net_b,
                r_ohm=item.r_ohm,
                l_nh=item.l_nh,
                c_nf=item.c_nf,
                layer_a=item.layer_a,
                layer_b=item.layer_b,
            )
            for item in self.project.parts
        ]
        return vrm, loads, parts

    def _on_pdn_done(self, result) -> None:
        thread = self._pdn_thread
        self._pdn_thread = None
        if thread is not None:
            thread.wait(1000)
        self.pdn_btn.setEnabled(True)
        self._pdn_result = result
        for note in result.warnings:
            self._log_line(note, "board")
        self._log_line(result.message, "app" if result.ok else "error")
        if result.ok and self.board_path is not None:
            folder = pdn_dir(self.board_path)
            save_result(folder, result)
            self._log_line(f"Wrote {folder}", "app")
        self.results.show_pdn(result, switch=True)
        connected = next((drop for drop in result.drops if drop.get("connected") and drop.get("loop_mv") is not None), None)
        if connected is not None:
            self.statusBar().showMessage(f"{connected['name']} loop drop {connected['loop_mv']:.2f} mV.")
        else:
            self.statusBar().showMessage(result.message)
        start_field = result.ok and self._want_field and self._process is None and not self._prep_busy()
        self._want_field = False
        if start_field:
            self._start_pdn_field()

    def _start_pdn_field(self) -> None:
        if self.board is None or self.board_path is None:
            return
        if self._process is not None or self._prep_busy():
            self._log_line("The field run waits. Another openEMS job is still going.", "app")
            return
        vrm, loads, parts = self._pdn_terminals()
        job_dir = pdn_em_dir(self.board_path)
        self._log_line("Building the PDN field mesh…", "app")
        self._begin_prep(
            {
                "kind": "pdn",
                "board": self.board,
                "crop": self._crop(),
                "power_net": self.project.power_net,
                "reference_net": self.project.reference_net,
                "vrm": vrm,
                "loads": loads,
                "parts": parts,
                "conductivity": self.project.conductivity,
                "mesh_settings": pdn_mesh_settings(self.project.pdn_f_max_hz, self.project.pdn_signal_mesh),
                "n_freq": self.project.pdn_n_freq,
                "end_criteria": self.project.end_criteria,
                "nr_ts": self.project.nr_ts,
                "threads": self.project.threads,
                "job_dir": str(job_dir),
            },
            job_dir,
            "pdn",
        )

    def run_simulation(self) -> None:
        if self.board is None or self.board_path is None:
            return
        if self._process is not None or self._prep_busy():
            self.statusBar().showMessage("A simulation is already running.")
            return
        job_dir = self.board_path.parent / ".sim" / "current"
        self._log_line("Building the mesh…", "app")
        self._begin_prep(
            {
                "board": self.board,
                "crop": self._crop(),
                "ports": list(self.project.ports),
                "mesh_settings": self._mesh_settings(),
                "n_freq": self.project.n_freq,
                "end_criteria": self.project.end_criteria,
                "nr_ts": self.project.nr_ts,
                "threads": self.project.threads,
                "stop": self.project.stop,
                "job_dir": str(job_dir),
            },
            job_dir,
            "si",
        )

    def _begin_prep(self, request: dict, job_dir: Path, kind: str) -> None:
        solver = str(_settings().value("solver_python", "") or "").strip() or _guess_python()
        self._job_kind = kind
        self._solver = solver
        self._job_dir = job_dir
        self.run_btn.setEnabled(False)
        if kind == "pdn":
            self.pdn_btn.setEnabled(False)
        self._set_cancel_enabled(True)
        self.preflight.setText("Building the mesh…")
        self.statusBar().showMessage("Building the mesh. Pan and scroll stay available. The run uses the board as it was when you clicked Run.")
        self._prep_text = ""
        self._prep_buf = ""
        QApplication.processEvents()
        request_path = job_dir.parent / "prep_request.pkl"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_bytes(pickle.dumps(request, protocol=pickle.HIGHEST_PROTOCOL))
        self._prep_request = request_path
        prep = QProcess(self)
        prep.setProcessEnvironment(_child_env())
        prep.setWorkingDirectory(str(job_dir.parent))
        prep.setProgram(solver)
        prep.setArguments(["-m", "pcb_simulator.sim.prepare_worker", str(request_path)])
        prep.readyReadStandardOutput.connect(self._read_prep_out)
        prep.readyReadStandardError.connect(self._read_prep_err)
        prep.finished.connect(self._on_prepared)
        prep.errorOccurred.connect(self._on_prep_error)
        self._prep = prep
        prep.start()

    def _read_prep_out(self) -> None:
        prep = self._prep
        if prep is None:
            return
        self._take_prep(bytes(prep.readAllStandardOutput()).decode("utf-8", "replace"))

    def _read_prep_err(self) -> None:
        prep = self._prep
        if prep is None:
            return
        self._take_prep(bytes(prep.readAllStandardError()).decode("utf-8", "replace"))

    def _take_prep(self, chunk: str) -> None:
        if not chunk:
            return
        self._prep_buf += chunk.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in self._prep_buf:
            line, self._prep_buf = self._prep_buf.split("\n", 1)
            self._prep_text += line + "\n"
            self._show_prep_line(line.rstrip())

    def _show_prep_line(self, line: str) -> None:
        if not line.startswith("PCB_PREP "):
            if line:
                self._log_line(line)
            return
        try:
            payload = json.loads(line[len("PCB_PREP ") :])
        except json.JSONDecodeError:
            self._log_line(line)
            return
        text = describe_prep(payload)
        if not text or payload.get("event") == "error":
            return
        self._log_line(text)
        self.preflight.setText(text)
        self.statusBar().showMessage(text)

    def _on_prep_error(self, error) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        self._prep = None
        self._unlock_after_prep()
        self._log_line("The solver interpreter did not start. Set it on the Settings tab.", "error")
        self.statusBar().showMessage("The solver interpreter did not start.")

    def _unlock_after_prep(self) -> None:
        self.view.setEnabled(True)
        self.run_btn.setEnabled(True)
        self.pdn_btn.setEnabled(True)
        self._set_cancel_enabled(False)

    def _on_prepared(self, exit_code: int, _status) -> None:
        prep = self._prep
        if prep is not None:
            self._take_prep(bytes(prep.readAllStandardOutput()).decode("utf-8", "replace"))
            self._take_prep(bytes(prep.readAllStandardError()).decode("utf-8", "replace"))
        if self._prep_buf.strip():
            leftover = self._prep_buf
            self._prep_buf = ""
            self._prep_text += leftover
            self._show_prep_line(leftover.rstrip("\n"))
        self._prep = None
        self.view.setEnabled(True)
        cancelled = self._prep_cancelled
        self._prep_cancelled = False
        if cancelled:
            self._unlock_after_prep()
            self.statusBar().showMessage("Mesh build cancelled.")
            if self._prep_request is not None:
                self._prep_request.unlink(missing_ok=True)
                self._prep_request = None
            return
        text = self._prep_text
        if self._prep_request is not None:
            self._prep_request.unlink(missing_ok=True)
            self._prep_request = None
        prep_path = self._job_dir / "prep.pkl" if self._job_dir is not None else None
        if exit_code != 0 or prep_path is None or not prep_path.is_file():
            self._unlock_after_prep()
            message = prep_failure_message(text, exit_code)
            self.preflight.setText(message)
            self._log_line(message, "error")
            self.statusBar().showMessage(message)
            return
        loaded = pickle.loads(prep_path.read_bytes())
        mesh = loaded["mesh"]
        warnings = list(loaded["warnings"])
        self._show_preflight(mesh, warnings)
        _warn, confirm = self._cell_limits()
        if mesh.cell_count > confirm:
            answer = QMessageBox.question(
                self,
                "Large mesh",
                f"This mesh has {mesh.cell_count:,} cells. Rough field memory is {mesh.ram_bytes_rough() / 1e6:.0f} MB. "
                "openEMS allocates the full operator before compressing it, so the peak is higher than that figure. Run it anyway?",
            )
            if answer != QMessageBox.Yes:
                self._unlock_after_prep()
                if self._job_dir is not None and self._job_dir.exists():
                    shutil.rmtree(self._job_dir, ignore_errors=True)
                return
        self.mesh = mesh
        self.view.set_mesh(mesh)
        self.run_btn.setEnabled(False)
        self._start_process(self._solver, self._job_dir)

    def _start_process(self, solver: str, job_dir: Path) -> None:
        proc = QProcess(self)
        proc.setProcessEnvironment(_child_env())
        proc.setWorkingDirectory(str(job_dir))
        proc.setProgram(solver)
        proc.setArguments(["-m", "pcb_simulator.sim.worker", str(job_dir)])
        proc.readyReadStandardOutput.connect(self._read_output)
        proc.readyReadStandardError.connect(self._read_error)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_process_error)
        self._process = proc
        self._stdout_buf = ""
        self.run_btn.setEnabled(False)
        self._set_cancel_enabled(True)
        self._log_line(f"Starting {solver} -m pcb_simulator.sim.worker", "app")
        proc.start()

    def _on_process_error(self, error) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        self._process = None
        self.run_btn.setEnabled(True)
        self.pdn_btn.setEnabled(True)
        self._set_cancel_enabled(False)
        self._log_line("The solver interpreter did not start. Set it on the Settings tab.", "error")
        self.statusBar().showMessage("The solver interpreter did not start.")

    def _read_output(self) -> None:
        if self._process is None:
            return
        chunk = bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace")
        # openEMS rewrites a progress line with carriage returns. Treat each
        # rewrite as its own line so the buffer cannot grow without bound.
        self._stdout_buf += chunk.replace("\r\n", "\n").replace("\r", "\n")
        if len(self._stdout_buf) > 1_000_000:
            self._stdout_buf = self._stdout_buf[-200_000:]
        while "\n" in self._stdout_buf:
            line, self._stdout_buf = self._stdout_buf.split("\n", 1)
            self._handle_line(line.rstrip())

    def _read_error(self) -> None:
        if self._process is None:
            return
        text = bytes(self._process.readAllStandardError()).decode("utf-8", "replace").rstrip()
        if text:
            self._log_line(text)

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        if not line.startswith("PCB_SIM "):
            self._log_line(line)
            return
        try:
            payload = json.loads(line[len("PCB_SIM ") :])
        except json.JSONDecodeError:
            self._log_line(line)
            return
        event = payload.get("event")
        sentence = describe_sim(payload)
        if sentence:
            self._log_line(sentence)
            self.preflight.setText(sentence)
            self.statusBar().showMessage(sentence)
            return
        if event == "start":
            cells = payload.get("cells", [])
            self.statusBar().showMessage(f"Solving {cells} cells, {payload.get('ports')} ports")
        elif event == "excite":
            self.statusBar().showMessage(
                f"Excitation {int(payload.get('index', 0)) + 1}/{payload.get('count')} ({payload.get('port')})"
            )
        elif event == "error":
            self._log_line(payload.get("message", "solver error"), "error")
        elif event == "done":
            self.statusBar().showMessage("Solver finished.")
        self._log_line(line)

    def _on_finished(self, exit_code: int, _status) -> None:
        self._read_output()
        self._process = None
        self.run_btn.setEnabled(True)
        self.pdn_btn.setEnabled(True)
        self._set_cancel_enabled(False)
        if exit_code == 2:
            self._log_line(SOLVER_HELP, "error")
            self.statusBar().showMessage("openEMS is not importable in the solver Python.")
            return
        if exit_code != 0 or self._job_dir is None:
            message = solver_exit_message(exit_code)
            self._log_line(message, "error")
            self.statusBar().showMessage(message)
            return
        result_path = self._job_dir / "results.npz"
        if not result_path.is_file():
            self.statusBar().showMessage("The solver finished without results.npz.")
            return
        results = load_results(result_path)
        if self._job_kind == "pdn":
            self._load_pdn_field(results, result_path.parent)
            return
        eps = self.project.eps_eff or (default_eps_eff(self.board.top_eps()) if self.board else default_eps_eff(4.5))
        self.results.show_results(results, eps)
        self._announce_decay()
        self.statusBar().showMessage("Results loaded.")

    def _announce_decay(self) -> None:
        """The undecayed-run notice goes in the solver log. The results tab does not keep it."""
        notice = self.results.decay_notice
        if notice:
            self._log_line(notice, "error")

    def _load_pdn_field(self, results: dict, directory: Path) -> None:
        result = self._pdn_result
        if result is None:
            self.statusBar().showMessage("The field run finished without a DC result.")
            return
        job = load_job(directory / "job.json")
        result.field_freq_hz = results["freq"]
        result.field_impedance = field_impedance(
            results["freq"],
            results["S"],
            results["z0"],
            list(results["port_names"]),
            list(job.get("load_ports") or []),
            list(job.get("stamps") or []),
        )
        result.field_decayed = bool(results.get("decayed", True))
        if not result.field_decayed:
            note = "The field run hit the timestep cap before the energy limit. The impedance curve is unfinished."
            if note not in result.warnings:
                result.warnings.append(note)
        if self.board_path is not None:
            save_result(pdn_dir(self.board_path), result)
        self.results.show_pdn(result, switch=True)
        if result.field_decayed:
            self.statusBar().showMessage("Field impedance loaded.")
        else:
            self.statusBar().showMessage("Field impedance loaded. The run did not decay.")

    def _set_cancel_enabled(self, enabled: bool) -> None:
        self.cancel_btn.setEnabled(enabled)
        for button in self._extra_cancels:
            button.setEnabled(enabled)

    def cancel_simulation(self) -> None:
        if self._prep is not None and self._prep.state() != QProcess.ProcessState.NotRunning:
            self._prep_cancelled = True
            self._prep.kill()
            self.statusBar().showMessage("Mesh build cancelled.")
            return
        if self._process is not None:
            self._process.kill()
            self.statusBar().showMessage("Cancel requested.")

    def export_touchstone(self) -> None:
        ntwk = self.results.simulated_network()
        if ntwk is None:
            self.statusBar().showMessage("There are no results to export.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Touchstone", "board.s2p", "Touchstone (*.s1p *.s2p *.s3p *.s4p *.snp)")
        if not path:
            return
        write_touchstone(ntwk, path)
        self.statusBar().showMessage(f"Wrote {path}. The file keeps the simulated port impedance.")

    def export_csv(self) -> None:
        ntwk = self.results.simulated_network()
        if ntwk is None:
            self.statusBar().showMessage("There are no results to export.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", "results.csv", "CSV (*.csv)")
        if not path:
            return
        names = list(self.results._names)
        pairs = differential_export(ntwk, self.project.pairs, names)
        write_csv(path, ntwk, names, pairs, self.results._excitations)
        self.statusBar().showMessage(f"Wrote {path}. The file keeps the simulated port impedance.")

    def export_pdn_csv(self) -> None:
        if self._pdn_result is None:
            self.statusBar().showMessage("There is no PDN result to export.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export PDN CSV", "pdn.csv", "CSV (*.csv)")
        if not path:
            return
        write_pdn_csv(path, self._pdn_result)
        self.statusBar().showMessage(f"Wrote {path}.")

    def _solver_dialog(self) -> None:
        SolverDialog(self).exec()


def _pad_where(footprint: str, pad: str, x: float, y: float) -> str:
    if footprint and pad:
        return f"{footprint}.{pad}"
    if footprint:
        return footprint
    return f"{x:.2f}, {y:.2f} mm"


def _terminal_where(item) -> str:
    power = f"{item.footprint}.{item.pad}  " if item.footprint else ""
    if item.gx is None:
        return power
    ground = f"{item.g_footprint}.{item.g_pad}" if item.g_footprint else f"{item.gx:.2f},{item.gy:.2f}"
    return f"{power}→ {ground}  "


def _positive_float(text: str, fallback: float) -> float:
    try:
        value = float(text)
    except ValueError:
        return fallback
    if value <= 0:
        return fallback
    return value


def _blank_zero(value: float) -> float | None:
    number = float(value)
    if number == 0:
        return None
    return number


def _same_pad(first, second) -> bool:
    return first.footprint == second.footprint and first.name == second.name and first.center == second.center


def _next_part_name(parts, r_ohm, l_nh, c_nf) -> str:
    prefix = "C" if c_nf else "L" if l_nh and not r_ohm else "R"
    used = {part.name for part in parts}
    number = 1
    while f"{prefix}{number}" in used:
        number += 1
    return f"{prefix}{number}"


def _optional_float(text: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def solver_exit_message(exit_code: int) -> str:
    """Explain a native solver abort. -1073740791 is Windows 0xC0000409."""
    code = exit_code & 0xFFFFFFFF
    if code == 0xC0000409:
        return (
            "openEMS aborted while building the FDTD operator "
            f"(Windows 0xC0000409, exit {exit_code}). "
            "The log stops at \"Multithreaded operator using N threads\" because the next step "
            "allocates the operator for the whole mesh. On a large mesh that process usually runs out of memory. "
            "Crop a smaller region or lower cells per feature. "
            "The S-parameter f max does not change the cell count. "
            "Threads set to 1 checks whether the multi-thread operator setup is the part that dies."
        )
    if exit_code < 0 or code >= 0xC0000000:
        return (
            f"openEMS crashed with Windows code {exit_code} (0x{code:08X}). "
            "The solver process died inside the native library."
        )
    return f"Solver exited with code {exit_code}."


def _guess_python() -> str:
    import sys

    return sys.executable


def _child_env() -> QProcessEnvironment:
    env = QProcessEnvironment.systemEnvironment()
    root = str(Path(__file__).resolve().parents[1])
    previous = env.value("PYTHONPATH")
    env.insert("PYTHONPATH", root if not previous else root + os.pathsep + previous)
    return env
