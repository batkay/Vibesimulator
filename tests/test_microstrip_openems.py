"""Field-solver check of the 50 ohm coupon. Skipped unless openEMS imports."""

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("openEMS")
pytest.importorskip("CSXCAD")

from examples.make_microstrip import width_for_z0, write
from pcb_simulator.model.crop import CropSettings
from pcb_simulator.model.kicad import load_kicad_pcb
from pcb_simulator.model.mesh import MeshSettings
from pcb_simulator.model.ports import vertical_port_on_pad
from pcb_simulator.sim.job import prepare, write_job
from pcb_simulator.sim.postprocess import distance_m, load_results, network, tdr_impedance
from pcb_simulator.sim.worker import run_directory


@pytest.mark.slow
def test_microstrip_impedance_and_transmission(tmp_path: Path):
    _width, _z0, eeff = width_for_z0()
    board = load_kicad_pcb(write(tmp_path / "microstrip_50ohm.kicad_pcb"))
    ports = []
    for pad in board.pads:
        port = vertical_port_on_pad(board, pad, "GND", ports, r_ohm=50)
        assert port is not None
        ports.append(port)
    job, mesh, _warnings = prepare(
        board,
        CropSettings(mode="outline", reference_net="GND"),
        ports,
        # The passing run used exact feature edges. The app default is now a 0.2 mm lattice.
        MeshSettings(f_max_hz=5e9, cells_per_feature=2, min_cell_mm=0, copper_cell_mm=0),
        n_freq=101,
    )
    assert mesh.cell_count < 500_000
    job_dir = tmp_path / "job"
    write_job(job_dir, job)
    assert run_directory(job_dir) == 0
    results = load_results(job_dir / "results.npz")
    s21 = np.abs(results["S"][:, 0, 1])
    low = max(1, len(s21) // 10)
    assert float(np.max(20 * np.log10(np.maximum(s21[:low], 1e-15)))) > -1.0
    ntwk = network(results["freq"], results["S"], results["z0"])
    time_s, impedance = tdr_impedance(ntwk, 0)
    distance = distance_m(time_s, eeff)
    # At 5 GHz the step is about 8 mm long, so a 2–8 mm slice can miss every
    # sample. Keep the samples that sit on the 10 mm line, after the step.
    on_line = (distance >= 0.0) & (distance < 0.012)
    assert np.count_nonzero(on_line) >= 1
    sample = impedance[on_line]
    assert abs(float(np.median(sample)) - 50.0) / 50.0 < 0.15
