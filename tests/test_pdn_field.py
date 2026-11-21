"""PDN field job and the Z reduction. No openEMS run."""

import numpy as np
import pytest

from pcb_simulator.model.board import Board, CopperPoly, StackLayer, Via, assign_stackup_z
from pcb_simulator.model.crop import CropSettings
from pcb_simulator.pdn.circuit import PdnPart, PdnTerminal
from pcb_simulator.pdn.em import reduce_impedance
from pcb_simulator.pdn.io import load_result, save_result
from pcb_simulator.pdn.circuit import PdnResult
from pcb_simulator.sim.job import pdn_mesh_settings, prepare_pdn

SIGMA = 5.8e7


def _board() -> Board:
    board = Board(thickness_mm=0.2)
    board.stackup = [
        StackLayer("F.Cu", "copper", 0.035),
        StackLayer("dielectric 1", "dielectric", 0.2, eps_r=4.5, loss_tangent=0.02),
        StackLayer("B.Cu", "copper", 0.035),
    ]
    assign_stackup_z(board.stackup)
    board.copper = [
        CopperPoly([(0, 0), (4, 0), (4, 2), (0, 2)], "F.Cu", "PWR", "zone"),
        CopperPoly([(0, 0), (4, 0), (4, 2), (0, 2)], "B.Cu", "GND", "zone"),
    ]
    board.vias = [Via(2.0, 1.0, 0.3, 0.6, ("F.Cu", "B.Cu"), "PWR")]
    board.outline = [(0.0, 0.0), (4.0, 0.0), (4.0, 2.0), (0.0, 2.0)]
    return board


def _terminals():
    vrm = PdnTerminal("VRM", 0.5, 0.5, "PWR", layer="F.Cu", volts=1.0, gx=0.5, gy=0.5, g_layer="B.Cu")
    load = PdnTerminal("U1", 3.5, 0.5, "PWR", layer="F.Cu", amps=1.0, gx=3.5, gy=0.5, g_layer="B.Cu")
    return vrm, load


def test_plane_mesh_is_coarser_than_the_signal_mesh():
    plane = pdn_mesh_settings(1e9, signal=False)
    signal = pdn_mesh_settings(1e9, signal=True)
    assert plane.copper_cell_mm == pytest.approx(1.0)
    assert plane.min_cell_mm == pytest.approx(0.2)
    assert plane.z_cells_per_dielectric == 2
    assert signal.copper_cell_mm == pytest.approx(0.2)
    assert signal.min_cell_mm == pytest.approx(0.05)
    assert signal.z_cells_per_dielectric == 4


def test_field_job_uses_a_conducting_sheet_and_a_vrm_short():
    vrm, load = _terminals()
    cap = PdnPart("C1", 2.0, 0.4, "PWR", 2.0, 0.8, "GND", r_ohm=0.01, l_nh=1.0, c_nf=100.0, layer_a="F.Cu", layer_b="B.Cu")
    inductor = PdnPart("L1", 2.5, 0.4, "PWR", 2.5, 0.8, "GND", l_nh=2.0, layer_a="F.Cu", layer_b="B.Cu")
    job, _mesh, warnings = prepare_pdn(
        _board(),
        CropSettings(mode="outline", reference_net="GND"),
        "PWR",
        "GND",
        vrm,
        [load],
        [cap, inductor],
        pdn_mesh_settings(1e9, signal=False),
        SIGMA,
    )
    assert job["copper"] == "sheet"
    assert job["conductivity"] == pytest.approx(SIGMA)
    assert job["metals"]
    assert all(item["thickness_m"] == pytest.approx(35e-6) for item in job["metals"])
    assert {round(item["z"], 3) for item in job["metals"]} == {0.0, 0.2}
    assert job["vias"][0]["kappa"] == pytest.approx(SIGMA)
    assert job["shorts"] and job["shorts"][0]["name"] == "VRM"
    assert job["load_ports"] == ["U1"]
    assert job["ports"][0]["name"] == "U1"
    assert job["ports"][0]["R"] == pytest.approx(0.1)
    assert any(item["name"] == "C1" and item["c_f"] == pytest.approx(100e-9) for item in job["elements"])
    assert any(item["name"] == "L1" and item["l_h"] == pytest.approx(2e-9) for item in job["stamps"])
    assert "L1" in {port["name"] for port in job["ports"]}
    assert any("drill radius" in note for note in warnings)


def test_closing_an_inductor_port_reduces_the_load_impedance():
    # Z = [[10, 1], [1, 5]]. Closing port 1 through a short leaves 10 - 1/5.
    impedance = np.array([[[10, 1], [1, 5]]], dtype=complex)
    reduced = reduce_impedance(impedance, [0], {1: np.array([0])})
    assert reduced[0, 0, 0] == pytest.approx(9.8)


def test_field_curve_roundtrip(tmp_path):
    result = PdnResult(
        True,
        "PDN finished.",
        [{"name": "U1", "loop_mv": 1.0, "power_mv": 1.0, "ground_mv": 0.0, "connected": True}],
        [],
        [],
        np.array([1e3]),
        {"U1": np.array([1 + 0j])},
        {"U1": np.array([1 + 0j])},
        None,
        [],
        field_freq_hz=np.array([1e6, 2e6]),
        field_impedance={"U1": np.array([0.2 + 0.1j, 0.3 + 0.2j])},
        field_decayed=False,
    )
    save_result(tmp_path, result)
    loaded = load_result(tmp_path)
    assert loaded is not None
    assert loaded.field_decayed is False
    assert loaded.field_freq_hz[1] == pytest.approx(2e6)
    assert loaded.field_impedance["U1"][0] == pytest.approx(0.2 + 0.1j)
