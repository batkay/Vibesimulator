"""ODB++ and IPC-2581 import. KiCad stays on its own path and is not Y-flipped."""

import os
import zipfile
from pathlib import Path

import pytest
from shapely.geometry import Point

from pcb_simulator.model.geom import as_polygon
from pcb_simulator.model.load import load_board
from pcb_simulator.model.odb import symbol_mm


def test_symbol_numbers_follow_the_file_unit():
    assert symbol_mm(200, "MM") == pytest.approx(0.2)
    assert symbol_mm(600, "MM") == pytest.approx(0.6)
    assert symbol_mm(0.6, "MM") == pytest.approx(0.6)
    assert symbol_mm(10.5, "INCH") == pytest.approx(10.5 / 1000 * 25.4)
    assert symbol_mm(25, "INCH") == pytest.approx(0.635)
    assert symbol_mm(600, "INCH", "M") == pytest.approx(0.6)
    assert symbol_mm(10, "MM", "I") == pytest.approx(0.254)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _demo_job(root: Path) -> None:
    _write(
        root / "matrix" / "matrix",
        """
UNITS=MM
STEP {
    COL=1
    NAME=pcb
}
LAYER {
    ROW=1
    CONTEXT=BOARD
    TYPE=SIGNAL
    NAME=top
    POLARITY=POSITIVE
}
LAYER {
    ROW=2
    CONTEXT=BOARD
    TYPE=DIELECTRIC
    NAME=core
    POLARITY=POSITIVE
}
LAYER {
    ROW=3
    CONTEXT=BOARD
    TYPE=SIGNAL
    NAME=bottom
    POLARITY=POSITIVE
}
LAYER {
    ROW=4
    CONTEXT=BOARD
    TYPE=DRILL
    NAME=drill
    POLARITY=POSITIVE
    START_NAME=top
    END_NAME=bottom
}
""",
    )
    _write(
        root / "steps" / "pcb" / "profile",
        """
U MM
S P 0
OB 0 0 I
OS 10 0
OS 10 5
OS 0 5
OS 0 0
OE
SE
""",
    )
    _write(
        root / "steps" / "pcb" / "layers" / "top" / "features",
        """
U MM
$0 r600
$1 rect1000x400
L 1 1 4 1 0 P 0
P 6 2 1 P 0 0
""",
    )
    _write(
        root / "steps" / "pcb" / "layers" / "drill" / "features",
        """
U MM
$0 r300
P 6 2 0 P 0 0
""",
    )
    _write(
        root / "steps" / "pcb" / "layers" / "core" / "attrlist",
        """
.dielectric_constant=4.2
.loss_tangent=0.015
.layer_thickness=1.5
""",
    )
    _write(
        root / "steps" / "pcb" / "eda" / "data",
        """
LYR top bottom drill
NET GND
SNT TRC
FID C 0 0
NET SIG
FID C 0 1
""",
    )


def _check_demo(board) -> None:
    trace = next(poly for poly in board.copper if poly.kind == "trace")
    ys = [point[1] for point in trace.points]
    assert trace.layer == "top"
    assert trace.net == "GND"
    assert trace.feature_mm == pytest.approx(0.6)
    assert min(ys) == pytest.approx(-1.3, abs=0.03)
    assert max(ys) == pytest.approx(-0.7, abs=0.03)

    assert len(board.pads) == 1
    pad = board.pads[0]
    assert pad.net == "SIG"
    assert pad.footprint == "top"
    assert pad.center == pytest.approx((6, -2))
    xs = [point[0] for point in pad.points]
    ys = [point[1] for point in pad.points]
    assert max(xs) - min(xs) == pytest.approx(1.0)
    assert max(ys) - min(ys) == pytest.approx(0.4)

    assert len(board.vias) == 1
    via = board.vias[0]
    assert (via.x, via.y) == pytest.approx((6, -2))
    assert via.drill_mm == pytest.approx(0.3)
    assert via.layers == ("top", "bottom")

    dielectric = next(layer for layer in board.stackup if layer.kind == "dielectric")
    assert dielectric.eps_r == pytest.approx(4.2)
    assert dielectric.loss_tangent == pytest.approx(0.015)
    assert dielectric.thickness_mm == pytest.approx(1.5)
    assert board.assumed_stackup is False
    assert board.layer_z("bottom") == pytest.approx(0.0)
    assert board.layer_z("top") == pytest.approx(1.5)
    assert any(abs(x - 10) < 1e-6 and abs(y + 5) < 1e-6 for x, y in board.outline)


def test_odb_folder_imports_copper_drill_and_stackup(tmp_path: Path):
    root = tmp_path / "job"
    _demo_job(root)
    _check_demo(load_board(root))


def test_odb_zip_round_trip(tmp_path: Path):
    root = tmp_path / "job"
    _demo_job(root)
    archive = tmp_path / "job.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for path in root.rglob("*"):
            if path.is_file():
                zipped.write(path, Path("job").joinpath(path.relative_to(root)).as_posix())
    _check_demo(load_board(archive))


def test_odb_millimeter_decimal_symbol_is_not_a_micron(tmp_path: Path):
    root = tmp_path / "dec"
    _write(
        root / "matrix" / "matrix",
        """
UNITS=MM
STEP { NAME=pcb }
LAYER { ROW=1 TYPE=SIGNAL NAME=top POLARITY=POSITIVE }
""",
    )
    _write(
        root / "steps" / "pcb" / "layers" / "top" / "features",
        """
U MM
$0 r0.6
L 0 0 2 0 0 P 0
""",
    )
    board = load_board(root)
    trace = board.copper[0]
    ys = [point[1] for point in trace.points]
    assert max(ys) - min(ys) == pytest.approx(0.6, abs=0.03)


def test_odb_negative_plane_keeps_the_clearance(tmp_path: Path):
    root = tmp_path / "plane"
    _write(
        root / "matrix" / "matrix",
        """
UNITS=MM
STEP { NAME=pcb }
LAYER { ROW=1 TYPE=SIGNAL NAME=top POLARITY=NEGATIVE }
""",
    )
    _write(
        root / "steps" / "pcb" / "profile",
        """
U MM
S P 0
OB 0 0 I
OS 10 0
OS 10 10
OS 0 10
OS 0 0
OE
SE
""",
    )
    _write(
        root / "steps" / "pcb" / "layers" / "top" / "features",
        """
U MM
$0 r1000
P 5 5 0 P 0 0
""",
    )
    _write(root / "steps" / "pcb" / "layers" / "top" / "attrlist", ".net=GND\n")
    board = load_board(root)
    assert board.pads == []
    geom = as_polygon(board.copper[0].points, board.copper[0].holes)
    assert geom is not None
    assert geom.covers(Point(1, -1))
    assert not geom.covers(Point(5, -5))
    assert board.copper[0].net == "GND"


def test_ipc2581_imports_pad_trace_via_and_stackup(tmp_path: Path):
    path = tmp_path / "demo.xml"
    path.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<IPC-2581 revision="B">
  <Ecad name="demo">
    <CadHeader units="MILLIMETER"/>
    <CadData>
      <Layer name="TOP" layerFunction="CONDUCTOR" side="TOP"/>
      <Layer name="BOTTOM" layerFunction="CONDUCTOR" side="BOTTOM"/>
      <Layer name="CORE" layerFunction="DIELCORE"/>
      <Stackup name="stack">
        <StackupGroup>
          <StackupLayer layerOrGroupRef="TOP" thickness="0.035" sequence="1"/>
          <StackupLayer layerOrGroupRef="CORE" thickness="1.53" sequence="2">
            <Material name="FR4" dielectricConstant="4.8" lossTangent="0.02"/>
          </StackupLayer>
          <StackupLayer layerOrGroupRef="BOTTOM" thickness="0.035" sequence="3"/>
        </StackupGroup>
      </Stackup>
      <Step name="PCB">
        <Profile>
          <Polygon>
            <PolyBegin x="0" y="0"/>
            <PolyStepSegment x="10" y="0"/>
            <PolyStepSegment x="10" y="4"/>
            <PolyStepSegment x="0" y="4"/>
            <PolyStepSegment x="0" y="0"/>
          </Polygon>
        </Profile>
        <LayerFeature layerRef="TOP">
          <Set net="SIG">
            <Features>
              <Polyline>
                <PolyBegin x="1" y="1"/>
                <PolyStepSegment x="3" y="1"/>
                <LineDesc lineWidth="0.2" lineEnd="ROUND"/>
              </Polyline>
              <Pad>
                <Location x="5" y="2"/>
                <StandardPrimitiveRef id="PAD"/>
              </Pad>
            </Features>
          </Set>
        </LayerFeature>
        <LayerFeature layerRef="BOTTOM">
          <Set net="GND">
            <Features>
              <Contour>
                <Polygon>
                  <PolyBegin x="0" y="0"/>
                  <PolyStepSegment x="10" y="0"/>
                  <PolyStepSegment x="10" y="4"/>
                  <PolyStepSegment x="0" y="4"/>
                  <PolyStepSegment x="0" y="0"/>
                </Polygon>
              </Contour>
            </Features>
          </Set>
        </LayerFeature>
        <Hole name="V1" diameter="0.3" platingStatus="PLATED" shape="CIRCLE">
          <Location x="5" y="2"/>
        </Hole>
      </Step>
      <DictionaryStandard>
        <Entry id="PAD">
          <StandardPrimitive>
            <RectCenter width="1.0" height="0.5"/>
          </StandardPrimitive>
        </Entry>
      </DictionaryStandard>
    </CadData>
  </Ecad>
</IPC-2581>
""",
        encoding="utf-8",
    )
    board = load_board(path)
    pad = board.pads[0]
    assert pad.center == pytest.approx((5, -2))
    assert pad.net == "SIG"
    assert pad.footprint == "TOP"
    xs = [point[0] for point in pad.points]
    ys = [point[1] for point in pad.points]
    assert max(xs) - min(xs) == pytest.approx(1.0)
    assert max(ys) - min(ys) == pytest.approx(0.5)
    trace = next(poly for poly in board.copper if poly.kind == "trace")
    ys = [point[1] for point in trace.points]
    assert min(ys) == pytest.approx(-1.1, abs=0.03)
    assert max(ys) == pytest.approx(-0.9, abs=0.03)
    assert board.vias[0].drill_mm == pytest.approx(0.3)
    assert (board.vias[0].x, board.vias[0].y) == pytest.approx((5, -2))
    assert board.vias[0].layers == ("TOP", "BOTTOM")
    dielectric = next(layer for layer in board.stackup if layer.kind == "dielectric")
    assert dielectric.eps_r == pytest.approx(4.8)
    assert dielectric.thickness_mm == pytest.approx(1.53)
    assert dielectric.loss_tangent == pytest.approx(0.02)
    assert any(abs(x - 10) < 1e-6 and abs(y + 4) < 1e-6 for x, y in board.outline)
    assert board.assumed_stackup is False


def test_native_altium_and_xpedition_ask_for_an_export(tmp_path: Path):
    pcbdoc = tmp_path / "board.PcbDoc"
    pcbdoc.write_bytes(b"not a board")
    with pytest.raises(ValueError, match="ODB\\+\\+|IPC-2581"):
        load_board(pcbdoc)
    native = tmp_path / "board.pcb"
    native.write_text("xpedition", encoding="utf-8")
    with pytest.raises(ValueError, match="ODB\\+\\+|IPC-2581"):
        load_board(native)


def test_kicad_dispatch_does_not_flip_y(tmp_path: Path):
    path = tmp_path / "tiny.kicad_pcb"
    path.write_text(
        """
        (kicad_pcb (version 20240108)
          (layers (0 "F.Cu" signal))
          (footprint "R" (layer "F.Cu") (at 0 0)
            (pad "1" smd rect (at 0 5) (size 1 1) (layers "F.Cu") (net "S"))
          )
        )
        """,
        encoding="utf-8",
    )
    board = load_board(path)
    assert board.pads[0].center == pytest.approx((0, 5))


def test_zip_slip_is_rejected(tmp_path: Path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../evil.txt", "nope")
    with pytest.raises(ValueError, match="escapes"):
        load_board(archive)


def test_project_file_sits_beside_an_odb_folder(tmp_path: Path):
    pytest.importorskip("PySide6")
    from pcb_simulator.app import project_file_for
    from pcb_simulator.project import board_path_for, save_project, Project

    folder = tmp_path / "job"
    folder.mkdir()
    assert project_file_for(folder) == tmp_path / "job.pcbsim.json"
    assert project_file_for(tmp_path / "board.zip") == tmp_path / "board.pcbsim.json"
    project = Project(board=folder.name)
    save_project(tmp_path / "job.pcbsim.json", project)
    assert board_path_for(tmp_path / "job.pcbsim.json", project) == folder.resolve()


def test_window_opens_an_odb_folder(tmp_path: Path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from pcb_simulator.app import MainWindow, project_file_for

    QApplication.instance() or QApplication([])
    root = tmp_path / "job"
    _demo_job(root)
    window = MainWindow()
    window.open_board(root)
    assert window.board is not None
    assert window.board.pads
    window.save_project()
    project = project_file_for(root)
    assert project == tmp_path / "job.pcbsim.json"
    assert project.is_file()
    again = MainWindow()
    again.open_project(project)
    assert again.board is not None
    assert again.board_path == root.resolve()
    assert again.board.pads[0].net == "SIG"
    window.close()
    again.close()
