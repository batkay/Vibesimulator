"""Desktop window: open a board, attach lumped ports, run openEMS."""

from __future__ import annotations

import json
import os
import pickle
import shutil
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, QSettings, QSize, Qt
from PySide6.QtGui import QAction, QKeySequence
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
from .model.crop import CropSettings, crop_board, region_polygon, region_ring
from .model.load import BOARD_FILTER, load_board
from .model.mesh import Mesh, MeshSettings, build_mesh
from .model.ports import port_between, vertical_port_on_pad
from .project import DiffPair, Project, apply_stackup, board_path_for, load_project, save_project, stackup_payload

from .sim.postprocess import default_eps_eff, differential_export, load_results, write_csv, write_touchstone
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
        self._filling = False
        self._splitter: QSplitter | None = None
        self._splitter_dragged = False
        self._applying_sizes = False

        self.view = BoardView()
        self.results = ResultsView()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.preflight = FitLabel("Open a KiCad board, an IPC-2581 file, or an ODB++ folder or archive.")
        self._build_panels()
        self._build_menus()
        self.view.status_text.connect(self.statusBar().showMessage)
        self.view.crop_rect.connect(self._on_crop_rect)
        self.view.pad_clicked.connect(self._on_pad_clicked)
        self.view.copper_clicked.connect(self._on_copper_clicked)
        self.view.net_picked.connect(self._highlight_net)
        self.results.eps_eff_changed.connect(self._on_eps_eff)
        self.results.export_csv_requested.connect(self.export_csv)
        self.statusBar().showMessage("Open a KiCad, IPC-2581, or ODB++ board to start.")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_pane_sizes()

    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        if not self._applying_sizes:
            self._splitter_dragged = True

    def _apply_pane_sizes(self) -> None:
        """Give the controls and the results about 22% each until the user drags a handle."""
        splitter = self._splitter
        if splitter is None or self._splitter_dragged or self._applying_sizes:
            return
        width = splitter.width()
        if width < 100:
            return
        usable = max(width - splitter.handleWidth() * 2, 1)
        left = int(usable * 0.22)
        right = int(usable * 0.22)
        center = usable - left - right
        if center < 200:
            spare = max(usable - 200, 0)
            left = spare // 2
            right = spare - left
            center = usable - left - right
        self._applying_sizes = True
        splitter.setSizes([left, center, right])
        self._applying_sizes = False

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        open_board = QAction("Open board…", self)
        open_board.setShortcut(QKeySequence.Open)
        open_board.triggered.connect(self._open_board_dialog)
        open_folder = QAction("Open ODB++ folder…", self)
        open_folder.triggered.connect(self._open_odb_folder_dialog)
        open_project = QAction("Open project…", self)
        open_project.triggered.connect(self._open_project_dialog)
        save = QAction("Save project", self)
        save.setShortcut(QKeySequence.Save)
        save.triggered.connect(self.save_project)
        touch = QAction("Export Touchstone…", self)
        touch.triggered.connect(self.export_touchstone)
        csv = QAction("Export CSV…", self)
        csv.triggered.connect(self.export_csv)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(open_board)
        file_menu.addAction(open_folder)
        file_menu.addAction(open_project)
        file_menu.addAction(save)
        file_menu.addSeparator()
        file_menu.addAction(touch)
        file_menu.addAction(csv)
        file_menu.addSeparator()
        file_menu.addAction(quit_action)

        sim_menu = self.menuBar().addMenu("Simulation")
        preview = QAction("Preview mesh", self)
        preview.triggered.connect(self.preview_mesh)
        run = QAction("Run", self)
        run.setShortcut(QKeySequence("Ctrl+R"))
        run.triggered.connect(self.run_simulation)
        cancel = QAction("Cancel run", self)
        cancel.triggered.connect(self.cancel_simulation)
        sim_menu.addAction(preview)
        sim_menu.addAction(run)
        sim_menu.addAction(cancel)

        settings_menu = self.menuBar().addMenu("Settings")
        solver = QAction("Solver Python…", self)
        solver.triggered.connect(self._solver_dialog)
        settings_menu.addAction(solver)

    def _build_panels(self) -> None:
        self.banner = FitLabel("")
        self.banner.setStyleSheet("color: #8a5a00;")
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
            "Loss tangent",
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
        self.pair_list.setMaximumHeight(88)
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
            "Lower coarsens the mesh in open space. The copper lattice does not change. "
            "Dielectric loss is matched at half this frequency."
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
            "A finer mesh has a shorter timestep, so the same cap covers less time."
        )
        self.nr_ts.valueChanged.connect(self._on_solver_fields)
        self.cells_per_feature = QSpinBox()
        self.cells_per_feature.setRange(1, 30)
        self.cells_per_feature.setValue(2)
        self.cells_per_feature.setToolTip(
            "Copper lattice, 0.4 mm divided by this count, and never finer than 0.05 mm. "
            "1 is 0.4 mm and the fastest. 2 is the default 0.2 mm lattice, about 0.1 mm of width error. "
            "4 is 0.1 mm. 8 and above stay at 0.05 mm. Open space stays at a tenth of a wavelength."
        )
        self.cells_per_feature.valueChanged.connect(self._on_solver_fields)
        self.threads = QSpinBox()
        self.threads.setRange(0, 128)
        self.threads.setSpecialValueText("auto")
        self.threads.setToolTip(
            "auto uses every core for the timestep loop. The loop often levels off around 4–8 cores. "
            "The mesh and the result stay the same."
        )
        self.threads.valueChanged.connect(self._on_solver_fields)
        preview = QPushButton("Preview mesh")
        preview.clicked.connect(self.preview_mesh)
        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(self.run_simulation)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_simulation)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.banner)
        left_layout.addWidget(self._group("Layers", self.layer_box))
        net_layout = QVBoxLayout()
        net_note = FitLabel("Checked nets are simulated. The reference net is always kept.")
        net_layout.addWidget(
            note_with_info(
                net_note,
                "Checked nets",
                "Checked nets are simulated. The reference net is always kept. "
                "With nothing checked, every net in the region is kept.",
            )
        )
        net_layout.addWidget(self.net_list, 1)
        ref_row = QHBoxLayout()
        ref_row.addWidget(QLabel("Reference"))
        ref_row.addWidget(
            with_info(
                self.reference,
                "Reference",
                "One-click ports run down to this net. It stays in the crop even when it is not checked.",
            ),
            1,
        )
        net_layout.addLayout(ref_row)
        left_layout.addWidget(self._group("Nets", net_layout))
        stack_note = FitLabel(
            "Copper thickness is stored and is not meshed. Each copper layer is a perfect-conductor sheet "
            "on one z plane, so sidewall capacitance is missing. On a 0.1 mm prepreg that can move a "
            "narrow trace by several ohms. Trace width, dielectric height, and εr are what the solver uses."
        )
        stack_wrap = QVBoxLayout()
        stack_wrap.addWidget(
            note_with_info(
                stack_note,
                "Stackup",
                "Copper thickness is stored and is not meshed, so sidewall capacitance is missing. "
                "εr and dielectric height set the impedance. tanδ is dielectric loss, matched at f max / 2.",
            )
        )
        stack_wrap.addWidget(self.stack_table)
        left_layout.addWidget(self._group("Stackup", stack_wrap))
        region = QVBoxLayout()
        region.addLayout(
            _beside(
                self.mode_crop,
                info_button(
                    "Crop",
                    "The mesh is the rectangle you draw, plus the margin, plus an air border. "
                    "This is the usual way to keep a run small.",
                ),
            )
        )
        region.addLayout(
            _beside(
                self.mode_outline,
                info_button(
                    "Whole outline",
                    "Meshes the whole board outline. With no rectangle and no checked nets, crop mode does this too.",
                ),
            )
        )
        region.addWidget(FitLabel("Crop is the default. With no rectangle and no checked nets, the whole outline is used."))
        margin_row = QHBoxLayout()
        margin_row.addWidget(QLabel("Margin"))
        margin_row.addWidget(
            with_info(
                self.margin,
                "Margin",
                "Extra board kept around the crop, clipped to the outline. "
                "It holds the ground plane and dielectric next to the trace. "
                "The absorbing boundary sits farther out than this.",
            ),
            1,
        )
        region.addLayout(margin_row)
        region.addWidget(crop_btn)
        region.addWidget(clear_crop)
        left_layout.addWidget(self._group("Region", region))
        ports = QVBoxLayout()
        ports.addWidget(FitLabel("One click places a vertical port on a pad that has reference copper under it."))
        r_row = QHBoxLayout()
        r_row.addWidget(QLabel("Port R"))
        r_row.addWidget(
            with_info(
                self.port_r,
                "Port R",
                "Termination of the lumped port, the same role as a VNA or TDR. It is not the trace impedance.",
            ),
            1,
        )
        ports.addLayout(r_row)
        ports.addWidget(nav_btn)
        ports.addLayout(
            _beside(
                pad_btn,
                info_button(
                    "Click a pad",
                    "Places a vertical port on that pad, down to the reference net. Each pad you click gets its own port.",
                ),
            )
        )
        ports.addLayout(
            _beside(
                two_btn,
                info_button(
                    "Two clicks",
                    "The line runs from the first click to the second, and the ends stay on those clicks. "
                    "A second click on another layer keeps the sheet on the first click and picks the layer.",
                ),
            )
        )
        ports.addLayout(
            _beside(
                all_btn,
                info_button(
                    "Port every pad",
                    "Puts a vertical port on every pad that is not the reference net. Each port is its own solve.",
                ),
            )
        )
        ports.addWidget(self.port_list, 1)
        ports.addWidget(delete_btn)
        pair_row = QHBoxLayout()
        pair_row.addWidget(self.pair_pos, 1)
        pair_row.addWidget(QLabel("−"))
        pair_row.addWidget(
            with_info(
                self.pair_neg,
                "Differential pair",
                "Positive leg, then negative leg. This uses the S matrix already computed and does not rerun the solver.",
            ),
            1,
        )
        pair_note = FitLabel("Differential pair: positive leg, then negative leg. This does not rerun the solver.")
        ports.addWidget(pair_note)
        ports.addLayout(pair_row)
        ports.addWidget(add_pair)
        ports.addWidget(self.pair_list)
        ports.addWidget(delete_pair)
        left_layout.addWidget(self._group("Ports", ports))
        solve = QFormLayout()
        solve.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        solve.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        solve.addRow(
            FitLabel("f max"),
            with_info(
                self.f_max,
                "f max",
                "Highest frequency of the source and of the plots. The TDR rise scales as 1 / f max, "
                "so 10 GHz is about half the edge of 5 GHz. On the ohms plot the 10–90% time is about 0.23 ns at 5 GHz, "
                "about 20 mm on the distance axis at εeff 2.75. The v(t) tab shows the Gaussian source. "
                "Integrating that pulse gives a step of about 0.17 ns at 5 GHz. "
                "The launch edge stays a few samples wide at any f max, because the rise time shrinks along with the sample spacing. "
                "Samples along the rest of the trace get closer as f max goes up. "
                "Lowering f max coarsens the mesh in open space. The copper lattice does not change. "
                "Dielectric loss is matched at half this frequency.",
            ),
        )
        solve.addRow(
            FitLabel("Frequency points"),
            with_info(
                self.n_freq,
                "Frequency points",
                "How many frequencies S is sampled at after the timestep loop. This barely changes the solve time. "
                "The TDR record is about this count divided by f max long, so fewer points hide a long line. "
                "The rise time follows f max.",
            ),
        )
        solve.addRow(
            FitLabel("Energy limit"),
            with_info(
                self.end_criteria,
                "Energy limit",
                "Stop when the energy left in the volume has fallen by this factor. "
                "A larger number (1e-3) stops sooner and cuts off late reflections. 1e-4 keeps the tail.",
            ),
        )
        solve.addRow(
            FitLabel("Max timesteps"),
            with_info(
                self.nr_ts,
                "Max timesteps",
                "Hard cap on steps. Lowering it does not make a step faster. "
                "A run that hits the cap is marked as not decayed, and late reflections are missing.",
            ),
        )
        solve.addRow(
            FitLabel("Cells / feature"),
            with_info(
                self.cells_per_feature,
                "Cells / feature",
                "Copper lattice, 0.4 mm divided by this count, and never finer than 0.05 mm. "
                "1 is 0.4 mm. 2 is 0.2 mm. 4 is 0.1 mm. Open space stays at a tenth of a wavelength.",
            ),
        )
        solve.addRow(
            FitLabel("Threads"),
            with_info(
                self.threads,
                "Threads",
                "auto uses every core for the timestep loop. The loop often levels off around 4–8 cores. "
                "The mesh and the result stay the same.",
            ),
        )
        solve_wrap = QVBoxLayout()
        solve_wrap.addLayout(solve)
        solve_wrap.addWidget(FitLabel("The solver Python is set under Settings. openEMS is not bundled, and this app does not download it."))
        solve_wrap.addWidget(self.preflight)
        solve_wrap.addWidget(preview)
        solve_wrap.addWidget(self.run_btn)
        solve_wrap.addWidget(self.cancel_btn)
        left_layout.addWidget(self._group("Solver", solve_wrap))
        left_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left)
        scroll.setMinimumWidth(280)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._left_scroll = scroll
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.results.setMinimumWidth(280)
        self.results.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        splitter = QSplitter()
        splitter.addWidget(scroll)
        splitter.addWidget(self.view)
        splitter.addWidget(self.results)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 1)
        splitter.setChildrenCollapsible(False)
        splitter.splitterMoved.connect(self._on_splitter_moved)
        self._splitter = splitter
        self.setCentralWidget(splitter)
        self.addDockWidget(Qt.BottomDockWidgetArea, self._log_dock())

    def _log_dock(self):
        from PySide6.QtWidgets import QDockWidget

        dock = QDockWidget("Solver log", self)
        dock.setWidget(self.log)
        return dock

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
        self.view.set_tool(tool)
        self.view.set_two_click(False)
        if tool == "crop":
            self.statusBar().showMessage("Drag a rectangle on the board. The margin is added around it.")
        elif tool == "port":
            self.statusBar().showMessage("Click a pad to attach a port. Port R is the instrument impedance.")
        else:
            self.statusBar().showMessage("Drag with the middle button to pan. Scroll to zoom toward the cursor.")

    def _arm_two_click(self) -> None:
        self._two_click = None
        self.view.set_tool("port")
        self.view.set_two_click(True)
        self._two_click = ("armed",)
        self.statusBar().showMessage(
            "Click the first conductor. A point marks that click, and the line follows the cursor until the second click. The port stays on that copper."
        )

    def closeEvent(self, event) -> None:
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
        self.board = board
        self.board_path = path
        self.project = project
        self.mesh = None
        self.results.clear()
        self._two_click = None
        self.setWindowTitle(f"PCB Simulator — {path.name}")
        self._fill_from_project()
        self.view.set_board(board)
        self.view.set_ports(project.ports)
        self._refresh_region()
        self._show_banner()
        self._show_saved_results()

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
        apply_stackup(board.stackup, project.stackup)
        assign_stackup_z(board.stackup)
        self.board = board
        self.board_path = board_path
        self.project = project
        self.mesh = None
        self.results.clear()
        self.setWindowTitle(f"PCB Simulator — {board_path.name}")
        self._fill_from_project()
        self.view.set_board(board)
        self.view.set_ports(project.ports)
        self._refresh_region()
        self._show_banner()
        self._show_saved_results()

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
        self.statusBar().showMessage("Showing the last saved results.")

    def save_project(self) -> None:
        if self.board is None or self.board_path is None:
            return
        self._pull_widgets()
        self.project.board = self.board_path.name
        self.project.stackup = stackup_payload(self.board.stackup)
        path = project_file_for(self.board_path)
        save_project(path, self.project)
        self.statusBar().showMessage(f"Saved {path.name}")

    def _show_banner(self) -> None:
        if self.board is not None and self.board.assumed_stackup:
            self.banner.setText(
                "This board has no stackup. Assuming 1.6 mm FR-4 (εr 4.5, tanδ 0.02) and 35 µm copper. "
                "Edit the table if that is not the stack you want to simulate."
            )
        elif self.board is not None and self.board.warnings:
            self.banner.setText(" ".join(self.board.warnings[:4]))
        else:
            self.banner.setText("")

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
        self.threads.setValue(0 if not project.threads else int(project.threads))
        self._rebuild_layers()
        self._rebuild_nets()
        self._rebuild_stack()
        self._rebuild_ports()
        if project.eps_eff:
            self.results.eps.setValue(float(project.eps_eff))
        elif board is not None:
            self.results.eps.setValue(default_eps_eff(board.top_eps()))
        self._filling = False

    def _rebuild_layers(self) -> None:
        while self.layer_box.count():
            item = self.layer_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if self.board is None:
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
        self.reference.blockSignals(True)
        self.reference.clear()
        self.reference.addItems(names)
        if self.project.reference_net in names:
            self.reference.setCurrentText(self.project.reference_net)
        elif names:
            self.reference.setCurrentText(default_reference_net(names))
            self.project.reference_net = self.reference.currentText()
        self.reference.blockSignals(False)
        self.net_list.blockSignals(False)

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
            if port.footprint:
                text = f"{port.name}   {port.footprint}.{port.pad}   {net}   {ohms}"
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
        self.project.threads = None if self.threads.value() == 0 else int(self.threads.value())
        self.project.reference_net = self.reference.currentText()
        self.project.selected_nets = self._checked_nets()
        self.project.eps_eff = float(self.results.eps.value())

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

    def _on_solver_fields(self, *_args) -> None:
        if self._filling:
            return
        self._pull_widgets()

    def _on_eps_eff(self, value: float) -> None:
        self.project.eps_eff = float(value)

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

    def _refresh_region(self) -> None:
        if self.board is None:
            return
        self._pull_widgets()
        region = region_polygon(self.board, self.project.crop_settings())
        ring = region_ring(region) if not region.is_empty else None
        self.view.set_region(ring)
        self.view.set_ports(self.project.ports)

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
        if self.view._two_click_mode:
            return
        if any(port.footprint == pad.footprint and port.pad == pad.name for port in self.project.ports):
            self.statusBar().showMessage(f"{pad.footprint}.{pad.name} already has a port.")
            return
        port = vertical_port_on_pad(
            self.board,
            pad,
            self.reference.currentText(),
            self.project.ports,
            r_ohm=float(self.port_r.value()),
        )
        if port is None:
            self.statusBar().showMessage(
                "No reference copper of the reference net intersects this pad. Use two clicks, or change the reference net."
            )
            return
        self._add_port(port)

    def _on_copper_clicked(self, x: float, y: float, layer: str) -> None:
        if self._two_click is None:
            self.statusBar().showMessage("That click was not on a pad. Choose Click a pad, or use Two clicks.")
            return
        if self._two_click == ("armed",):
            if not layer:
                self.statusBar().showMessage("Click copper for the first side of the port.")
                return
            self._two_click = (x, y, layer)
            net = self.view.net_at(x, y)
            shown = net or "no net"
            self.statusBar().showMessage(
                f"First side on {layer}, {shown}. Click the other conductor. Other pads stay clickable."
            )
            return
        x0, y0, layer0 = self._two_click
        port = port_between(
            self.board,
            (x0, y0, layer0),
            (x, y, layer),
            self.reference.currentText(),
            self.project.ports,
            r_ohm=float(self.port_r.value()),
            signal_net=self.view.net_at(x0, y0),
        )
        self._two_click = ("armed",)
        if port is None:
            self.statusBar().showMessage("Those two clicks did not make a port. Pick two different copper layers, or two points with a gap.")
            return
        self._add_port(port)

    def port_every_pad(self) -> None:
        if self.board is None:
            return
        added = 0
        for pad in self.board.pads:
            if not pad.net or pad.net == self.reference.currentText():
                continue
            if any(port.footprint == pad.footprint and port.pad == pad.name for port in self.project.ports):
                continue
            port = vertical_port_on_pad(
                self.board, pad, self.reference.currentText(), self.project.ports, r_ohm=float(self.port_r.value())
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
        self.statusBar().showMessage(f"Added {port.name} on {port.net or 'no net'}.")

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
            return
        self.port_r.blockSignals(True)
        self.port_r.setValue(self.project.ports[row].r_ohm)
        self.port_r.blockSignals(False)

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

    def run_simulation(self) -> None:
        if self.board is None or self.board_path is None:
            return
        if self._process is not None or self._prep_busy():
            self.statusBar().showMessage("A simulation is already running.")
            return
        solver = str(_settings().value("solver_python", "") or "").strip() or _guess_python()
        job_dir = self.board_path.parent / ".sim" / "current"
        self._solver = solver
        self._job_dir = job_dir
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.preflight.setText("Building the mesh…")
        self.statusBar().showMessage("Building the mesh. Pan and scroll stay available. The run uses the board as it was when you clicked Run.")
        self.log.appendPlainText("Building the mesh…")
        self._prep_text = ""
        self._prep_buf = ""
        QApplication.processEvents()
        request_path = job_dir.parent / "prep_request.pkl"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_bytes(
            pickle.dumps(
                {
                    "board": self.board,
                    "crop": self._crop(),
                    "ports": list(self.project.ports),
                    "mesh_settings": self._mesh_settings(),
                    "n_freq": self.project.n_freq,
                    "end_criteria": self.project.end_criteria,
                    "nr_ts": self.project.nr_ts,
                    "threads": self.project.threads,
                    "job_dir": str(job_dir),
                },
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        )
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
                self.log.appendPlainText(line)
            return
        try:
            payload = json.loads(line[len("PCB_PREP ") :])
        except json.JSONDecodeError:
            self.log.appendPlainText(line)
            return
        text = describe_prep(payload)
        if not text or payload.get("event") == "error":
            return
        self.log.appendPlainText(text)
        self.preflight.setText(text)
        self.statusBar().showMessage(text)

    def _on_prep_error(self, error) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        self._prep = None
        self._unlock_after_prep()
        self.log.appendPlainText("The solver interpreter did not start. Set it under Settings → Solver Python.")
        self.statusBar().showMessage("The solver interpreter did not start.")

    def _unlock_after_prep(self) -> None:
        self._left_scroll.setEnabled(True)
        self.view.setEnabled(True)
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

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
        self._left_scroll.setEnabled(True)
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
            self.log.appendPlainText(message)
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
        self.cancel_btn.setEnabled(True)
        self.log.appendPlainText(f"Starting {solver} -m pcb_simulator.sim.worker")
        proc.start()

    def _on_process_error(self, error) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        self._process = None
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.log.appendPlainText("The solver interpreter did not start. Set it under Settings → Solver Python.")
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
            self.log.appendPlainText(text)

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        if not line.startswith("PCB_SIM "):
            self.log.appendPlainText(line)
            return
        try:
            payload = json.loads(line[len("PCB_SIM ") :])
        except json.JSONDecodeError:
            self.log.appendPlainText(line)
            return
        event = payload.get("event")
        sentence = describe_sim(payload)
        if sentence:
            self.log.appendPlainText(sentence)
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
            self.log.appendPlainText(payload.get("message", "solver error"))
        elif event == "done":
            self.statusBar().showMessage("Solver finished.")
        self.log.appendPlainText(line)

    def _on_finished(self, exit_code: int, _status) -> None:
        self._read_output()
        self._process = None
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if exit_code == 2:
            self.log.appendPlainText(SOLVER_HELP)
            self.statusBar().showMessage("openEMS is not importable in the solver Python.")
            return
        if exit_code != 0 or self._job_dir is None:
            message = solver_exit_message(exit_code)
            self.log.appendPlainText(message)
            self.statusBar().showMessage(message)
            return
        result_path = self._job_dir / "results.npz"
        if not result_path.is_file():
            self.statusBar().showMessage("The solver finished without results.npz.")
            return
        results = load_results(result_path)
        eps = self.project.eps_eff or (default_eps_eff(self.board.top_eps()) if self.board else default_eps_eff(4.5))
        self.results.show_results(results, eps)
        self.statusBar().showMessage("Results loaded.")

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

    def _solver_dialog(self) -> None:
        SolverDialog(self).exec()


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
            "Crop a smaller region, lower f max, or lower cells per feature. "
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
