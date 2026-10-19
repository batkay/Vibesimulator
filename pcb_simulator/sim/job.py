"""Build the JSON job the solver worker runs.

The mesh in the job is the mesh the preview drew. The worker does not invent
a second grid.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from ..model.board import Board
from ..model.crop import CropSettings, crop_board, region_ring
from ..model.geom import solid_triangles
from ..model.mesh import Mesh, MeshSettings, PortBox, build_mesh
from ..model.ports import port_inside_region

EPS0 = 8.854187817e-12


def kappa_for(eps_r: float, loss_tangent: float, f_center_hz: float) -> float:
    """Constant conductivity that matches tanδ at one frequency. openEMS has no tanδ."""
    return 2 * math.pi * f_center_hz * EPS0 * eps_r * loss_tangent


def prepare(
    board: Board,
    crop: CropSettings,
    ports: list[PortBox],
    mesh_settings: MeshSettings,
    n_freq: int = 401,
    end_criteria: float = 1e-4,
    nr_ts: int = 1_000_000,
    threads: int | None = None,
    progress=None,
) -> tuple[dict, Mesh, list[str]]:
    warnings: list[str] = []
    cropped = crop_board(board, crop, progress=progress)
    warnings.extend(cropped.warnings)
    if cropped.region.is_empty or not region_ring(cropped.region):
        raise ValueError("The simulation region is empty.")
    usable = []
    for port in ports:
        if not port_inside_region(port, cropped.region):
            warnings.append(f"{port.name} is outside the simulation region and was left out.")
            continue
        usable.append(port)
    if not usable:
        raise ValueError("Add at least one port inside the simulation region.")
    if not cropped.copper:
        raise ValueError("The simulation region has no copper.")
    if progress is not None:
        progress({"event": "mesh"})
    mesh = build_mesh(board, cropped, usable, mesh_settings)
    if progress is not None:
        progress(
            {
                "event": "cells",
                "count": mesh.cell_count,
                "cells": list(mesh.cells),
                "min_mm": mesh.dx_mm,
            }
        )
    snapped = [port.snapped(mesh.x, mesh.y, mesh.z) for port in usable]
    for port in snapped:
        if abs(port.start[{"x": 0, "y": 1, "z": 2}[port.exc_dir]] - port.stop[{"x": 0, "y": 1, "z": 2}[port.exc_dir]]) < 1e-9:
            raise ValueError(f"{port.name} collapsed onto one mesh line. Move it or refine the mesh.")
    job = _job_dict(
        board, cropped, snapped, mesh, mesh_settings,
        n_freq=n_freq, end_criteria=end_criteria, nr_ts=nr_ts, threads=threads,
        progress=progress,
    )
    warnings.extend(mesh.warnings)
    return job, mesh, warnings


def write_job(directory: str | Path, job: dict) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    return path


def _job_dict(
    board: Board,
    cropped,
    ports: list[PortBox],
    mesh: Mesh,
    settings: MeshSettings,
    n_freq: int,
    end_criteria: float,
    nr_ts: int,
    threads: int | None,
    progress=None,
) -> dict:
    f_center = settings.f_max_hz / 2
    ring = region_ring(cropped.region)
    holes = [[(float(x), float(y)) for x, y in list(r.coords)[:-1]] for r in cropped.region.interiors]
    # One outline for every dielectric. The slabs share the crop, so the triangles are the same.
    outline = solid_triangles(ring, holes)
    materials = []
    for index, diel in enumerate(board.dielectrics()):
        eps = diel.eps_r if diel.eps_r is not None else 4.5
        tan = diel.loss_tangent if diel.loss_tangent is not None else 0.0
        materials.append(
            {
                "name": f"diel_{index + 1}",
                "eps_r": eps,
                "kappa": kappa_for(eps, tan, f_center),
                "z0": diel.z_mm,
                "z1": (diel.z_mm or 0.0) + diel.thickness_mm,
                # Tessellated here so the solver process does not need shapely.
                "polygons": outline,
            }
        )
    metals = []
    total = len(cropped.copper)
    last = time.monotonic()
    for index, poly in enumerate(cropped.copper):
        if progress is not None and (index == 0 or time.monotonic() - last >= 2.0):
            progress({"event": "triangles", "done": index, "total": total})
            last = time.monotonic()
        z = board.layer_z(poly.layer)
        if z is None:
            continue
        priority = {"zone": 1, "trace": 2, "pad": 3, "via": 4}.get(poly.kind, 2)
        for tri in solid_triangles(poly.points, poly.holes):
            if len(tri) >= 3:
                metals.append({"z": z, "priority": priority, "polygon": tri})
    vias = []
    for via in cropped.vias:
        z0, z1 = _via_z(board, via)
        if z0 is None or abs(z1 - z0) < 1e-9 or via.drill_mm <= 0:
            continue
        vias.append({"x": via.x, "y": via.y, "r": via.drill_mm / 2, "z0": min(z0, z1), "z1": max(z0, z1), "priority": 4})
    return {
        "f_max_hz": settings.f_max_hz,
        "n_freq": n_freq,
        "end_criteria": end_criteria,
        "nr_ts": nr_ts,
        "threads": threads,
        "pml": settings.pml_cells,
        "materials": materials,
        "metals": metals,
        "vias": vias,
        "ports": [
            {
                "name": port.name,
                "R": port.r_ohm,
                "exc_dir": port.exc_dir,
                "start": list(port.start),
                "stop": list(port.stop),
            }
            for port in ports
        ],
        "mesh": {"x": mesh.x, "y": mesh.y, "z": mesh.z},
        "port_names": [port.name for port in ports],
    }


def _via_z(board: Board, via) -> tuple[float | None, float | None]:
    names = board.copper_names_top_to_bottom()
    start, end = via.layers
    if start in names and end in names:
        i, j = names.index(start), names.index(end)
        spanned = names[min(i, j) : max(i, j) + 1]
    else:
        spanned = [start, end]
    zs = [board.layer_z(name) for name in spanned]
    zs = [z for z in zs if z is not None]
    if not zs:
        return None, None
    return min(zs), max(zs)


def load_job(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
