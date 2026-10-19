"""Run one job directory with openEMS.

Launch with the solver interpreter, not the GUI interpreter:

    python -m pcb_simulator.sim.worker path/to/job_dir

Progress lines are ``PCB_SIM`` plus a JSON object. Results land in
``results.npz``. Exit 0 on success, 2 if openEMS cannot be imported, 3 on a
geometry or solver error.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np

from .job import load_job


def _emit(payload: dict) -> None:
    print("PCB_SIM " + json.dumps(payload), flush=True)


def _operator_watch(stop: threading.Event, fields: dict, started: float, sim_path: Path) -> None:
    """openEMS walks every cell with no progress line. Say how long that has taken.

    The run-stats file appears when the timestep loop starts, which ends this watch.
    """
    stats = sim_path / "openEMS_run_stats.txt"
    while not stop.wait(10):
        if stats.is_file():
            return
        _emit({"event": "operator", "elapsed_s": int(time.perf_counter() - started), **fields})


def _thread_count(value) -> int:
    """Cores for the FDTD loop.

    openEMS 0.37 treats numThreads 0 as "start at 1 and search upward". The
    operator setup then uses every core, and the timestep loop stays on one
    core until that search moves. Passing the machine's core count starts
    the loop on all of them.
    """
    try:
        count = int(value) if value else 0
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        count = os.cpu_count() or 1
    return max(count, 1)


def _points(polygon) -> np.ndarray:
    arr = np.asarray(polygon, dtype=float)
    if arr.ndim != 2 or arr.shape[1] != 2:
        raise ValueError("A polygon must be a list of [x, y] points")
    return np.ascontiguousarray(arr.T)


def _min_cell_mm(mesh: dict) -> float:
    spans = []
    for axis in ("x", "y", "z"):
        lines = mesh.get(axis) or []
        if len(lines) < 2:
            continue
        spans.append(min(abs(float(b) - float(a)) for a, b in zip(lines, lines[1:])))
    return min(spans) if spans else 0.0


def _build(csx, job: dict, excite_index: int, tick=None):
    metal = csx.AddMetal("copper")
    dielectric = sum(len(mat["polygons"]) for mat in job["materials"])
    total = len(job["metals"]) + len(job["vias"]) + dielectric
    done = 0
    last = time.monotonic()

    def step() -> None:
        nonlocal done, last
        done += 1
        now = time.monotonic()
        if tick is not None and done < total and now - last >= 2.0:
            last = now
            tick(done, total)

    for item in job["metals"]:
        metal.AddPolygon(_points(item["polygon"]), "z", float(item["z"]), priority=int(item["priority"]))
        step()
    for via in job["vias"]:
        metal.AddCylinder(
            [via["x"], via["y"], via["z0"]],
            [via["x"], via["y"], via["z1"]],
            float(via["r"]),
            priority=int(via["priority"]),
        )
        step()
    for mat in job["materials"]:
        prop = csx.AddMaterial(mat["name"])
        prop.SetMaterialProperty(epsilon=float(mat["eps_r"]), kappa=float(mat["kappa"]))
        height = float(mat["z1"]) - float(mat["z0"])
        for tri in mat["polygons"]:
            prop.AddLinPoly(_points(tri), "z", float(mat["z0"]), height, priority=0)
            step()
    import openEMS
    fdtd = openEMS.openEMS(NrTS=int(job["nr_ts"]), EndCriteria=float(job["end_criteria"]))
    fdtd.SetCSX(csx)
    pml = int(job.get("pml") or 8)
    fdtd.SetBoundaryCond([f"PML_{pml}"] * 6)
    fdtd.SetGaussExcite(0, float(job["f_max_hz"]))
    ports = []
    for index, port in enumerate(job["ports"]):
        excite = 1 if index == excite_index else 0
        ports.append(
            fdtd.AddLumpedPort(
                index + 1,
                float(port["R"]),
                port["start"],
                port["stop"],
                port["exc_dir"],
                excite=excite,
            )
        )
    return fdtd, ports


def _decayed(sim_path: Path, nr_ts: int) -> bool | None:
    """True when openEMS stopped before the timestep cap, which is the energy limit."""
    stats = sim_path / "openEMS_stats.txt"
    if stats.is_file():
        for line in stats.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "number of iterations" in line.lower():
                try:
                    iterations = int(float(line.split()[0]))
                except ValueError:
                    break
                return iterations < int(nr_ts)
    return None


def run_directory(directory: Path) -> int:
    job_path = directory / "job.json"
    if not job_path.is_file():
        _emit({"event": "error", "message": f"No job.json in {directory}"})
        return 3
    job = load_job(job_path)
    try:
        from CSXCAD import ContinuousStructure
        import openEMS  # noqa: F401
    except Exception as exc:
        _emit({"event": "error", "message": f"openEMS is not importable in this Python: {exc}"})
        return 2

    n_ports = len(job["ports"])
    if n_ports < 1:
        _emit({"event": "error", "message": "The job has no ports"})
        return 3
    n_freq = int(job.get("n_freq") or 401)
    f_max = float(job["f_max_hz"])
    freq = np.linspace(f_max / n_freq, f_max, n_freq)
    mesh = job["mesh"]
    cells = (len(mesh["x"]) - 1, len(mesh["y"]) - 1, len(mesh["z"]) - 1)
    _emit({"event": "start", "cells": list(cells), "ports": n_ports})

    S = np.zeros((n_freq, n_ports, n_ports), dtype=complex)
    z0 = np.array([float(port["R"]) for port in job["ports"]], dtype=float)
    names = [port["name"] for port in job["ports"]]
    saved_time = {}
    decayed_flags = []
    threads = _thread_count(job.get("threads"))
    _emit({"event": "threads", "count": threads})

    for k in range(n_ports):
        _emit({"event": "excite", "index": k, "count": n_ports, "port": names[k]})
        sim_path = (directory / f"exc_{k}").resolve()
        if sim_path.exists():
            import shutil
            shutil.rmtree(sim_path, ignore_errors=True)
        sim_path.mkdir(parents=True)
        try:
            from CSXCAD import ContinuousStructure
            csx = ContinuousStructure()
            grid = csx.GetGrid()
            grid.SetDeltaUnit(1e-3)
            grid.SetLines("x", np.asarray(mesh["x"], dtype=float))
            grid.SetLines("y", np.asarray(mesh["y"], dtype=float))
            grid.SetLines("z", np.asarray(mesh["z"], dtype=float))
            n_diel = sum(len(mat["polygons"]) for mat in job["materials"])
            _emit(
                {
                    "event": "geometry",
                    "metals": len(job["metals"]),
                    "vias": len(job["vias"]),
                    "dielectric": n_diel,
                }
            )
            fdtd, ports = _build(
                csx,
                job,
                k,
                tick=lambda done, total: _emit({"event": "geometry", "done": done, "total": total}),
            )
            operator = {
                "cells": int(cells[0] * cells[1] * cells[2]),
                "min_mm": round(_min_cell_mm(mesh), 4),
                "port": names[k],
                "index": k,
                "count": n_ports,
            }
            _emit({"event": "operator", "elapsed_s": 0, **operator})
            stop = threading.Event()
            started = time.perf_counter()
            watch = threading.Thread(
                target=_operator_watch,
                args=(stop, operator, started, sim_path),
                daemon=True,
            )
            watch.start()
            run_kw = {"numThreads": threads, "dump_statistics": True}
            try:
                try:
                    fdtd.Run(str(sim_path), verbose=1, **run_kw)
                except TypeError:
                    fdtd.Run(str(sim_path))
            finally:
                stop.set()
                watch.join(timeout=1)
        except Exception as exc:
            _emit({"event": "error", "message": str(exc), "trace": traceback.format_exc()})
            return 3
        flag = _decayed(sim_path, int(job["nr_ts"]))
        decayed_flags.append(flag)
        try:
            for port in ports:
                port.CalcPort(str(sim_path), freq, ref_impedance=z0[ports.index(port)])
            inc = np.asarray(ports[k].uf_inc, dtype=complex)
            safe = np.where(np.abs(inc) < 1e-18, np.nan, inc)
            for i, port in enumerate(ports):
                S[:, i, k] = np.asarray(port.uf_ref, dtype=complex) / safe
            nt = len(ports[k].u_time)
            v = np.zeros((n_ports, nt), dtype=float)
            i_arr = np.zeros((n_ports, nt), dtype=float)
            for i, port in enumerate(ports):
                vv = np.asarray(port.ut_tot, dtype=float)
                ii = np.asarray(port.it_tot, dtype=float)
                n = min(nt, len(vv), len(ii))
                v[i, :n] = vv[:n]
                i_arr[i, :n] = ii[:n]
            saved_time[f"t_{k}"] = np.asarray(ports[k].u_time, dtype=float)
            saved_time[f"v_{k}"] = v
            saved_time[f"i_{k}"] = i_arr
        except Exception as exc:
            _emit({"event": "error", "message": f"Post-processing excitation {k + 1} failed: {exc}"})
            return 3

    known = [flag for flag in decayed_flags if flag is not None]
    # Unknown is reported as not decayed so the plot warns instead of hiding it.
    decayed = bool(known) and all(known)
    out = directory / "results.npz"
    np.savez(
        out,
        freq=freq,
        S=S,
        z0=z0,
        port_names=np.array(names),
        decayed=np.array([decayed]),
        **saved_time,
    )
    _emit({"event": "done", "results": str(out), "decayed": decayed})
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m pcb_simulator.sim.worker JOB_DIR", file=sys.stderr)
        return 3
    try:
        return run_directory(Path(argv[0]))
    except Exception as exc:
        _emit({"event": "error", "message": str(exc), "trace": traceback.format_exc()})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
