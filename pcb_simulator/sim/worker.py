"""Run one job directory with openEMS.

Launch with the solver interpreter, not the GUI interpreter:

    python -m pcb_simulator.sim.worker path/to/job_dir

Progress lines are ``PCB_SIM`` plus a JSON object. Results land in
``results.npz``. Exit 0 on success, 2 if openEMS cannot be imported, 3 on a
geometry or solver error.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np

from ..calc.s_parameters import s_column
from ..calc.waveforms import port_waveforms
from .job import load_job


def _emit(payload: dict) -> None:
    print("PCB_SIM " + json.dumps(payload), flush=True)


# Courant number openEMS uses for the default timestep. Lines in the job are millimetres.
_CFL = 0.99
_C0 = 299_792_458.0


def crossing_steps(job: dict) -> int | None:
    """Steps for one round trip, a short wait for the open, and the Gaussian pulse.

    The pulse length matches ``SetGaussExcite(0, f_max)``: ``ceil(9 / (pi * f_max) / dt)``.
    Travel is out and back along ``route_mm`` when the job has it, at
    ``c / sqrt(max eps_r)``, then 15% of that trip plus ``2/f_max`` so the end
    reflection can rise. With no route, the distance is the longest mesh span
    and that wait is omitted.
    ``dt`` is ``0.99 / (c * sqrt(1/dx^2 + 1/dy^2 + 1/dz^2))`` with the gaps in metres.
    Returns None when the mesh has no cell to measure.
    """

    mesh = job.get("mesh") or {}
    gaps_m: list[float] = []
    spans_m: list[float] = []
    for key in ("x", "y", "z"):
        lines = sorted(float(value) for value in (mesh.get(key) or []))
        if len(lines) < 2:
            return None
        gaps = [b - a for a, b in zip(lines, lines[1:]) if b - a > 1e-12]
        if not gaps:
            return None
        gaps_m.append(min(gaps) * 1e-3)
        spans_m.append((lines[-1] - lines[0]) * 1e-3)
    inverse = math.sqrt(sum(1.0 / (gap * gap) for gap in gaps_m))
    if inverse <= 0:
        return None
    dt = _CFL / (_C0 * inverse)
    if dt <= 0:
        return None
    eps = 1.0
    for mat in job.get("materials") or []:
        if not isinstance(mat, dict):
            continue
        for key in ("eps_r", "eps_inf"):
            raw = mat.get(key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value > eps:
                eps = value
    distance, kind = _crossing_distance(job, spans_m)
    travel = 2.0 * distance / (_C0 / math.sqrt(eps))
    pulse = 0.0
    try:
        fc = float(job.get("f_max_hz") or 0.0)
    except (TypeError, ValueError):
        fc = 0.0
    if fc > 0:
        pulse = 9.0 / (math.pi * fc)
    # A fraction of the trip covers a route measured a little short, and a wave
    # that is not exactly at the bulk dielectric speed. The edge term follows
    # f max, so a short trace does not inherit a fixed wait.
    settle = 0.0
    if kind == "trace":
        settle = 0.15 * travel
        if fc > 0:
            settle += 2.0 / fc
    return max(1, math.ceil((travel + pulse + settle) / dt))


def _crossing_distance(job: dict, spans_m: list[float]) -> tuple[float, str]:
    """Metres to travel, and ``trace`` or ``mesh``.

    A round trip is twice this distance. The trace length is the copper route.
    The mesh span is the whole box, which is longer than the route.
    """
    raw = job.get("route_mm")
    try:
        route_mm = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        route_mm = 0.0
    if route_mm > 0:
        return route_mm * 1e-3, "trace"
    return max(spans_m), "mesh"


def apply_stop(job: dict) -> dict:
    """Lower ``nr_ts`` in memory for a crossing stop. The job file stays as written.

    The typed cap still wins when it is the smaller number. Energy mode leaves
    the cap alone. Reaching the trace round trip is a finished run. Reaching
    the typed cap before that trip is not.
    """

    if str(job.get("stop") or "") != "crossing":
        return job
    try:
        cap = int(job.get("nr_ts") or 1_000_000)
    except (TypeError, ValueError):
        cap = 1_000_000
    cap = max(cap, 1)
    steps = crossing_steps(job)
    if steps is None:
        print(
            f"Step cap stays {cap:,}. The crossing length could not be estimated from this mesh.",
            flush=True,
        )
        return job
    reduced = max(1, min(cap, steps))
    _distance, kind = _crossing_kind(job)
    if reduced < cap:
        if kind == "trace":
            route = float(job["route_mm"])
            print(
                f"Step cap for this run is {reduced:,}. That is a trip to the end of the "
                f"longest trace ({route:.0f} mm) and back, plus a short wait for the open "
                f"at that end, plus the source pulse. "
                f"The typed cap is {cap:,}. The energy limit can stop earlier.",
                flush=True,
            )
        else:
            print(
                f"Step cap for this run is {reduced:,}. That covers two crossings of the mesh "
                f"plus the source pulse. The typed cap is {cap:,}. The energy limit can stop earlier.",
                flush=True,
            )
    else:
        print(
            f"Step cap for this run is {cap:,}, the typed cap. "
            f"A round trip would be {steps:,} steps.",
            flush=True,
        )
    updated = {**job, "nr_ts": reduced}
    if kind == "trace" and reduced < cap:
        updated["stop_kind"] = "trace"
    return updated


def _crossing_kind(job: dict) -> tuple[float, str]:
    """Same distance choice as ``crossing_steps``, for the log line."""
    mesh = job.get("mesh") or {}
    spans_m: list[float] = []
    for key in ("x", "y", "z"):
        lines = [float(value) for value in (mesh.get(key) or [])]
        if len(lines) >= 2:
            spans_m.append((max(lines) - min(lines)) * 1e-3)
    if not spans_m:
        return 0.0, "mesh"
    return _crossing_distance(job, spans_m)


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


# The Debye timestep extension prints this name. v0.37.0-rc1 has the property
# and not the extension. Its Lorentz updater then reads a pole strength as a
# plasma frequency and a relaxation time as a resonant frequency.
# On Windows the name is inside openEMS.dll. The .pyd is only the Python
# wrapper, and the v0.37.0 wheel does not contain the string.
_DEBYE_MARK = "Debye Dispersive Material Extension"
_debye_ready: bool | None = None
_debye_noted = False


def engine_supports_debye(blob: bytes) -> bool:
    """True when ``blob`` is an openEMS binary that timesteps a Debye material."""
    mark = _DEBYE_MARK.encode("utf-8")
    return mark in blob or _DEBYE_MARK.encode("utf-16le") in blob


def _engine_files() -> list[Path]:
    """Python wrapper plus the DLL CSXCAD loads for the timestep updater."""
    import openEMS

    root = Path(openEMS.__file__).resolve().parent
    paths = [root / "__init__.py", *sorted(root.glob("openEMS*.pyd"))]
    for key in ("OPENEMS_INSTALL_PATH", "CSXCAD_INSTALL_PATH"):
        base = os.environ.get(key)
        if base:
            paths.append(Path(base) / "openEMS.dll")
    return paths


def _openems_supports_debye() -> bool:
    global _debye_ready
    if _debye_ready is None:
        blob = bytearray()
        for path in _engine_files():
            if path.is_file():
                blob.extend(path.read_bytes())
        _debye_ready = engine_supports_debye(bytes(blob))
    return _debye_ready


def _note_constant_dielectric() -> None:
    global _debye_noted
    if _debye_noted:
        return
    _debye_noted = True
    print(
        "This openEMS cannot timestep the loss-tangent fit, "
        "so each layer stays at its stackup εr with no loss.",
        flush=True,
    )


def _dielectric(csx, mat: dict):
    """Constant εr when tanδ is 0, and on an openEMS with no Debye updater.

    Poles are applied only when this binary contains the Debye updater.
    The stackup εr stays the material epsilon in the constant case. eps_inf
    is the high-frequency end of the fit and is not the typed εr.
    The installed CSXCAD binding accepts Python float only. A numpy scalar
    misses the property name and the call fails.
    """
    poles = mat.get("debye") or []
    if not poles or not _openems_supports_debye():
        if poles:
            _note_constant_dielectric()
        prop = csx.AddMaterial(mat["name"])
        prop.SetMaterialProperty(epsilon=float(mat["eps_r"]), kappa=float(mat.get("kappa") or 0.0))
        return prop
    from CSXCAD.CSProperties import CSPropDebyeMaterial

    prop = CSPropDebyeMaterial(
        csx.GetParameterSet(),
        name=str(mat["name"]),
        order=len(poles),
        epsilon=float(mat["eps_inf"]),
        kappa=float(mat.get("kappa") or 0.0),
    )
    for index, pole in enumerate(poles):
        prop.SetDispersiveMaterialProperty(
            index,
            eps_delta=float(pole["delta"]),
            eps_relax=float(pole["tau"]),
        )
    csx.AddProperty(prop)
    return prop


def _build(csx, job: dict, excite_index: int, tick=None):
    # A PDN job sets copper to "sheet". S-parameter jobs leave it unset and stay PEC.
    metal = None if job.get("copper") == "sheet" else csx.AddMetal("copper")
    foils: dict[float, object] = {}
    lossy = None
    dielectric = sum(len(mat["polygons"]) for mat in job["materials"])
    extras = len(job.get("elements") or []) + len(job.get("shorts") or [])
    total = len(job["metals"]) + len(job["vias"]) + dielectric + extras
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
        _copper(csx, job, metal, foils, item).AddPolygon(
            _points(item["polygon"]), "z", float(item["z"]), priority=int(item["priority"])
        )
        step()
    for via in job["vias"]:
        if via.get("kappa"):
            if lossy is None:
                lossy = csx.AddMaterial("via_copper")
                lossy.SetMaterialProperty(epsilon=1.0, kappa=float(via["kappa"]))
            target = lossy
        else:
            target = metal
        target.AddCylinder(
            [via["x"], via["y"], via["z0"]],
            [via["x"], via["y"], via["z1"]],
            float(via["r"]),
            priority=int(via["priority"]),
        )
        step()
    shorts = job.get("shorts") or []
    if shorts:
        pec = csx.AddMetal("vrm_short")
        for short in shorts:
            pec.AddBox(list(short["start"]), list(short["stop"]))
            step()
    for element in job.get("elements") or []:
        _add_lumped(csx, element)
        step()
    for mat in job["materials"]:
        prop = _dielectric(csx, mat)
        height = float(mat["z1"]) - float(mat["z0"])
        for tri in mat["polygons"]:
            prop.AddLinPoly(_points(tri), "z", float(mat["z0"]), height, priority=0)
            step()
    import openEMS
    fdtd = openEMS.openEMS(NrTS=int(job["nr_ts"]), EndCriteria=float(job["end_criteria"]))
    fdtd.SetCSX(csx)
    if str(job.get("boundary") or "pml") == "mur":
        fdtd.SetBoundaryCond(["MUR"] * 6)
    else:
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


def _copper(csx, job, metal, foils, item):
    """PEC for an S-parameter job. One conducting sheet per foil thickness for a PDN job.

    Both planes of a stack usually share a thickness, so they are two polygons
    on one sheet. Thickness is passed in metres, as in the openEMS 35 µm example.
    """
    if metal is not None:
        return metal
    thickness = float(item.get("thickness_m") or 35e-6)
    found = foils.get(thickness)
    if found is not None:
        return found
    foil = csx.AddConductingSheet(
        f"foil_{len(foils)}",
        conductivity=float(job.get("conductivity") or 5.8e7),
        thickness=thickness,
    )
    foils[thickness] = foil
    return foil


def _add_lumped(csx, item: dict) -> None:
    """Series RLC. A pure inductor is a port instead, because that element often fails to decay."""
    values = {}
    if item.get("r_ohm"):
        values["R"] = float(item["r_ohm"])
    if item.get("l_h"):
        values["L"] = float(item["l_h"])
    if item.get("c_f"):
        values["C"] = float(item["c_f"])
    if not values:
        return
    kwargs = {"caps": True, **values}
    try:
        from CSXCAD.CSProperties import LEtype

        kwargs["LEtype"] = LEtype.LE_SERIES
    except Exception:
        kwargs["LEtype"] = 1
    try:
        element = csx.AddLumpedElement(str(item["name"]), str(item["ny"]), **kwargs)
    except TypeError:
        kwargs.pop("LEtype", None)
        element = csx.AddLumpedElement(str(item["name"]), str(item["ny"]), **kwargs)
    element.AddBox(list(item["start"]), list(item["stop"]))


def _completed(flag: bool | None, job: dict) -> bool | None:
    """True when this excitation finished the window the run asked for.

    Stopping on the energy limit is complete. Stopping at the trace round trip
    is complete. Stopping on the typed step cap before that trip is not.
    None stays None so a missing stats file is still reported as unfinished.
    """
    if flag is None:
        return None
    if flag:
        return True
    return job.get("stop_kind") == "trace"


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
    job = apply_stop(load_job(job_path))
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
        flag = _completed(_decayed(sim_path, int(job["nr_ts"])), job)
        decayed_flags.append(flag)
        try:
            for port in ports:
                port.CalcPort(str(sim_path), freq, ref_impedance=z0[ports.index(port)])
            S[:, :, k] = s_column([port.uf_ref for port in ports], ports[k].uf_inc)
            time_s, voltage, current = port_waveforms(ports, k)
            saved_time[f"t_{k}"] = time_s
            saved_time[f"v_{k}"] = voltage
            saved_time[f"i_{k}"] = current
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
