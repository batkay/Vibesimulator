"""DC IR drop and plane impedance on a copper grid. No openEMS."""

import json
import math

import numpy as np
import pytest

from pcb_simulator.model.board import Board, CopperPoly, StackLayer, Via, assign_stackup_z
from pcb_simulator.model.crop import CropSettings
from pcb_simulator.pdn.io import load_result, save_result
from pcb_simulator.project import PdnTerminalRecord, load_project, save_project, suggest_power_net
from pcb_simulator.pdn.circuit import (
    PdnPart,
    PdnTerminal,
    _solve_ac,
    build_grid,
    sheet_resistance_ohm,
    solve_pdn,
    via_resistance_ohm,
)

SIGMA = 5.8e7
PITCH = 0.5


def _board(copper, vias=None, h_mm=0.2) -> Board:
    board = Board(thickness_mm=h_mm)
    board.stackup = [
        StackLayer("F.Cu", "copper", 0.035),
        StackLayer("dielectric 1", "dielectric", h_mm, eps_r=4.5, loss_tangent=0.02),
        StackLayer("B.Cu", "copper", 0.035),
    ]
    assign_stackup_z(board.stackup)
    board.copper = copper
    board.vias = list(vias or [])
    board.outline = [(0.0, 0.0), (4.0, 0.0), (4.0, 2.0), (0.0, 2.0)]
    return board


def _rect(x0, y0, x1, y1, layer, net):
    return CopperPoly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], layer, net, "zone")


def _crop() -> CropSettings:
    return CropSettings(mode="outline", reference_net="GND")


def _ends(board):
    grid = build_grid(board, _crop(), "PWR", PITCH, SIGMA, 0.025)
    vrm = PdnTerminal("VRM", 0.25, 0.25, "PWR", layer="F.Cu", volts=1.0, amps=0.0)
    load = PdnTerminal("U1", 1.75, 0.25, "PWR", layer="F.Cu", volts=0.0, amps=1.0)
    return grid, vrm, load


def test_via_resistance_matches_the_plating_area():
    resistance = via_resistance_ohm(0.2, 0.3, 0.025, SIGMA)
    area = math.pi * 0.025 * (0.3 - 0.025) * 1e-6
    expected = (1.0 / SIGMA) * (0.2 * 1e-3) / area
    assert resistance == pytest.approx(expected)


def test_strip_drop_matches_sheet_squares():
    board = _board([
        _rect(0, 0, 2, 0.5, "F.Cu", "PWR"),
        _rect(0, 0, 2, 0.5, "B.Cu", "GND"),
    ])
    grid, vrm, load = _ends(board)
    result = solve_pdn(grid, "PWR", "GND", vrm, [load], [])
    assert result.ok
    rsheet = sheet_resistance_ohm(0.035, SIGMA)
    # Four cells in a row, so three squares on each plane.
    expected_mv = 2 * 3 * rsheet * 1e3
    assert result.drops[0]["loop_mv"] == pytest.approx(expected_mv, rel=0.02)
    assert result.voltages


def test_load_return_uses_the_ground_click():
    board = _board([
        _rect(0, 0, 2, 0.5, "F.Cu", "PWR"),
        _rect(0, 0, 2, 0.5, "B.Cu", "GND"),
    ])
    grid = build_grid(board, _crop(), "PWR", PITCH, SIGMA, 0.025)
    vrm = PdnTerminal("VRM", 0.25, 0.25, "PWR", layer="F.Cu", volts=1.0, gx=0.25, gy=0.25, g_layer="B.Cu")
    load = PdnTerminal("U1", 1.75, 0.25, "PWR", layer="F.Cu", amps=1.0, gx=0.25, gy=0.25, g_layer="B.Cu")
    result = solve_pdn(grid, "PWR", "GND", vrm, [load], [])
    assert result.ok
    rsheet = sheet_resistance_ohm(0.035, SIGMA)
    # The return sits on the VRM ground cell, so only the power plane's three squares remain.
    assert result.drops[0]["loop_mv"] == pytest.approx(3 * rsheet * 1e3, rel=0.02)
    assert result.drops[0]["ground_mv"] == pytest.approx(0.0, abs=1e-6)


def test_one_via_adds_its_barrel_resistance():
    board = _board(
        [
            _rect(0, 0, 0.5, 0.5, "F.Cu", "PWR"),
            _rect(0, 0, 0.5, 0.5, "B.Cu", "PWR"),
            _rect(0, 0, 0.5, 0.5, "B.Cu", "GND"),
        ],
        vias=[Via(0.25, 0.25, 0.3, 0.6, ("F.Cu", "B.Cu"), "PWR")],
    )
    grid = build_grid(board, _crop(), "PWR", PITCH, SIGMA, 0.025)
    vrm = PdnTerminal("VRM", 0.25, 0.25, "PWR", layer="B.Cu", volts=1.0)
    load = PdnTerminal("U1", 0.25, 0.25, "PWR", layer="F.Cu", amps=1.0)
    result = solve_pdn(grid, "PWR", "GND", vrm, [load], [])
    assert result.ok
    expected = via_resistance_ohm(0.2, 0.3, 0.025, SIGMA) * 1e3
    assert result.drops[0]["loop_mv"] == pytest.approx(expected, rel=0.02)


def test_a_cut_pour_names_the_open_load():
    board = _board([
        _rect(0, 0, 0.5, 0.5, "F.Cu", "PWR"),
        _rect(1.5, 0, 2.0, 0.5, "F.Cu", "PWR"),
        _rect(0, 0, 2.0, 0.5, "B.Cu", "GND"),
    ])
    grid = build_grid(board, _crop(), "PWR", PITCH, SIGMA, 0.025)
    vrm = PdnTerminal("VRM", 0.25, 0.25, "PWR", layer="F.Cu", volts=1.0)
    load = PdnTerminal("U1", 1.75, 0.25, "PWR", layer="F.Cu", amps=1.0)
    result = solve_pdn(grid, "PWR", "GND", vrm, [load], [])
    assert not result.ok
    assert "U1" in result.message or any("U1" in note for note in result.warnings)


def test_a_trace_thinner_than_the_pitch_still_connects():
    board = _board([
        _rect(0, 0.1, 2.0, 0.25, "F.Cu", "PWR"),
        _rect(0, 0, 2.0, 0.5, "B.Cu", "GND"),
    ])
    grid, vrm, load = _ends(board)
    result = solve_pdn(grid, "PWR", "GND", vrm, [load], [])
    assert result.ok
    assert result.drops[0]["connected"]
    assert result.drops[0]["loop_mv"] > 0


def test_series_r_adds_and_a_decap_does_not_conduct_at_dc():
    board = _board([
        _rect(0, 0, 0.5, 0.5, "F.Cu", "PWR"),
        _rect(1.0, 0, 1.5, 0.5, "F.Cu", "PWR"),
        _rect(0, 0, 1.5, 0.5, "B.Cu", "GND"),
    ])
    grid = build_grid(board, _crop(), "PWR", PITCH, SIGMA, 0.025)
    vrm = PdnTerminal("VRM", 0.25, 0.25, "PWR", layer="F.Cu", volts=1.0)
    load = PdnTerminal("U1", 1.25, 0.25, "PWR", layer="F.Cu", amps=1.0)
    series = PdnPart("R1", 0.25, 0.25, "PWR", 1.25, 0.25, "PWR", r_ohm=1.0)
    bridged = solve_pdn(grid, "PWR", "GND", vrm, [load], [series])
    assert bridged.ok
    assert bridged.drops[0]["loop_mv"] == pytest.approx(1000, rel=0.05)
    decap = PdnPart("C1", 1.25, 0.25, "PWR", 1.25, 0.25, "GND", r_ohm=0.01, l_nh=1.0, c_nf=100.0)
    opened = solve_pdn(grid, "PWR", "GND", vrm, [load], [decap])
    assert not opened.ok


def test_low_frequency_impedance_follows_the_plane_capacitance():
    board = _board([
        _rect(0, 0, 1.0, 1.0, "F.Cu", "PWR"),
        _rect(0, 0, 1.0, 1.0, "B.Cu", "GND"),
    ])
    grid = build_grid(board, _crop(), "PWR", PITCH, SIGMA, 0.025)
    assert grid.shunts
    capacitance = sum(shunt.c_f for shunt in grid.shunts)
    ground = grid.shunts[0].ground
    power = grid.shunts[0].power
    load = PdnTerminal("U1", grid.nodes[power].x, grid.nodes[power].y, "PWR", amps=1.0)
    vrm = PdnTerminal("VRM", grid.nodes[ground].x, grid.nodes[ground].y, "GND")
    freq = np.array([1e3])
    impedance = _solve_ac(grid, vrm, power, ground, [(load, power, ground)], [], freq, with_parts=False, with_vrm=False)
    expected = 1.0 / (2 * math.pi * 1e3 * capacitance)
    assert abs(impedance["U1"][0]) == pytest.approx(expected, rel=0.05)


def test_low_frequency_resistance_matches_the_dc_loop():
    board = _board([
        _rect(0, 0, 2, 0.5, "F.Cu", "PWR"),
        _rect(0, 0, 2, 0.5, "B.Cu", "GND"),
    ])
    grid, vrm, load = _ends(board)
    dc = solve_pdn(grid, "PWR", "GND", vrm, [load], [], freq_hz=np.array([1e3]))
    assert dc.ok
    assert dc.impedance["U1"][0].real == pytest.approx(dc.drops[0]["loop_mv"] / 1e3, rel=0.05)


def test_capacitor_resonance_and_vrm_inductance():
    board = _board([
        _rect(0, 0, 0.5, 0.5, "F.Cu", "PWR"),
        _rect(0, 0, 0.5, 0.5, "B.Cu", "GND"),
    ], h_mm=1.0)
    grid = build_grid(board, _crop(), "PWR", PITCH, SIGMA, 0.025)
    vrm = PdnTerminal("VRM", 0.25, 0.25, "PWR", layer="F.Cu", volts=1.0, l_nh=10.0)
    load = PdnTerminal("U1", 0.25, 0.25, "PWR", layer="F.Cu", amps=1.0)
    cap = PdnPart("C1", 0.25, 0.25, "PWR", 0.25, 0.25, "GND", r_ohm=0.05, l_nh=1.0, c_nf=100.0)
    freq = np.geomspace(1e5, 1e8, 80)
    result = solve_pdn(grid, "PWR", "GND", vrm, [load], [cap], freq_hz=freq)
    assert result.ok
    curve = np.abs(result.impedance["U1"])
    resonant = 1.0 / (2 * math.pi * math.sqrt(1e-9 * 100e-9))
    # Below the parallel peak the slope is the VRM inductance. The series
    # dip after that peak sits at 1 / (2 π √(LC)) and the floor is the ESR.
    omega_l = 2 * math.pi * freq[0] * 10e-9
    assert curve[0] == pytest.approx(omega_l, rel=0.25)
    peak_at = int(np.nanargmax(curve))
    dip_at = peak_at + int(np.nanargmin(curve[peak_at:]))
    assert freq[dip_at] == pytest.approx(resonant, rel=0.15)
    assert curve[dip_at] == pytest.approx(0.05, rel=0.25)


def test_old_project_file_still_loads(tmp_path):
    path = tmp_path / "board.pcbsim.json"
    path.write_text(json.dumps({"version": 1, "board": "board.kicad_pcb", "ports": []}), encoding="utf-8")
    project = load_project(path)
    assert project.vrm is None
    assert project.loads == []
    assert project.parts == []
    assert project.boundary == "mur"
    assert project.stop == "crossing"
    assert project.signal_only is True
    assert project.conductivity == 5.8e7
    assert project.pitch_mm == 0.5
    project.vrm = PdnTerminalRecord(
        "VRM", 1.0, 2.0, "VCC", volts=3.45, r_ohm=0.01, l_nh=2.0, gx=4.0, gy=5.0, g_layer="B.Cu", g_pad="2"
    )
    project.power_net = "VCC"
    save_project(path, project)
    again = load_project(path)
    assert again.vrm is not None
    assert again.vrm.volts == pytest.approx(3.45)
    assert again.vrm.l_nh == pytest.approx(2.0)
    assert again.vrm.gx == pytest.approx(4.0)
    assert again.vrm.gy == pytest.approx(5.0)
    assert again.vrm.g_layer == "B.Cu"
    assert again.power_net == "VCC"
    assert suggest_power_net(["GND", "USB_D_P", "VCC"]) == "VCC"
    assert suggest_power_net(["GND", "VCC", "VDD"]) == ""


def test_saved_pdn_reloads_without_pickle(tmp_path):
    board = _board([
        _rect(0, 0, 2, 0.5, "F.Cu", "PWR"),
        _rect(0, 0, 2, 0.5, "B.Cu", "GND"),
    ])
    grid, vrm, load = _ends(board)
    result = solve_pdn(grid, "PWR", "GND", vrm, [load], [], freq_hz=np.array([1e3, 1e6]), ripple_mv=50, step_a=2)
    folder = tmp_path / "pdn"
    save_result(folder, result)
    loaded = load_result(folder)
    assert loaded is not None and loaded.ok
    assert loaded.drops[0]["name"] == "U1"
    assert loaded.drops[0]["loop_mv"] == pytest.approx(result.drops[0]["loop_mv"])
    assert loaded.z_target == pytest.approx(0.025)
    assert loaded.impedance["U1"][0] == pytest.approx(result.impedance["U1"][0])
    assert loaded.impedance_bare["U1"][1] == pytest.approx(result.impedance_bare["U1"][1])
    assert np.isfinite(loaded.impedance["U1"][0])


def test_target_line_is_ripple_over_current():
    board = _board([
        _rect(0, 0, 0.5, 0.5, "F.Cu", "PWR"),
        _rect(0, 0, 0.5, 0.5, "B.Cu", "GND"),
    ])
    grid = build_grid(board, _crop(), "PWR", PITCH, SIGMA, 0.025)
    vrm = PdnTerminal("VRM", 0.25, 0.25, "PWR", layer="F.Cu", volts=3.3)
    load = PdnTerminal("U1", 0.25, 0.25, "PWR", layer="F.Cu", amps=0.2)
    result = solve_pdn(grid, "PWR", "GND", vrm, [load], [], freq_hz=np.array([1e3]), ripple_mv=50, step_a=2)
    assert result.z_target == pytest.approx(0.025)
