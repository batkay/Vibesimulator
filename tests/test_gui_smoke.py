"""Offscreen check that the window loads a board, takes ports, and draws results."""

import os
import shutil
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
)

from pcb_simulator.app import FitLabel, MainWindow, project_file_for, solver_exit_message
from pcb_simulator.view.board_view import _decimate
from pcb_simulator.calc.tdr import tdr_impedance, tdr_view_window


@pytest.fixture(scope="module")
def qapp():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    yield app


def test_mesh_overlay_stays_sparse():
    values = [i * 0.01 for i in range(8000)]
    kept = _decimate(values, 0.0)
    assert len(kept) <= 49
    assert kept[0] == values[0]
    assert kept[-1] == values[-1]


def test_native_abort_is_explained():
    text = solver_exit_message(-1073740791)
    assert "0xC0000409" in text
    assert "operator" in text


def test_save_status_shows_before_ctrl_s(tmp_path, qapp):
    board = _copy_coupon(tmp_path)
    window = MainWindow()
    window.open_board(board)
    assert window.save_state.text() == "Unsaved"
    assert window.windowTitle().endswith("*")
    assert window.file_save_state.text().startswith("Unsaved")

    window.save_project()
    assert window.save_state.text() == "Saved"
    assert not window.windowTitle().endswith("*")
    assert window.file_save_state.text().startswith("Saved")

    window.f_max.setValue(8.0)
    assert window.save_state.text() == "Unsaved"
    window.f_max.setValue(5.0)
    assert window.save_state.text() == "Saved"

    window.margin.setValue(4.0)
    assert window.save_state.text() == "Unsaved"
    window.close()

    again = MainWindow()
    again.open_project(project_file_for(board))
    assert again.save_state.text() == "Saved"
    assert not again.windowTitle().endswith("*")
    again.close()


def _copy_coupon(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "examples" / "microstrip_50ohm.kicad_pcb"
    dest = tmp_path / "microstrip_50ohm.kicad_pcb"
    shutil.copy(source, dest)
    return dest


def _layer_z(window, name: str) -> float:
    values = [
        item.zValue()
        for item in window.view.scene().items()
        if item.data(0) == name
    ]
    assert values, name
    return max(values)


def _union(window, name: str):
    rect = None
    for item in window.view.scene().items():
        if item.data(0) != name:
            continue
        bounds = item.sceneBoundingRect()
        rect = bounds if rect is None else rect.united(bounds)
    assert rect is not None
    return rect


def _shelf_page(window, title: str):
    for index in range(window._shelf.count()):
        if window._shelf.tabText(index) == title:
            return window._shelf.widget(index)
    raise AssertionError(title)


def _click(view, x: float, y: float) -> None:
    point = view.mapFromScene(view.model_to_scene(x, y))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)


def test_window_loads_coupon_and_takes_ports(qapp, tmp_path: Path):
    window = MainWindow()
    window.show()
    window.resize(1400, 900)
    qapp.processEvents()
    window.open_board(_copy_coupon(tmp_path))
    qapp.processEvents()
    window.view.zoom_fit()
    qapp.processEvents()

    assert _layer_z(window, "F.Cu") > _layer_z(window, "B.Cu")
    ground = _union(window, "B.Cu")
    # File +Y is downward on the canvas, the same way KiCad draws an unflipped board.
    assert window.view.model_to_scene(0, 4).y() > window.view.model_to_scene(0, -4).y()
    assert ground.top() < -3
    assert ground.bottom() > 3
    assert window.view.pad_at(0, 0) is not None
    assert window.view.pad_at(10, 0) is not None

    window._set_tool("port")
    _click(window.view, 0, 0)
    qapp.processEvents()
    assert window.project.ports == []
    assert window.view.highlight_net == ""
    _click(window.view, 0, 3)
    _click(window.view, 10, 0)
    qapp.processEvents()
    assert [port.footprint for port in window.project.ports] == ["J1"]
    _click(window.view, 10, 3)
    qapp.processEvents()
    assert [port.exc_dir for port in window.project.ports] == ["z", "z"]
    assert [port.reference_net for port in window.project.ports] == ["GND", "GND"]
    assert [port.footprint for port in window.project.ports] == ["J1", "J2"]
    assert window.view.highlight_net == ""
    assert window.port_list.item(0).text() == "P1   J1.1   SIG to GND   50.0 Ω"
    assert window.port_list.item(1).text() == "P2   J2.1   SIG to GND   50.0 Ω"
    assert any(item.data(0) == "port" for item in window.view.scene().items())
    assert window.view._port_anchor is None
    assert window.add_differential_pair("P1", "P1")
    assert window.add_differential_pair("P1", "P2") is None
    assert window.add_differential_pair("P2", "P1")
    assert [(pair.positive, pair.negative) for pair in window.project.pairs] == [("P1", "P2")]

    window.cells_per_feature.setValue(2)
    mesh = window.preview_mesh()
    assert mesh is not None
    assert mesh.cell_count < 500_000
    assert "cells" in window.preflight.text()
    assert any(item.zValue() == 40 for item in window.view.scene().items())

    window._set_tool("crop")
    start = window.view.mapFromScene(window.view.model_to_scene(1, -1))
    end = window.view.mapFromScene(window.view.model_to_scene(6, 2))
    QTest.mousePress(window.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(window.view.viewport(), end)
    QTest.mouseRelease(window.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    qapp.processEvents()
    assert window.project.mode == "crop"
    assert window.project.rect is not None
    x0, y0, x1, y1 = window.project.rect
    # A mouse event lands on a pixel. One pixel on this view is a few hundredths of a millimetre.
    assert min(x0, x1) == pytest.approx(1, abs=0.05)
    assert max(x0, x1) == pytest.approx(6, abs=0.05)

    dielectric = next(row for row in range(window.stack_table.rowCount()) if window.stack_table.item(row, 1).text() == "dielectric")
    window.stack_table.item(dielectric, 2).setText("0.30")
    qapp.processEvents()
    assert window.board.layer_z("F.Cu") == pytest.approx(0.3)
    assert any(abs(port.stop[2] - 0.3) < 1e-6 or abs(port.start[2] - 0.3) < 1e-6 for port in window.project.ports)

    for index in range(window.layer_box.count()):
        widget = window.layer_box.itemAt(index).widget()
        if isinstance(widget, QCheckBox) and widget.text() == "B.Cu":
            widget.setChecked(False)
    qapp.processEvents()
    assert not any(item.data(0) == "B.Cu" for item in window.view.scene().items())

    window.save_project()
    saved = project_file_for(window.board_path)
    assert saved.is_file()
    again = MainWindow()
    again.open_project(saved)
    assert [port.name for port in again.project.ports] == ["P1", "P2"]
    assert [(pair.positive, pair.negative) for pair in again.project.pairs] == [("P1", "P2")]
    assert again.board.layer_z("F.Cu") == pytest.approx(0.3)

    freq = np.linspace(1e8, 5e9, 32)
    s = np.zeros((32, 2, 2), dtype=complex)
    s[:, 0, 0] = 0.05
    s[:, 1, 1] = 0.05
    s[:, 0, 1] = 0.9
    s[:, 1, 0] = 0.9
    window.results.show_results(
        {
            "freq": freq,
            "S": s,
            "z0": np.array([50.0, 50.0]),
            "port_names": ["P1", "P2"],
            "decayed": False,
            "excitations": [
                {"t": np.linspace(0, 1e-9, 16), "v": np.ones((2, 16)), "i": np.zeros((2, 16))},
                {"t": np.linspace(0, 1e-9, 16), "v": np.ones((2, 16)), "i": np.zeros((2, 16))},
            ],
        },
        eps_eff=3.4,
    )
    assert "did not decay" in window.results.decay_notice
    assert "did not decay" not in window.results.banner.text()
    labels = [window.results.port.itemText(i) for i in range(window.results.port.count())]
    assert any(label.startswith("D1") for label in labels)
    for index in range(window.results.port.count()):
        if str(window.results.port.itemText(index)).startswith("D1"):
            window.results.port.setCurrentIndex(index)
            break
    for index in range(window.results.tabs.count()):
        window.results.tabs.setCurrentIndex(index)
        qapp.processEvents()
        assert window.results.figure.axes
    window.results.port.setCurrentIndex(0)
    for index in range(window.results.tabs.count()):
        window.results.tabs.setCurrentIndex(index)
        qapp.processEvents()
        assert window.results.figure.axes
    window.results.tabs.setCurrentIndex(4)
    qapp.processEvents()
    shown = window.results.displayed_network()
    time_s, impedance = tdr_impedance(shown, 0)
    t0, t1 = tdr_view_window(time_s, impedance)
    left, right = window.results.figure.axes[0].get_xlim()
    assert left == pytest.approx(t0 * 1e9)
    assert right == pytest.approx(t1 * 1e9)
    window.results.z0.setValue(75)
    qapp.processEvents()
    assert window.results.figure.axes
    window.close()
    again.close()


def test_left_pane_does_not_scroll_sideways(qapp):
    window = MainWindow()
    window.show()
    long_name = "Net-(J1-Pad12)-a-very-long-kicad-net-name-that-used-to-overflow"
    window.reference.addItem(long_name)
    window.net_list.addItem(long_name)
    window.pair_pos.addItem("P3  " + long_name)
    window.pair_neg.addItem("P4  " + long_name)
    window.power_net.addItem(long_name)
    window.preflight.setText(
        "243 × 370 × 50 = 4,495,500 cells. Rough memory 450 MB. "
        "Cells grade from 0.050 mm on copper to 2.83 mm in open space. This is a large mesh."
    )
    window._nets_dialog.resize(320, 480)
    window._nets_dialog.show()
    window._stack_dialog.resize(640, 420)
    window._stack_dialog.show()
    qapp.processEvents()
    for width in (1000, 1200, 1400, 1900):
        window.resize(width, 800)
        for index in range(window._shelf.count()):
            window._shelf.setCurrentIndex(index)
            qapp.processEvents()
            page = window._shelf.widget(index)
            for label in page.findChildren(FitLabel):
                if not label.text().strip() or label.width() < 80:
                    continue
                assert label.height() + 2 >= label.heightForWidth(label.width())
        assert window.net_list.horizontalScrollBar().maximum() == 0
        assert window.stack_table.horizontalScrollBar().maximum() == 0
        assert window._shelf._scroll.horizontalScrollBar().maximum() == 0
    assert [window._shelf.tabText(i) for i in range(window._shelf.count())] == [
        "File", "Place", "Components", "View", "Board", "Run", "Settings",
    ]
    assert not window.menuBar().isVisible()
    place = _shelf_page(window, "Place")
    run = _shelf_page(window, "Run")
    assert any(button.text() == "Click a pad" for button in place.findChildren(QPushButton))
    assert any(button.text() == "S parameters…" for button in run.findChildren(QPushButton))
    assert any(button.text() == "PDN…" for button in run.findChildren(QPushButton))
    assert any(button.text() == "S parameters" for button in window._s_dialog.findChildren(QPushButton))
    assert any(button.text() == "PDN" for button in window._pdn_dialog.findChildren(QPushButton))
    assert any(button.text() == "Cancel" for button in run.findChildren(QPushButton))
    for dialog in (window._s_dialog, window._pdn_dialog):
        assert any(button.text() == "Cancel" and not button.isEnabled() for button in dialog.findChildren(QPushButton))
    window._set_cancel_enabled(True)
    assert window.cancel_btn.isEnabled()
    for dialog in (window._s_dialog, window._pdn_dialog):
        assert any(button.text() == "Cancel" and button.isEnabled() for button in dialog.findChildren(QPushButton))
    window._set_cancel_enabled(False)
    view = _shelf_page(window, "View")
    board_page = _shelf_page(window, "Board")
    assert any(button.text() == "Layers" for button in view.findChildren(QPushButton))
    assert any(button.text() == "Recenter" for button in view.findChildren(QPushButton))
    assert any(button.text() == "Measure" for button in view.findChildren(QPushButton))
    assert not any(button.text() == "Measure" for button in place.findChildren(QPushButton))
    assert any(button.text() == "Nets…" for button in view.findChildren(QPushButton))
    assert any(button.text() == "Results" for button in run.findChildren(QPushButton))
    assert any(button.text() == "Stackup…" for button in board_page.findChildren(QPushButton))
    assert not board_page.isAncestorOf(window.reference)
    assert not any(label.text() == "Reference" for label in board_page.findChildren(QLabel))
    assert run.isAncestorOf(window.mode_crop)
    assert run.isAncestorOf(window.mode_outline)
    assert run.isAncestorOf(window.margin)
    assert window.mode_crop.parent() is window.mode_outline.parent()
    assert not board_page.isAncestorOf(window.mode_crop)
    assert window.net_list.window() is window._nets_dialog
    assert window.stack_table.window() is window._stack_dialog
    window.close()
    assert not window._nets_dialog.isVisible()
    assert not window._stack_dialog.isVisible()


def test_run_tab_speed_controls_follow_the_project(qapp):
    window = MainWindow()
    window.resize(1100, 800)
    window.show()
    qapp.processEvents()
    run = _shelf_page(window, "Run")
    assert run.isAncestorOf(window.boundary)
    assert run.isAncestorOf(window.stop_mode)
    assert run.isAncestorOf(window.signal_only)
    assert window.boundary.currentData() == "mur"
    assert window.stop_mode.currentData() == "crossing"
    assert window.signal_only.isChecked()
    assert window.signal_only.text() == "Port nets"
    window.boundary.setCurrentIndex(window.boundary.findData("pml"))
    window.stop_mode.setCurrentIndex(window.stop_mode.findData("energy"))
    window.signal_only.setChecked(False)
    qapp.processEvents()
    assert window.project.boundary == "pml"
    assert window.project.stop == "energy"
    assert window.project.signal_only is False
    assert window._shelf._scroll.horizontalScrollBar().maximum() == 0
    window.close()


def test_auto_crop_button_boxes_the_port_net(qapp):
    from pcb_simulator.model.board import Board, CopperPoly, StackLayer, assign_stackup_z
    from pcb_simulator.model.mesh import PortBox

    window = MainWindow()
    board = Board(thickness_mm=0.2)
    board.stackup = [
        StackLayer("F.Cu", "copper", 0.035),
        StackLayer("dielectric 1", "dielectric", 0.2, eps_r=4.5, loss_tangent=0.02),
        StackLayer("B.Cu", "copper", 0.035),
    ]
    assign_stackup_z(board.stackup)
    board.outline = [(0, 0), (20, 0), (20, 10), (0, 10)]
    board.copper = [
        CopperPoly([(4, 3), (4.4, 3), (4.4, 6), (4, 6)], "F.Cu", "D_P", "trace", feature_mm=0.4),
        CopperPoly([(0, 0), (20, 0), (20, 10), (0, 10)], "B.Cu", "GND", "zone"),
    ]
    window.board = board
    window.project.reference_net = "GND"
    window.reference.addItem("GND")
    window.reference.setCurrentText("GND")
    window.project.ports = [PortBox("P1", 50, "y", (4.0, 3.0, 0.2), (4.4, 3.2, 0.0), net="D_P")]
    window.project.rect = (0, 0, 20, 10)
    window.mode_outline.setChecked(True)
    run = _shelf_page(window, "Run")
    board_page = _shelf_page(window, "Board")
    assert any(button.text() == "Auto crop" for button in run.findChildren(QPushButton))
    assert not any(button.text() == "Auto crop" for button in board_page.findChildren(QPushButton))
    window._auto_crop()
    assert window.project.mode == "crop"
    assert window.mode_crop.isChecked()
    assert window.project.rect == pytest.approx((4.0, 3.0, 4.4, 6.0))
    ring = window.view.region_points
    assert ring is not None
    assert max(point[0] for point in ring) == pytest.approx(6.4)
    assert max(point[1] for point in ring) == pytest.approx(8.0)
    window.close()


def test_place_fields_stay_wide_enough_to_edit(qapp):
    import warnings

    window = MainWindow()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            window.resize(1400, 860)
            window.show()
            qapp.processEvents()
            window._set_tool("vrm")
            qapp.processEvents()
            assert window.vrm_v.isVisible()
            assert window.vrm_v.width() > 40
            window._set_tool("load")
            qapp.processEvents()
            assert window.load_i.isVisible()
            assert window.load_i.width() > 40
            window._set_tool("port")
            qapp.processEvents()
            window._ports_dialog.show()
            qapp.processEvents()
            assert window.port_r.isVisible()
            assert window.port_r.width() > 40
            assert window.results.canvas.height() > 80
            assert not hasattr(window, "ripple")
            assert not hasattr(window, "step_a")
        assert not any("collapsed to zero" in str(item.message) for item in caught)
    finally:
        window.close()


def test_chart_pops_out_and_hover_reads_the_nearest_point(qapp):
    from matplotlib.backend_bases import MouseEvent

    window = MainWindow()
    try:
        window.resize(1200, 800)
        window.show()
        qapp.processEvents()
        freq = np.linspace(1e8, 5e9, 32)
        samples = np.zeros((32, 1, 1), dtype=complex)
        samples[:, 0, 0] = 0.2
        window.results.show_results(
            {
                "freq": freq,
                "S": samples,
                "z0": np.array([50.0]),
                "port_names": ["P1"],
                "decayed": True,
                "excitations": [
                    {"t": np.linspace(0, 1e-9, 16), "v": np.ones(16), "i": np.zeros(16)},
                ],
            },
            eps_eff=2.75,
        )
        qapp.processEvents()
        window.results.canvas.draw()
        assert window.results._pop_button.text() == "Pop out"
        assert window.results.canvas.parent() is window.results

        window.results._pop_button.click()
        qapp.processEvents()
        window.results.canvas.draw()
        assert window.results._chart.isVisible()
        assert window.results._chart.windowTitle() == "Magnitude"
        assert window.results.canvas.parent() is window.results._chart
        assert window.results.toolbar.parent() is window.results._chart
        assert window.results._pop_button.parent() is window.results
        assert window.results._chart_note.isVisible()
        assert window.results._pop_button.text() == "Dock"

        window.results.tabs.setCurrentIndex(1)
        qapp.processEvents()
        window.results.canvas.draw()
        assert window.results.canvas.parent() is window.results._chart
        assert window.results.figure.axes
        assert window.results._chart.windowTitle() == "Phase"

        window.results.tabs.setCurrentIndex(0)
        qapp.processEvents()
        window.results.canvas.draw()
        ax = window.results.figure.axes[0]
        line = next(item for item in ax.get_lines() if item.get_gid() == "data")
        displayed = line.get_transform().transform((float(line.get_xdata()[4]), float(line.get_ydata()[4])))
        px, py = float(displayed[0]), float(displayed[1])
        event = MouseEvent("motion_notify_event", window.results.canvas, px, py)
        assert event.inaxes is not None
        window.results.canvas.callbacks.process("motion_notify_event", event)
        assert "|S11|" in window.results._hover_text
        assert "Frequency" in window.results._hover_text
        assert any(item.get_gid() == "hover" for item in ax.get_lines())

        miss = MouseEvent("motion_notify_event", window.results.canvas, px, py + 80)
        window.results.canvas.callbacks.process("motion_notify_event", miss)
        assert window.results._hover_text == ""

        for index in range(window.results.tabs.count()):
            if window.results.tabs.tabText(index) == "Smith":
                window.results.tabs.setCurrentIndex(index)
                break
        qapp.processEvents()
        window.results.canvas.draw()
        ax = next(item for item in window.results.figure.axes if any(line.get_gid() == "data" for line in item.get_lines()))
        line = next(item for item in ax.get_lines() if item.get_gid() == "data")
        xs = np.asarray(line.get_xdata(), dtype=float).reshape(-1)
        ys = np.asarray(line.get_ydata(), dtype=float).reshape(-1)
        displayed = line.get_transform().transform((float(xs[4]), float(ys[4])))
        event = MouseEvent("motion_notify_event", window.results.canvas, float(displayed[0]), float(displayed[1]))
        assert event.inaxes is not None
        window.results.canvas.callbacks.process("motion_notify_event", event)
        assert "S11" in window.results._hover_text
        assert "Re" in window.results._hover_text
        assert "|S|" in window.results._hover_text

        window.results._chart.close()
        qapp.processEvents()
        assert not window.results._chart.isVisible()
        assert window.results.canvas.parent() is window.results
        assert window.results.toolbar.parent() is window.results
        assert not window.results._chart_note.isVisible()
        assert window.results._pop_button.text() == "Pop out"

        window.results._pop_button.click()
        qapp.processEvents()
        assert window.results._chart.isVisible()
        window.results._pop_button.click()
        qapp.processEvents()
        assert not window.results._chart.isVisible()
        assert window.results.canvas.parent() is window.results

        window.results._pop_button.click()
        qapp.processEvents()
        window.close()
        qapp.processEvents()
        assert not window.results._chart.isVisible()
    finally:
        window.close()


def test_drag_zooms_the_chart_and_a_click_leaves_a_point(qapp):
    from matplotlib.backend_bases import MouseEvent

    window = MainWindow()
    try:
        window.resize(1200, 800)
        window.show()
        qapp.processEvents()
        freq = np.linspace(1e8, 5e9, 32)
        samples = np.zeros((32, 1, 1), dtype=complex)
        samples[:, 0, 0] = np.linspace(0.05, 0.9, 32)
        window.results.show_results(
            {
                "freq": freq,
                "S": samples,
                "z0": np.array([50.0]),
                "port_names": ["P1"],
                "decayed": True,
                "excitations": [
                    {"t": np.linspace(0, 1e-9, 16), "v": np.ones(16), "i": np.zeros(16)},
                ],
            },
            eps_eff=2.75,
        )
        qapp.processEvents()
        window.results.canvas.draw()
        ax = window.results.figure.axes[0]
        original = ax.get_xlim()
        original_y = ax.get_ylim()
        box = ax.bbox
        x0 = float(box.x0 + box.width * 0.40)
        x1 = float(box.x0 + box.width * 0.60)
        y0 = float(box.y0 + box.height * 0.05)
        y1 = float(box.y0 + box.height * 0.95)
        canvas = window.results.canvas

        def fire(name, x, y, button=1):
            event = MouseEvent(name, canvas, x, y, button=button)
            assert event.inaxes is not None
            canvas.callbacks.process(name, event)

        fire("button_press_event", x0, y0)
        fire("motion_notify_event", x1, y1)
        fire("button_release_event", x1, y1)
        zoomed = ax.get_xlim()
        zoomed_y = ax.get_ylim()
        assert zoomed[1] - zoomed[0] < (original[1] - original[0]) * 0.45
        assert abs(zoomed_y[1] - zoomed_y[0]) < abs(original_y[1] - original_y[0]) * 0.5
        assert len(window.results._pins) == 0
        window.results.toolbar.home()
        assert ax.get_xlim() == pytest.approx(original)
        assert ax.get_ylim() == pytest.approx(original_y)

        line = next(item for item in ax.get_lines() if item.get_gid() == "data")
        displayed = line.get_transform().transform((float(line.get_xdata()[4]), float(line.get_ydata()[4])))
        px, py = float(displayed[0]), float(displayed[1])
        fire("button_press_event", px, py)
        fire("button_release_event", px, py)
        assert len(window.results._pins) == 1
        note = next(iter(window.results._pins.values()))[1]
        assert "|S11|" in note.get_text()
        later = line.get_transform().transform((float(line.get_xdata()[12]), float(line.get_ydata()[12])))
        fire("button_press_event", float(later[0]), float(later[1]))
        fire("button_release_event", float(later[0]), float(later[1]))
        assert len(window.results._pins) == 2
        window.results._clear_points.click()
        qapp.processEvents()
        assert window.results._pins == {}
        assert not any(item.get_gid() == "pin" for item in ax.get_lines())
    finally:
        window.close()


def test_fine_mesh_check_sets_a_tighter_pitch(qapp):
    window = MainWindow()
    window.show()
    qapp.processEvents()
    assert window.cells_per_feature.value() == 2
    assert window.fine_mesh.isChecked() is False
    assert window.pdn_f_max.value() == pytest.approx(1.0)
    assert window.pdn_signal_mesh.isChecked() is False
    window.fine_mesh.setChecked(True)
    qapp.processEvents()
    assert window.cells_per_feature.value() == 4
    window.fine_mesh.setChecked(False)
    qapp.processEvents()
    assert window.cells_per_feature.value() == 2
    window.cells_per_feature.setValue(8)
    qapp.processEvents()
    assert window.fine_mesh.isChecked() is False
    assert window.cells_per_feature.value() == 8
    window.close()


def test_shelf_pages_share_one_height_and_push_the_board_down(qapp):
    from pcb_simulator.view.flow_layout import FlowHost
    from pcb_simulator.view.info_tip import NoteWithInfo

    window = MainWindow()
    window.resize(1000, 800)
    window.show()
    qapp.processEvents()
    shelf = window._shelf
    kinds = (FlowHost, NoteWithInfo, FitLabel, QPushButton, QComboBox, QLineEdit, QAbstractSpinBox, QCheckBox)
    for width in (1000, 1400):
        window.resize(width, 800)
        qapp.processEvents()
        board_y = window._splitter.mapTo(window, QPoint(0, 0)).y()
        panel_heights = []
        for index in range(shelf.count()):
            shelf.setCurrentIndex(index)
            qapp.processEvents()
            if shelf.tabText(index) == "Place":
                for tool in ("navigate", "port", "vrm", "load", "part", "crop"):
                    window._set_tool(tool)
                    qapp.processEvents()
                    _assert_shelf_page_fits(window, board_y, kinds)
                    panel_heights.append(shelf._panel.height())
            else:
                _assert_shelf_page_fits(window, board_y, kinds)
                panel_heights.append(shelf._panel.height())
        assert len(set(panel_heights)) == 1
        assert panel_heights[0] > 80
    from PySide6.QtWidgets import QWidget
    from pcb_simulator.view.shelf import _show_dialog

    for dialog in (window._s_dialog, window._pdn_dialog):
        _show_dialog(dialog)
        qapp.processEvents()
        assert dialog.isVisible()
        for widget in dialog.findChildren(QWidget):
            if not isinstance(widget, (FlowHost, FitLabel)) or not widget.isVisible() or widget.width() < 40:
                continue
            wanted = widget.heightForWidth(max(1, widget.width()))
            assert widget.height() + 3 >= wanted
            assert widget.height() <= wanted + 24, (dialog.windowTitle(), widget.height(), wanted)
    window.close()


def _assert_shelf_page_fits(window, board_y, kinds) -> None:
    from PySide6.QtWidgets import QWidget

    shelf = window._shelf
    panel = shelf._panel
    assert panel.isVisible()
    assert shelf.height() > shelf.tabBar().height() + 40
    splitter_top = window._splitter.mapTo(window, QPoint(0, 0)).y()
    panel_bottom = panel.mapTo(window, QPoint(0, panel.height())).y()
    assert splitter_top >= panel_bottom - 2
    assert splitter_top == board_y
    page = shelf.widget(shelf.currentIndex())
    title = shelf.tabText(shelf.currentIndex())
    scroll = shelf._scroll
    assert scroll.horizontalScrollBar().maximum() == 0
    scrolling = scroll.verticalScrollBar().maximum() > 0
    if title != "Place":
        assert not scrolling, (title, scroll.verticalScrollBar().maximum(), panel.height())
    for widget in page.findChildren(QWidget):
        if not isinstance(widget, kinds):
            continue
        if not widget.isVisible() or widget.width() < 4 or widget.height() < 1:
            continue
        if widget.hasHeightForWidth():
            wanted = widget.heightForWidth(max(1, widget.width()))
        else:
            wanted = widget.minimumSizeHint().height()
        if wanted < 0:
            continue
        wanted = min(wanted, widget.maximumHeight())
        assert widget.height() + 3 >= wanted, (title, type(widget).__name__, widget.height(), wanted)
        if scrolling:
            continue
        bottom = widget.mapTo(panel, QPoint(0, widget.height() - 1)).y()
        assert bottom <= panel.height() + 2, (title, type(widget).__name__, bottom, panel.height())


def test_components_tab_opens_a_list_for_each_kind(qapp):
    window = MainWindow()
    window.resize(1100, 800)
    window.show()
    qapp.processEvents()
    place = _shelf_page(window, "Place")
    components = _shelf_page(window, "Components")
    for tool, listed in (
        ("port", window.port_list),
        ("vrm", window.vrm_list),
        ("load", window.load_list),
        ("part", window.part_list),
    ):
        window._set_tool(tool)
        qapp.processEvents()
        assert not place.isAncestorOf(listed)
    assert any(button.text() == "Click a pad" for button in place.findChildren(QPushButton))
    for text in ("Delete port", "Delete VRM", "Delete load", "Delete part", "Add pair"):
        assert not any(button.text() == text for button in place.findChildren(QPushButton))
    components_index = next(
        index for index in range(window._shelf.count()) if window._shelf.tabText(index) == "Components"
    )
    window._shelf.setCurrentIndex(components_index)
    qapp.processEvents()
    assert window._shelf._scroll.horizontalScrollBar().maximum() == 0
    opened = (
        ("Ports…", window._ports_dialog, window.port_list),
        ("Pairs…", window._pairs_dialog, window.pair_list),
        ("VRM…", window._vrm_dialog, window.vrm_list),
        ("Load…", window._load_dialog, window.load_list),
        ("Part…", window._part_dialog, window.part_list),
    )
    for index in range(40):
        window.port_list.addItem(f"P{index}   J1.{index}   SIG to GND   50.0 Ω")
    for label, dialog, listed in opened:
        button = next(item for item in components.findChildren(QPushButton) if item.text() == label)
        button.click()
        qapp.processEvents()
        assert dialog.isVisible()
        assert listed.window() is dialog
        assert listed.maximumHeight() > 500
        assert not components.isAncestorOf(listed)
        if listed is window.port_list:
            assert listed.verticalScrollBar().maximum() > 0
        closer = next(item for item in dialog.findChildren(QPushButton) if item.text() == "Close")
        closer.click()
        qapp.processEvents()
        assert not dialog.isVisible()
    window.close()
    for _label, dialog, _listed in opened:
        assert not dialog.isVisible()


def test_clicking_the_open_shelf_tab_hides_the_panel(qapp):
    window = MainWindow()
    window.show()
    window.resize(1200, 800)
    qapp.processEvents()
    shelf = window._shelf
    bar = shelf.tabBar()
    board = next(index for index in range(shelf.count()) if shelf.tabText(index) == "Board")
    point = bar.tabRect(board).center()
    open_y = window._splitter.mapTo(window, QPoint(0, 0)).y()
    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=point)
    qapp.processEvents()
    assert shelf.currentIndex() == board
    assert shelf._panel.isVisible()
    assert window._splitter.mapTo(window, QPoint(0, 0)).y() == open_y
    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=point)
    qapp.processEvents()
    assert not shelf._panel.isVisible()
    assert shelf.height() <= bar.height() + 4
    closed_y = window._splitter.mapTo(window, QPoint(0, 0)).y()
    assert closed_y < open_y - 20
    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=point)
    qapp.processEvents()
    assert shelf.currentIndex() == board
    assert shelf._panel.isVisible()
    assert window._splitter.mapTo(window, QPoint(0, 0)).y() == open_y
    window.close()


def test_escape_stops_a_placement_then_returns_to_pan(qapp):
    window = MainWindow()
    window.resize(1200, 800)
    window.show()
    qapp.processEvents()
    window.view.setFocus()
    window._set_tool("port")
    window._arm_two_click()
    window._two_click = (1.0, 1.0, "F.Cu")
    window.view._port_anchor = (1.0, 1.0)
    window.view._show_port_preview((1.0, 1.0), (4.0, 2.0))
    qapp.processEvents()
    assert window.view.cursor().shape() == Qt.CursorShape.CrossCursor
    QTest.keyClick(window.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert window.view._tool == "port"
    assert window.view._port_anchor is None
    assert window._two_click == ("armed",)
    assert window.view.cursor().shape() == Qt.CursorShape.CrossCursor
    assert not any(item.data(0) == "port-preview" for item in window.view.scene().items())
    QTest.keyClick(window.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert window.view._tool == "navigate"
    assert window.view.cursor().shape() == Qt.CursorShape.ArrowCursor
    window._set_tool("vrm")
    window._arm_supply("vrm", 0.0, 0.0, "F.Cu", "SIG", "J1", "1")
    assert window._terminal_anchor is not None
    QTest.keyClick(window, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert window._terminal_anchor is None
    assert window.view._tool == "vrm"
    QTest.keyClick(window, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert window.view._tool == "navigate"
    window._set_tool("load")
    window.load_i.setFocus()
    QTest.keyClick(window.load_i, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert window.view._tool == "navigate"
    assert window.view.cursor().shape() == Qt.CursorShape.ArrowCursor
    window.close()


def test_each_option_has_its_own_info_popup(qapp):
    from pcb_simulator.view.info_tip import _popup

    window = MainWindow()
    window.show()
    qapp.processEvents()
    buttons = window.findChildren(QPushButton, "infoButton")
    titles = {button.property("infoTitle") for button in buttons}
    assert {"f max", "Energy limit", "Margin", "Two clicks", "Display Z0", "εeff"} <= titles
    f_max = next(button for button in buttons if button.property("infoTitle") == "f max")
    window._s_dialog.show()
    qapp.processEvents()
    QTest.mouseClick(f_max, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    popup = _popup()
    assert popup.isVisible()
    assert popup.title.text() == "f max"
    assert "mesh does not change" in popup.body.text()
    assert "1 / f max" in popup.body.text()
    assert "0.23 ns" in popup.body.text()
    margin = next(button for button in buttons if button.property("infoTitle") == "Margin")
    QTest.mouseClick(margin, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert popup.isVisible()
    assert popup.title.text() == "Margin"
    assert "ground plane" in popup.body.text()
    QTest.mouseClick(margin, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert not popup.isVisible()
    QTest.mouseClick(f_max, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert popup.isVisible()
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent, QWindow

    native = QWindow()
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(-10000, -10000),
        QPointF(-10000, -10000),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    assert popup.eventFilter(native, press) is False
    assert not popup.isVisible()
    native.destroy()
    window.close()


def test_port_tool_waits_for_the_reference_click(qapp, tmp_path: Path):
    window = MainWindow()
    window.show()
    window.resize(1400, 900)
    qapp.processEvents()
    window.open_board(_copy_coupon(tmp_path))
    qapp.processEvents()
    window.view.zoom_fit()
    qapp.processEvents()
    window._arm_two_click()
    window._set_tool("port")
    _click(window.view, 0, 0)
    qapp.processEvents()
    assert window.project.ports == []
    assert window.view.highlight_net == ""
    _click(window.view, 10, 0)
    qapp.processEvents()
    assert window.project.ports == []
    assert window.view._port_anchor is not None
    assert "different net" in window.log.toPlainText()
    _click(window.view, 0, 3)
    qapp.processEvents()
    assert [port.footprint for port in window.project.ports] == ["J1"]
    assert window.project.ports[0].reference_net == "GND"
    assert window.project.reference_net == "GND"
    _click(window.view, 10, 0)
    qapp.processEvents()
    assert len(window.project.ports) == 1
    _click(window.view, 10, 3)
    qapp.processEvents()
    assert [port.footprint for port in window.project.ports] == ["J1", "J2"]
    assert window.view.highlight_net == ""
    window.close()


def test_two_click_marks_the_first_point_and_tracks_the_cursor(qapp, tmp_path: Path):
    window = MainWindow()
    window.show()
    window.resize(1400, 900)
    qapp.processEvents()
    window.open_board(_copy_coupon(tmp_path))
    qapp.processEvents()
    window.view.zoom_fit()
    qapp.processEvents()
    window._arm_two_click()
    _click(window.view, 1, 1)
    qapp.processEvents()
    assert window.view._port_anchor[0] == pytest.approx(1, abs=0.05)
    assert window.view._port_anchor[1] == pytest.approx(1, abs=0.05)
    assert any(item.data(0) == "port-preview" for item in window.view.scene().items())
    end = window.view.mapFromScene(window.view.model_to_scene(4, 2))
    QTest.mouseMove(window.view.viewport(), end)
    qapp.processEvents()
    line = next(item for item in window.view.scene().items() if item.data(0) == "port-preview-line")
    assert line.line().p1().x() == pytest.approx(1, abs=0.05)
    assert line.line().p1().y() == pytest.approx(1, abs=0.05)
    assert line.line().p2().x() == pytest.approx(4, abs=0.05)
    assert line.line().p2().y() == pytest.approx(2, abs=0.05)
    _click(window.view, 4, 2)
    qapp.processEvents()
    assert window.project.ports == []
    assert window.view._port_anchor is not None
    assert any(item.data(0) == "port-preview" for item in window.view.scene().items())
    assert "different net" in window.log.toPlainText()
    window.close()


def test_right_drag_pans_the_board(qapp, tmp_path: Path):
    window = MainWindow()
    window.show()
    window.resize(900, 700)
    qapp.processEvents()
    window.open_board(_copy_coupon(tmp_path))
    qapp.processEvents()
    view = window.view
    view.zoom_fit()
    view.scale(4, 4)
    qapp.processEvents()
    bar = view.horizontalScrollBar()
    assert bar.maximum() > 40
    bar.setValue(bar.maximum() // 2)
    start = bar.value()
    origin = QPoint(120, 120)
    viewport = view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.RightButton, pos=origin)
    QTest.mouseMove(viewport, origin + QPoint(50, 0))
    qapp.processEvents()
    QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, pos=origin + QPoint(50, 0))
    assert bar.value() < start
    held = bar.value()
    QTest.mousePress(viewport, Qt.MouseButton.MiddleButton, pos=origin)
    QTest.mouseMove(viewport, origin + QPoint(-40, 0))
    QTest.mouseRelease(viewport, Qt.MouseButton.MiddleButton, pos=origin + QPoint(-40, 0))
    assert bar.value() == held
    window._set_tool("port")
    before = len(window.project.ports)
    pad = view.mapFromScene(view.model_to_scene(0, 0))
    QTest.mouseClick(viewport, Qt.MouseButton.RightButton, pos=pad)
    qapp.processEvents()
    assert len(window.project.ports) == before
    assert not view._pan
    window.close()


def test_board_warnings_are_lines_in_the_solver_log(qapp):
    from types import SimpleNamespace

    window = MainWindow()
    window.resize(1200, 800)
    window.show()
    qapp.processEvents()
    assert window._solver_dock.widget() is window.log
    assert not hasattr(window, "banner")
    window._log_line("solver step")
    window.board = SimpleNamespace(
        assumed_stackup=False,
        warnings=[
            "Zone on F.Cu net (unnamed) has no fill. Fill zones in KiCad before simulating; the outline is not copper."
        ],
    )
    window._log_board_notes()
    qapp.processEvents()
    lines = [line for line in window.log.toPlainText().splitlines() if line.strip()]
    assert lines[0].startswith("Zone on F.Cu")
    assert lines[-1] == "solver step"
    assert _log_color(window.log, "Zone on F.Cu") == (138, 90, 0)
    window.board.warnings = ["Edge.Cuts did not form a closed outline."]
    window._log_board_notes()
    text = window.log.toPlainText()
    assert "Edge.Cuts" in text
    assert "no fill" not in text
    assert "solver step" in text
    window._log_line("Building the mesh…", "app")
    window._log_line("The solver interpreter did not start.", "error")
    assert _log_color(window.log, "Building the mesh") == (31, 78, 121)
    assert _log_color(window.log, "did not start") == (155, 44, 44)
    window.close()


def _log_color(log, text: str) -> tuple[int, int, int]:
    from PySide6.QtGui import QTextCursor

    block = log.document().firstBlock()
    while block.isValid():
        if text in block.text():
            cursor = QTextCursor(block)
            cursor.movePosition(QTextCursor.MoveOperation.NextCharacter)
            color = cursor.charFormat().foreground().color()
            return (color.red(), color.green(), color.blue())
        block = block.next()
    raise AssertionError(text)


def test_recenter_fits_the_board_back_in_the_window(qapp, tmp_path: Path):
    window = MainWindow()
    window.show()
    window.resize(900, 700)
    qapp.processEvents()
    window.open_board(_copy_coupon(tmp_path))
    qapp.processEvents()
    view = window.view
    view.zoom_fit()
    view.scale(4, 4)
    qapp.processEvents()
    bar = view.horizontalScrollBar()
    zoomed = bar.maximum()
    assert zoomed > 40
    button = next(item for item in _shelf_page(window, "View").findChildren(QPushButton) if item.text() == "Recenter")
    button.click()
    qapp.processEvents()
    assert bar.maximum() < zoomed
    window.close()


def test_side_panes_track_the_window(qapp):
    window = MainWindow()
    window.show()
    window.resize(1200, 800)
    qapp.processEvents()
    assert window._splitter.widget(0) is window.results
    assert window._splitter.widget(1) is window.view
    assert window._splitter.widget(2) is window._layers_panel
    assert window._layers_button.isChecked()
    assert window._layers_panel.isVisible()
    narrow = list(window._splitter.sizes())
    window.resize(1900, 800)
    qapp.processEvents()
    wide = list(window._splitter.sizes())
    assert len(wide) == 3
    assert narrow[2] >= 160
    assert wide[2] >= 160
    assert wide[0] > narrow[0] + 20
    assert wide[1] > narrow[1] + 20
    window._layers_button.click()
    qapp.processEvents()
    closed = list(window._splitter.sizes())
    assert not window._layers_panel.isVisible()
    assert closed[2] == 0
    assert closed[1] > wide[1]
    window._layers_button.click()
    qapp.processEvents()
    opened = list(window._splitter.sizes())
    assert window._layers_panel.isVisible()
    assert opened[2] >= 160
    assert opened[1] > 80
    assert window._results_button.isChecked()
    before = list(window._splitter.sizes())
    window._results_button.click()
    qapp.processEvents()
    hidden = list(window._splitter.sizes())
    assert not window.results.isVisible()
    assert hidden[0] == 0
    assert hidden[1] > before[1]
    window._results_button.click()
    qapp.processEvents()
    shown = list(window._splitter.sizes())
    assert window.results.isVisible()
    assert shown[0] >= 200
    nets = next(button for button in _shelf_page(window, "View").findChildren(QPushButton) if button.text() == "Nets…")
    nets.click()
    qapp.processEvents()
    assert window._nets_dialog.isVisible()
    assert window.net_list.isVisible()
    stack = next(button for button in _shelf_page(window, "Board").findChildren(QPushButton) if button.text() == "Stackup…")
    stack.click()
    qapp.processEvents()
    assert window._stack_dialog.isVisible()
    assert window.stack_table.isVisible()
    assert window.stack_table.horizontalHeader().sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch
    window.close()


def _legend_labels(window):
    ax = window.results.figure.axes[0]
    legend = ax.get_legend()
    if legend is None:
        return []
    return [text.get_text() for text in legend.get_texts()]


def test_opening_an_undecayed_run_writes_the_solver_log(qapp, tmp_path: Path, monkeypatch):
    board = _copy_coupon(tmp_path)
    job = board.parent / ".sim" / "current"
    job.mkdir(parents=True)
    freq = np.linspace(1e8, 5e9, 8)
    np.savez(
        job / "results.npz",
        freq=freq,
        S=np.zeros((8, 1, 1), dtype=complex),
        z0=np.array([50.0]),
        port_names=np.array(["P1"]),
        decayed=np.array([False]),
    )
    shown = []
    monkeypatch.setattr("pcb_simulator.app.QMessageBox.warning", lambda *args, **kwargs: shown.append(args))
    window = MainWindow()
    window.open_board(board)
    qapp.processEvents()
    assert window.results.decay_notice
    assert "did not decay" not in window.results.banner.text()
    assert shown == []
    assert "did not decay" in window.log.toPlainText()
    assert "unfinished" in window.log.toPlainText()
    window.results.clear()
    assert window.results.decay_notice == ""
    window.close()


def test_opening_a_board_loads_its_saved_results(qapp, tmp_path: Path):
    board = _copy_coupon(tmp_path)
    job = board.parent / ".sim" / "current"
    job.mkdir(parents=True)
    freq = np.linspace(1e8, 5e9, 8)
    np.savez(
        job / "results.npz",
        freq=freq,
        S=np.zeros((8, 1, 1), dtype=complex),
        z0=np.array([50.0]),
        port_names=np.array(["P1"]),
        decayed=np.array([True]),
    )
    window = MainWindow()
    window.show()
    qapp.processEvents()
    assert window.board is None
    window.open_board(board)
    qapp.processEvents()
    assert window.board_path == board
    assert window.results._names == ["P1"]
    assert window.results._raw is not None
    window.close()


def test_checked_curves_are_the_only_ones_drawn(qapp):
    window = MainWindow()
    window.show()
    freq = np.linspace(1e8, 5e9, 32)
    s = np.zeros((32, 2, 2), dtype=complex)
    s[:, 0, 0] = 0.05
    s[:, 1, 1] = 0.2
    s[:, 0, 1] = 0.4
    s[:, 1, 0] = 0.6
    window.results.show_results(
        {"freq": freq, "S": s, "z0": np.array([50.0, 50.0]), "port_names": ["P1", "P2"], "decayed": True},
        eps_eff=3.0,
    )
    qapp.processEvents()
    labels = _legend_labels(window)
    assert "|S11|" in labels
    assert "|S22|" in labels
    assert "|S21|" not in labels
    assert "|S12|" not in labels
    terms = window.results.terms
    s21 = next(terms.item(i) for i in range(terms.count()) if terms.item(i).data(Qt.UserRole) == (1, 0))
    s21.setCheckState(Qt.Checked)
    qapp.processEvents()
    assert "|S21|" in _legend_labels(window)
    window.results._check_all()
    qapp.processEvents()
    labels = _legend_labels(window)
    assert {"|S11|", "|S12|", "|S21|", "|S22|"} <= set(labels)
    window.results._check_reflections()
    qapp.processEvents()
    labels = _legend_labels(window)
    assert "|S21|" not in labels
    assert "|S11|" in labels
    for index in range(window.results.tabs.count()):
        if window.results.tabs.tabText(index) == "Smith":
            window.results.tabs.setCurrentIndex(index)
            break
    qapp.processEvents()
    texts = [text.get_text() for ax in window.results.figure.axes for text in ax.texts]
    assert not any("NoneType" in text for text in texts)
    ends = []
    for line in window.results.figure.axes[0].lines:
        x = np.asarray(line.get_xdata(), dtype=float)
        y = np.asarray(line.get_ydata(), dtype=float)
        if x.size == 1:
            ends.append((float(x[0]), float(y[0])))
    assert (0.05, 0.0) in [(round(x, 5), round(y, 5)) for x, y in ends]
    window.close()


def test_pdn_run_keeps_the_s_parameter_folder(qapp, tmp_path: Path):
    import time

    from pcb_simulator.model.board import Board, CopperPoly, StackLayer, assign_stackup_z
    from pcb_simulator.project import PdnTerminalRecord

    window = MainWindow()
    window.show()
    qapp.processEvents()
    board = Board(thickness_mm=0.2)
    board.stackup = [
        StackLayer("F.Cu", "copper", 0.035),
        StackLayer("dielectric 1", "dielectric", 0.2, eps_r=4.5, loss_tangent=0.02),
        StackLayer("B.Cu", "copper", 0.035),
    ]
    assign_stackup_z(board.stackup)
    board.copper = [
        CopperPoly([(0, 0), (2, 0), (2, 0.5), (0, 0.5)], "F.Cu", "PWR", "zone"),
        CopperPoly([(0, 0), (2, 0), (2, 0.5), (0, 0.5)], "B.Cu", "GND", "zone"),
    ]
    board.outline = [(0.0, 0.0), (4.0, 0.0), (4.0, 2.0), (0.0, 2.0)]
    board_path = tmp_path / "tiny.kicad_pcb"
    board_path.write_text("tiny", encoding="utf-8")
    kept = tmp_path / ".sim" / "current" / "results.npz"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"keep-si")
    window.board = board
    window.board_path = board_path
    window.project.reference_net = "GND"
    window.project.power_net = "PWR"
    window.project.vrm = PdnTerminalRecord("VRM", 0.25, 0.25, "PWR", layer="F.Cu", volts=1.0)
    window.project.loads = [PdnTerminalRecord("U1", 1.75, 0.25, "PWR", layer="F.Cu", amps=1.0)]
    window.power_net.addItem("PWR")
    window.power_net.setCurrentText("PWR")
    window.reference.addItem("GND")
    window.reference.setCurrentText("GND")
    window.run_pdn()
    for _ in range(200):
        qapp.processEvents()
        thread = window._pdn_thread
        if window._pdn_result is not None and (thread is None or not thread.isRunning()):
            break
        time.sleep(0.02)
    qapp.processEvents()
    assert kept.read_bytes() == b"keep-si"
    assert (tmp_path / ".sim" / "pdn" / "result.npz").is_file()
    assert window._pdn_result is not None and window._pdn_result.ok
    assert window._pdn_result.drops[0]["loop_mv"] > 0
    assert window.results.tabs.tabText(window.results.tabs.currentIndex()) == "PDN"
    window.close()


def test_vrm_and_load_take_a_power_click_then_a_ground_click(qapp):
    from pcb_simulator.model.board import Board, Pad

    def pad(footprint, name, net, x, y):
        return Pad(
            footprint,
            name,
            net,
            ["F.Cu"],
            (x, y),
            [(x - 0.2, y - 0.2), (x + 0.2, y - 0.2), (x + 0.2, y + 0.2), (x - 0.2, y + 0.2)],
        )

    power = pad("U1", "1", "PWR", 1.0, 1.0)
    ground = pad("U1", "2", "GND", 2.0, 1.0)
    other = pad("U1", "3", "USB", 3.0, 1.0)
    window = MainWindow()
    window.board = Board(outline=[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)])
    window.view.set_board(window.board)
    window.project.reference_net = "GND"
    window.power_net.addItem("PWR")
    window.vrm_v.setValue(3.45)
    window._set_tool("vrm")
    window._on_pad_clicked(power)
    assert window.project.vrm is None
    # The default GND does not reject a different first ground click.
    window._on_pad_clicked(other)
    assert window.project.vrm is not None
    assert window.project.reference_net == "USB"
    window.project.vrm = None
    window.project.reference_net = "GND"
    window._on_pad_clicked(power)
    window._on_pad_clicked(ground)
    assert window.project.vrm is not None
    assert window.project.reference_net == "GND"
    assert window.project.vrm.volts == pytest.approx(3.45)
    assert window.project.vrm.x == pytest.approx(1.0)
    assert window.project.vrm.gx == pytest.approx(2.0)
    assert window.project.vrm.g_pad == "2"
    window._on_pad_clicked(power)
    window._on_pad_clicked(other)
    assert window.project.vrm.g_pad == "2"
    assert window.project.reference_net == "GND"
    window.load_i.setValue(0.4)
    window._set_tool("load")
    window._on_pad_clicked(ground)
    assert window.project.loads == []
    window._on_pad_clicked(power)
    assert window.project.loads == []
    window._on_pad_clicked(ground)
    assert len(window.project.loads) == 1
    assert window.project.loads[0].amps == pytest.approx(0.4)
    assert window.project.loads[0].pad == "1"
    assert window.project.loads[0].gx == pytest.approx(2.0)
    window.close()


def test_measure_follows_a_trace_and_tdr_hover_marks_it(qapp):
    import math

    from matplotlib.lines import Line2D

    from pcb_simulator.calc.tdr import C0, distance_m
    from pcb_simulator.model.board import Board, CopperPoly
    from pcb_simulator.model.mesh import PortBox

    window = MainWindow()
    try:
        window.resize(1200, 800)
        window.show()
        qapp.processEvents()
        view = _shelf_page(window, "View")
        measure = next(button for button in view.findChildren(QPushButton) if button.text() == "Measure")
        assert not any(button.text() == "Measure" for button in _shelf_page(window, "Place").findChildren(QPushButton))
        board = Board(thickness_mm=1.6)
        board.outline = [(0, -5), (30, -5), (30, 20), (0, 20)]
        # The pour is listed first and covers the trace, which is how a filled zone arrives.
        board.copper = [
            CopperPoly([(0, -5), (30, -5), (30, 20), (0, 20)], "F.Cu", "GND", "zone"),
            CopperPoly([(0, -0.2), (10, -0.2), (10, 0.2), (0, 0.2)], "F.Cu", "SIG", "trace", feature_mm=0.4),
            CopperPoly([(9.8, 0), (10.2, 0), (10.2, 6), (9.8, 6)], "F.Cu", "SIG", "trace", feature_mm=0.4),
        ]
        window.board = board
        window.project.reference_net = "GND"
        window.view.set_board(board)
        qapp.processEvents()

        measure.click()
        qapp.processEvents()
        assert window.view._tool == "measure"
        assert measure.isChecked()
        assert window._place_stack.currentIndex() == 0
        _click(window.view, 1.0, 0.0)
        assert window.view._measure_anchor is not None
        _click(window.view, 10.0, 5.0)
        qapp.processEvents()
        assert "along SIG" in window.view.measurements()[0][1]
        assert float(window.view.measurements()[0][1].split()[0]) == pytest.approx(14.0, abs=0.4)
        assert "along SIG" in window.statusBar().currentMessage()
        window._set_tool("navigate")
        assert "along SIG" in window.view.measurements()[0][1]

        window._set_tool("measure")
        _click(window.view, 20.0, 15.0)
        _click(window.view, 25.0, 12.0)
        qapp.processEvents()
        marks = window.view.measurements()
        assert len(marks) == 2
        assert "along SIG" in marks[0][1]
        path, label = marks[1]
        assert len(path) == 2
        assert label == f"{math.dist(path[0], path[1]):.2f} mm"
        assert "along" not in label
        assert window.view.selected() == ("measure", 1)

        for index in range(window.results.tabs.count()):
            if window.results.tabs.tabText(index) == "TDR":
                window.results.tabs.setCurrentIndex(index)
        qapp.processEvents()
        line = Line2D([0.0], [50.0])
        line.set_label("P1")
        assert "before the port" in window.results._format_hover(None, line, -0.2, 48.0)
        eps = float(window.results.eps.value())
        mm = float(distance_m([0.4e-9], eps)[0]) * 1e3
        assert f"{mm:.2f} mm along the trace" in window.results._format_hover(None, line, 0.4, 50.0)

        window.project.ports = [PortBox("P1", 50.0, "z", (0.0, -0.1, 0.0), (0.0, 0.1, 0.2), net="SIG")]
        time_s = 0.004 * 2 * math.sqrt(eps) / C0
        window.results._sync_probe(time_s * 1e9)
        qapp.processEvents()
        assert len(window.view._probe_marks) == 1
        x, y, caption = window.view._probe_marks[0]
        assert x == pytest.approx(4.0, abs=0.2)
        assert abs(y) < 0.05
        assert caption == "4.0 mm"
        assert len(window.view._probe_items) == 2

        window.results.eps.setValue(4.0)
        qapp.processEvents()
        assert window.view._probe_marks
        moved = float(distance_m([time_s], 4.0)[0]) * 1e3
        assert window.view._probe_marks[0][0] == pytest.approx(moved, abs=0.2)

        window.results._sync_probe(-0.2)
        qapp.processEvents()
        assert window.view._probe_marks == []
        window.results._sync_probe(time_s * 1e9)
        qapp.processEvents()
        window.results._sync_probe(1.0e6)
        qapp.processEvents()
        assert window.view._probe_marks[0][2] == "end of SIG"
        assert window.view._probe_marks[0][1] == pytest.approx(6.0, abs=0.3)
    finally:
        window.close()


def test_delete_key_removes_the_selected_port_or_measurement(qapp):
    from pcb_simulator.model.board import Board, CopperPoly
    from pcb_simulator.model.mesh import PortBox

    window = MainWindow()
    try:
        window.resize(1200, 800)
        window.show()
        qapp.processEvents()
        board = Board(thickness_mm=1.6)
        board.outline = [(0, -5), (30, -5), (30, 20), (0, 20)]
        board.copper = [
            CopperPoly([(0, -0.2), (10, -0.2), (10, 0.2), (0, 0.2)], "F.Cu", "SIG", "trace", feature_mm=0.4),
        ]
        window.board = board
        window.project.reference_net = "GND"
        window.project.ports = [
            PortBox("P1", 50.0, "y", (0.0, 7.8, 0.0), (6.0, 8.2, 0.2), net="SIG", marks=((0.0, 8.0), (6.0, 8.0))),
            PortBox("P2", 50.0, "y", (12.0, 7.8, 0.0), (16.0, 8.2, 0.2), net="SIG", marks=((12.0, 8.0), (16.0, 8.0))),
        ]
        window.view.set_board(board)
        window.view.set_ports(window.project.ports)
        window._rebuild_ports()
        qapp.processEvents()

        window._on_measure(0.0, 0.0, 10.0, 0.0)
        qapp.processEvents()
        assert window.view.selected() == ("measure", 0)
        assert any(item.data(2) == "selected" and item.data(1) == ("measure", 0) for item in window.view.scene().items())
        window.view.setFocus()
        QTest.keyClick(window.view, Qt.Key.Key_Delete)
        qapp.processEvents()
        assert window.view.measurements() == []
        assert [port.name for port in window.project.ports] == ["P1", "P2"]

        window._on_measure(0.0, 0.0, 10.0, 0.0)
        window._on_measure(0.0, 12.0, 8.0, 12.0)
        assert len(window.view.measurements()) == 2
        assert window.view.selected() == ("measure", 1)
        window._set_tool("navigate")
        qapp.processEvents()
        _click(window.view, 3.0, 8.0)
        qapp.processEvents()
        assert window.view.selected() == ("port", 0)
        assert window.port_list.currentRow() == 0
        assert len(window.view.measurements()) == 2
        window.view.setFocus()
        QTest.keyClick(window.view, Qt.Key.Key_Delete)
        qapp.processEvents()
        assert [port.name for port in window.project.ports] == ["P2"]
        assert len(window.view.measurements()) == 2

        _click(window.view, 4.0, 0.0)
        qapp.processEvents()
        assert window.view.selected() == ("measure", 0)
        window.view.setFocus()
        QTest.keyClick(window.view, Qt.Key.Key_Delete)
        qapp.processEvents()
        assert len(window.view.measurements()) == 1
        assert window.view.measurements()[0][1] == "8.00 mm"
        assert [port.name for port in window.project.ports] == ["P2"]

        window.port_list.setCurrentRow(0)
        qapp.processEvents()
        assert window.view.selected() == ("port", 0)
        window.port_r.setFocus()
        QTest.keyClick(window.port_r, Qt.Key.Key_Delete)
        qapp.processEvents()
        assert [port.name for port in window.project.ports] == ["P2"]

        window._ports_dialog.show()
        qapp.processEvents()
        window.port_list.setFocus()
        QTest.keyClick(window.port_list.viewport(), Qt.Key.Key_Delete)
        qapp.processEvents()
        assert window.project.ports == []
    finally:
        window.close()
