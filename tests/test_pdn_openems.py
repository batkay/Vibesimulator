"""Two conducting sheets against the sheet-solve resistance and a cavity mode.

Slow. Do not run it unless asked. A second sheet that carries no current fails
the resistance check, and that is the stop for the PDN field button.
"""

import math
from pathlib import Path

import numpy as np
import pytest

from pcb_simulator.model.board import Board, CopperPoly, StackLayer, assign_stackup_z
from pcb_simulator.model.crop import CropSettings
from pcb_simulator.model.mesh import MeshSettings
from pcb_simulator.pdn.circuit import PdnTerminal, build_grid, solve_pdn
from pcb_simulator.pdn.em import field_impedance
from pcb_simulator.sim.job import prepare_pdn, write_job

MU0 = 1.25663706212e-6
EPS0 = 8.854187817e-12
SIGMA = 5.8e7


def _cavity_hz(m: int, n: int, width_m: float, height_m: float, eps_r: float) -> float:
    return (1.0 / (2.0 * math.sqrt(MU0 * EPS0 * eps_r))) * math.sqrt((m / width_m) ** 2 + (n / height_m) ** 2)


@pytest.mark.slow
def test_two_sheets_match_the_loop_resistance_and_a_cavity_mode(tmp_path: Path):
    pytest.importorskip("openEMS")
    pytest.importorskip("CSXCAD")
    from pcb_simulator.calc.load import load_results
    from pcb_simulator.sim.worker import run_directory

    width, height, gap = 30.0, 20.0, 0.2
    eps_r = 4.0
    board = Board(thickness_mm=gap)
    board.stackup = [
        StackLayer("F.Cu", "copper", 0.035),
        StackLayer("dielectric 1", "dielectric", gap, eps_r=eps_r, loss_tangent=0.02),
        StackLayer("B.Cu", "copper", 0.035),
    ]
    assign_stackup_z(board.stackup)
    board.copper = [
        CopperPoly([(0, 0), (width, 0), (width, height), (0, height)], "F.Cu", "PWR", "zone"),
        CopperPoly([(0, 0), (width, 0), (width, height), (0, height)], "B.Cu", "GND", "zone"),
    ]
    board.outline = [(0.0, 0.0), (width, 0.0), (width, height), (0.0, height)]
    vrm = PdnTerminal("VRM", 1.0, height / 2, "PWR", layer="F.Cu", volts=1.0, gx=1.0, gy=height / 2, g_layer="B.Cu")
    load = PdnTerminal(
        "U1", width - 1.0, height / 2, "PWR", layer="F.Cu", amps=1.0, gx=width - 1.0, gy=height / 2, g_layer="B.Cu"
    )
    grid = build_grid(board, CropSettings(mode="outline", reference_net="GND"), "PWR", 0.5, SIGMA, 0.025)
    dc = solve_pdn(grid, "PWR", "GND", vrm, [load], [])
    assert dc.ok
    loop_ohm = dc.drops[0]["loop_mv"] * 1e-3 / load.amps
    mesh = MeshSettings(
        f_max_hz=3e9,
        cells_per_feature=2,
        min_cell_mm=0.2,
        copper_cell_mm=2.0,
        z_cells_per_dielectric=1,
    )
    job, _built, _warnings = prepare_pdn(
        board,
        CropSettings(mode="outline", reference_net="GND"),
        "PWR",
        "GND",
        vrm,
        [load],
        [],
        mesh,
        SIGMA,
        n_freq=81,
        end_criteria=1e-3,
        nr_ts=200_000,
    )
    assert len({round(item["z"], 3) for item in job["metals"]}) == 2
    write_job(tmp_path, job)
    assert run_directory(tmp_path) == 0
    results = load_results(tmp_path / "results.npz")
    assert results["decayed"]
    curves = field_impedance(
        results["freq"], results["S"], results["z0"], results["port_names"], job["load_ports"], job["stamps"]
    )
    impedance = curves["U1"]
    assert np.real(impedance[0]) == pytest.approx(loop_ohm, rel=0.2)
    magnitude = np.abs(impedance)
    peak = 1 + int(np.argmax(magnitude[1:]))
    modes = [
        _cavity_hz(m, n, width * 1e-3, height * 1e-3, eps_r)
        for m, n in ((1, 0), (0, 1), (1, 1), (2, 0))
    ]
    nearest = min(modes, key=lambda mode: abs(mode - results["freq"][peak]))
    assert results["freq"][peak] == pytest.approx(nearest, rel=0.1)
