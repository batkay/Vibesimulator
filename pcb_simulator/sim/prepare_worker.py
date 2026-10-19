"""Build the mesh and job file outside the GUI process.

The mesh is pure Python plus shapely. Running it on a thread in the window
still holds the interpreter lock, so the window cannot repaint. This module
is started as its own process: python -m pcb_simulator.sim.prepare_worker REQUEST.pkl
"""

from __future__ import annotations

import json
import pickle
import shutil
import sys
import traceback
from pathlib import Path

from .job import prepare, write_job


def _emit(payload: dict) -> None:
    print("PCB_PREP " + json.dumps(payload), flush=True)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m pcb_simulator.sim.prepare_worker REQUEST.pkl", file=sys.stderr)
        return 2
    request_path = Path(argv[0])
    try:
        request = pickle.loads(request_path.read_bytes())
        job, mesh, warnings = prepare(
            request["board"],
            request["crop"],
            request["ports"],
            request["mesh_settings"],
            n_freq=int(request["n_freq"]),
            end_criteria=float(request["end_criteria"]),
            nr_ts=int(request["nr_ts"]),
            threads=request["threads"],
            progress=_emit,
        )
        job_dir = Path(request["job_dir"])
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        job_dir.mkdir(parents=True, exist_ok=True)
        _emit({"event": "write"})
        write_job(job_dir, job)
        (job_dir / "prep.pkl").write_bytes(
            pickle.dumps({"mesh": mesh, "warnings": list(warnings)}, protocol=pickle.HIGHEST_PROTOCOL)
        )
    except Exception as exc:
        traceback.print_exc()
        _emit({"event": "error", "message": str(exc)})
        return 3
    _emit(
        {
            "event": "ready",
            "count": mesh.cell_count,
            "cells": list(mesh.cells),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
