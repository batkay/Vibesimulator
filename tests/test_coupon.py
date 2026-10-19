"""The 50 ohm coupon is the geometry the solver is checked against."""

import subprocess
import sys
from pathlib import Path

from pcb_simulator.model.crop import CropSettings
from pcb_simulator.model.kicad import load_kicad_pcb
from pcb_simulator.model.mesh import MeshSettings
from pcb_simulator.model.ports import vertical_port_on_pad
from pcb_simulator.sim.job import prepare

from examples.make_microstrip import write


def test_coupon_ports_and_mesh_stay_under_the_gate(tmp_path: Path):
    board = load_kicad_pcb(write(tmp_path / "microstrip_50ohm.kicad_pcb"))
    assert board.layer_z("F.Cu") > board.layer_z("B.Cu")
    ports = []
    for pad in board.pads:
        port = vertical_port_on_pad(board, pad, "GND", ports)
        assert port is not None
        ports.append(port)
    job, mesh, _warnings = prepare(
        board,
        CropSettings(mode="outline", reference_net="GND"),
        ports,
        MeshSettings(f_max_hz=5e9, cells_per_feature=2),
    )
    assert [port["exc_dir"] for port in job["ports"]] == ["z", "z"]
    assert mesh.cell_count < 500_000


def test_prepare_worker_writes_the_job_outside_the_gui(tmp_path: Path):
    import pickle

    from pcb_simulator.sim.prepare_worker import main

    board = load_kicad_pcb(write(tmp_path / "microstrip_50ohm.kicad_pcb"))
    ports = []
    for pad in board.pads:
        port = vertical_port_on_pad(board, pad, "GND", ports)
        assert port is not None
        ports.append(port)
    job_dir = tmp_path / "job"
    request = tmp_path / "request.pkl"
    request.write_bytes(
        pickle.dumps(
            {
                "board": board,
                "crop": CropSettings(mode="outline", reference_net="GND"),
                "ports": ports,
                "mesh_settings": MeshSettings(f_max_hz=5e9, cells_per_feature=2),
                "n_freq": 401,
                "end_criteria": 1e-4,
                "nr_ts": 1_000_000,
                "threads": 0,
                "job_dir": str(job_dir),
            }
        )
    )
    assert main([str(request)]) == 0
    assert (job_dir / "job.json").is_file()
    loaded = pickle.loads((job_dir / "prep.pkl").read_bytes())
    assert loaded["mesh"].cell_count < 500_000


def test_worker_module_does_not_import_the_gui():
    root = Path(__file__).resolve().parents[1]
    script = "import pcb_simulator.sim.worker, sys; assert not any(name.startswith('PySide6') for name in sys.modules)"
    subprocess.check_call([sys.executable, "-c", script], cwd=root)


def test_prepare_reports_each_stage(tmp_path: Path):
    from pcb_simulator.sim.progress import describe_prep, describe_sim, prep_failure_message

    board = load_kicad_pcb(write(tmp_path / "microstrip_50ohm.kicad_pcb"))
    ports = []
    for pad in board.pads:
        port = vertical_port_on_pad(board, pad, "GND", ports)
        assert port is not None
        ports.append(port)
    seen = []
    _job, mesh, _warnings = prepare(
        board,
        CropSettings(mode="outline", reference_net="GND"),
        ports,
        MeshSettings(f_max_hz=5e9, cells_per_feature=2),
        progress=seen.append,
    )
    events = [item["event"] for item in seen]
    assert events[0] == "crop"
    assert "mesh" in events
    assert "cells" in events
    assert "triangles" in events
    cells = next(item for item in seen if item["event"] == "cells")
    assert cells["count"] == mesh.cell_count
    assert f"{mesh.cell_count:,}" in describe_prep(cells)
    assert "timestep log" in describe_sim(
        {"event": "operator", "elapsed_s": 0, "cells": mesh.cell_count, "min_mm": 0.05, "count": 1}
    )
    assert "10 s" in describe_sim(
        {"event": "operator", "elapsed_s": 10, "cells": mesh.cell_count, "min_mm": 0.05, "port": "P1", "index": 0, "count": 2}
    )
    failed = 'PCB_PREP {"event": "crop", "done": 0, "total": 3}\nPCB_PREP {"event": "error", "message": "no copper"}\n'
    assert prep_failure_message(failed, 3) == "no copper"


def test_auto_threads_is_a_real_core_count():
    import os

    from pcb_simulator.sim.worker import _thread_count

    assert _thread_count(0) == (os.cpu_count() or 1)
    assert _thread_count(None) == (os.cpu_count() or 1)
    assert _thread_count(4) == 4
