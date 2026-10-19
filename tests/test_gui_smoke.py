"""Offscreen check that the window loads a board, takes ports, and draws results."""

import os
import shutil
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QHeaderView, QPushButton

from pcb_simulator.app import FitLabel, MainWindow, project_file_for, solver_exit_message
from pcb_simulator.view.board_view import _decimate
from pcb_simulator.sim.postprocess import tdr_impedance, tdr_view_window


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
    _click(window.view, 10, 0)
    qapp.processEvents()
    assert [port.exc_dir for port in window.project.ports] == ["z", "z"]
    assert [port.footprint for port in window.project.ports] == ["J1", "J2"]
    assert window.view.highlight_net == ""
    assert window.port_list.item(0).text() == "P1   J1.1   SIG   50.0 Ω"
    assert window.port_list.item(1).text() == "P2   J2.1   SIG   50.0 Ω"
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
    assert min(x0, x1) == pytest.approx(1)
    assert max(x0, x1) == pytest.approx(6)

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
    assert "did not decay" in window.results.banner.text()
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
    window.preflight.setText(
        "243 × 370 × 50 = 4,495,500 cells. Rough memory 450 MB. "
        "Cells grade from 0.050 mm on copper to 2.83 mm in open space. This is a large mesh."
    )
    for width in (1000, 1200, 1400, 1900):
        window.resize(width, 800)
        qapp.processEvents()
        bar = window._left_scroll.horizontalScrollBar()
        assert bar.maximum() == 0
        assert window.net_list.horizontalScrollBar().maximum() == 0
        assert window.stack_table.horizontalScrollBar().maximum() == 0
        for label in window._left_scroll.findChildren(FitLabel):
            if not label.text().strip():
                continue
            assert label.height() + 2 >= label.heightForWidth(max(label.width(), 1))
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
    QTest.mouseClick(f_max, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    popup = _popup()
    assert popup.isVisible()
    assert popup.title.text() == "f max"
    assert "copper lattice" in popup.body.text()
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


def test_click_a_pad_works_again_after_two_click(qapp, tmp_path: Path):
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
    _click(window.view, 10, 0)
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
    assert window.view._port_anchor is None
    assert not any(item.data(0) == "port-preview" for item in window.view.scene().items())
    assert not any(item.data(0) == "port-preview-line" for item in window.view.scene().items())
    window.close()


def test_side_panes_track_the_window(qapp):
    window = MainWindow()
    window.show()
    window.resize(1200, 800)
    qapp.processEvents()
    narrow = list(window._splitter.sizes())
    window.resize(1900, 800)
    qapp.processEvents()
    wide = list(window._splitter.sizes())
    assert wide[0] > narrow[0] + 20
    assert wide[2] > narrow[2] + 20
    assert wide[1] > narrow[1]
    assert window.stack_table.horizontalHeader().sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch
    window.close()


def _legend_labels(window):
    ax = window.results.figure.axes[0]
    legend = ax.get_legend()
    if legend is None:
        return []
    return [text.get_text() for text in legend.get_texts()]


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
