"""Build the JSON job the solver worker runs.

The mesh in the job is the mesh the preview drew. The worker does not invent
a second grid.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from ..model.board import Board
from ..model.crop import CropSettings, crop_board, region_ring
from ..model.geom import solid_triangles
from ..model.mesh import Mesh, MeshSettings, PortBox, build_mesh
from ..model.ports import port_between, port_inside_region
from ..model.trace_path import route_length
from .djordjevic import dielectric_fields

# Port reference for the PDN field run. It is not a load on the board.
PDN_PORT_OHM = 0.1
_OZ_M = 35e-6


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
    stop: str = "energy",
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
        progress=progress, stop=stop,
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
    stop: str = "energy",
) -> dict:
    ring = region_ring(cropped.region)
    holes = [[(float(x), float(y)) for x, y in list(r.coords)[:-1]] for r in cropped.region.interiors]
    # One outline for every dielectric. The slabs share the crop, so the triangles are the same.
    outline = solid_triangles(ring, holes)
    materials = []
    for index, diel in enumerate(board.dielectrics()):
        # tanδ above 0 is the 10 GHz point of a Djordjevic–Sarkar Debye fit.
        # A constant conductivity matched at one frequency is far too lossy
        # at the bottom of the band, and the TDR then falls along a trace
        # whose width does not change. Blank or 0 stays a constant εr.
        materials.append(
            {
                "name": f"diel_{index + 1}",
                **dielectric_fields(diel.eps_r, diel.loss_tangent),
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
    payload = {
        "f_max_hz": settings.f_max_hz,
        "n_freq": n_freq,
        "end_criteria": end_criteria,
        "nr_ts": nr_ts,
        "threads": threads,
        "pml": settings.pml_cells,
        "boundary": settings.boundary if settings.boundary in ("mur", "pml") else "pml",
        "stop": stop if stop in ("crossing", "energy") else "energy",
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
    route_mm = _longest_route_mm(board, ports)
    if route_mm is not None:
        payload["route_mm"] = route_mm
    return payload


def _longest_route_mm(board: Board, ports: list[PortBox]) -> float | None:
    """Longest port route. The timestep cap has to cover every port."""
    longest = 0.0
    found = False
    for port in ports:
        if not port.net:
            continue
        origin = ((port.start[0] + port.stop[0]) / 2, (port.start[1] + port.stop[1]) / 2)
        length = route_length(board, port.net, origin)
        if length is None:
            continue
        found = True
        longest = max(longest, length)
    if not found:
        return None
    return longest


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


def pdn_mesh_settings(f_max_hz: float, signal: bool) -> MeshSettings:
    """Plane mesh, or the S-parameter mesh when ``signal`` is set.

    The plane pitch is 1 mm with two cells through each dielectric. The signal
    mesh is the app default: 0.2 mm copper, four dielectric cells, 0.05 mm floor.
    """
    if signal:
        return MeshSettings(f_max_hz=f_max_hz, mesh_f_hz=f_max_hz)
    return MeshSettings(
        f_max_hz=f_max_hz,
        mesh_f_hz=f_max_hz,
        cells_per_feature=2,
        min_cell_mm=0.2,
        copper_cell_mm=1.0,
        z_cells_per_dielectric=2,
    )


def prepare_pdn(
    board: Board,
    crop: CropSettings,
    power_net: str,
    reference_net: str,
    vrm,
    loads: list,
    parts: list,
    mesh_settings: MeshSettings,
    conductivity: float,
    n_freq: int = 201,
    end_criteria: float = 1e-4,
    nr_ts: int = 1_000_000,
    threads: int | None = None,
    progress=None,
) -> tuple[dict, Mesh, list[str]]:
    """Field job for the power and reference nets. The sheet solve is separate."""
    warnings: list[str] = []
    if vrm is None:
        raise ValueError("Place a VRM on the power net.")
    if not loads:
        raise ValueError("Place at least one load.")
    narrowed = CropSettings(
        mode=crop.mode,
        rect=crop.rect,
        margin_mm=crop.margin_mm,
        selected_nets=[power_net] if power_net else [],
        reference_net=reference_net,
    )
    cropped = crop_board(board, narrowed, progress=progress)
    warnings.extend(cropped.warnings)
    if cropped.region.is_empty or not region_ring(cropped.region):
        raise ValueError("The simulation region is empty.")
    ports, extras, stamps, notes = _pdn_excitation(board, power_net, reference_net, vrm, loads, parts)
    warnings.extend(notes)
    load_names = {load.name for load in loads}
    if not any(port.name in load_names for port in ports):
        raise ValueError("Add at least one load whose clicks land on copper.")
    if cropped.vias:
        warnings.append(
            "Vias are solid cylinders of the drill radius. A plated barrel has less copper than that."
        )
    if progress is not None:
        progress({"event": "mesh"})
    seeded = list(ports) + [item["box"] for item in extras]
    mesh = build_mesh(board, cropped, seeded, mesh_settings)
    if progress is not None:
        progress(
            {
                "event": "cells",
                "count": mesh.cell_count,
                "cells": list(mesh.cells),
                "min_mm": mesh.dx_mm,
            }
        )
    snapped = [port.snapped(mesh.x, mesh.y, mesh.z) for port in ports]
    for port in snapped:
        _reject_collapsed(port)
    job = _job_dict(
        board, cropped, snapped, mesh, mesh_settings,
        n_freq=n_freq, end_criteria=end_criteria, nr_ts=nr_ts, threads=threads,
        progress=progress, stop="energy",
    )
    _tag_sheets(board, job, conductivity)
    elements = []
    shorts = []
    for item in extras:
        box = item["box"].snapped(mesh.x, mesh.y, mesh.z)
        _reject_collapsed(box)
        record = {
            "name": box.name,
            "ny": box.exc_dir,
            "start": list(box.start),
            "stop": list(box.stop),
        }
        if item["kind"] == "short":
            shorts.append(record)
        else:
            record.update(item["values"])
            elements.append(record)
    job["elements"] = elements
    job["shorts"] = shorts
    job["load_ports"] = [port.name for port in ports if port.name not in {stamp["name"] for stamp in stamps}]
    job["stamps"] = stamps
    warnings.extend(mesh.warnings)
    return job, mesh, warnings


def _reject_collapsed(port: PortBox) -> None:
    axis = {"x": 0, "y": 1, "z": 2}[port.exc_dir]
    if abs(port.start[axis] - port.stop[axis]) < 1e-9:
        raise ValueError(f"{port.name} collapsed onto one mesh line. Move it or refine the mesh.")


def _tag_sheets(board: Board, job: dict, conductivity: float) -> None:
    """Lossy foil. Thickness is metres, the unit in the openEMS 35 µm example."""
    job["copper"] = "sheet"
    job["conductivity"] = float(conductivity)
    for item in job["metals"]:
        item["thickness_m"] = _thickness_m(board, float(item["z"]))
    for via in job["vias"]:
        via["kappa"] = float(conductivity)


def _thickness_m(board: Board, z_mm: float) -> float:
    for layer in board.stackup:
        if layer.kind != "copper" or layer.z_mm is None:
            continue
        if abs(layer.z_mm - z_mm) < 1e-6:
            mm = layer.thickness_mm if layer.thickness_mm > 0 else 0.035
            return mm * 1e-3
    return _OZ_M


def _return_layer(board: Board, layer: str) -> str:
    names = board.copper_names_top_to_bottom()
    if not names:
        return layer
    if layer and layer in names and len(names) > 1:
        return names[-1] if names[0] == layer else names[0]
    return names[-1]


def _ends(term, board: Board) -> tuple[tuple[float, float, str], tuple[float, float, str]]:
    layer = term.layer or _return_layer(board, "")
    ground = term.g_layer or _return_layer(board, layer)
    gx = term.x if term.gx is None else term.gx
    gy = term.y if term.gy is None else term.gy
    return (float(term.x), float(term.y), layer), (float(gx), float(gy), ground)


def _part_ends(part, board: Board) -> tuple[tuple[float, float, str], tuple[float, float, str]] | None:
    if not part.layer_a or not part.layer_b:
        return None
    return (
        (float(part.ax), float(part.ay), part.layer_a),
        (float(part.bx), float(part.by), part.layer_b),
    )


def _box(board, power_net, reference_net, name, ends, existing) -> PortBox | None:
    box = port_between(board, ends[0], ends[1], reference_net, existing, r_ohm=PDN_PORT_OHM, signal_net=power_net)
    if box is None:
        return None
    return replace(box, name=name)


def _pdn_excitation(board, power_net, reference_net, vrm, loads, parts):
    """Load ports, plus lumped parts, the VRM short, and inductor ports."""
    warnings: list[str] = []
    ports: list[PortBox] = []
    extras: list[dict] = []
    stamps: list[dict] = []
    existing: list[PortBox] = []

    def place(name, ends):
        box = _box(board, power_net, reference_net, name, ends, existing)
        if box is None:
            warnings.append(f"{name} has no copper span between its clicks.")
            return None
        existing.append(box)
        return box

    for load in loads:
        box = place(load.name, _ends(load, board))
        if box is not None:
            ports.append(box)
    vrm_box = place("VRM", _ends(vrm, board))
    if vrm_box is not None:
        _place_vrm(vrm, vrm_box, ports, extras, stamps)
    for part in parts:
        ends = _part_ends(part, board)
        if ends is None:
            warnings.append(f"{part.name} needs a layer on each pad.")
            continue
        box = place(part.name, ends)
        if box is None:
            continue
        _place_part(part, box, ports, extras, stamps)
    return ports, extras, stamps, warnings


def _place_vrm(vrm, box: PortBox, ports, extras, stamps) -> None:
    resistance = abs(float(vrm.r_ohm))
    inductance = abs(float(vrm.l_nh)) * 1e-9
    if resistance < 1e-15 and inductance < 1e-15:
        extras.append({"kind": "short", "box": box})
        return
    if inductance > 0 and resistance < 1e-15:
        ports.append(box)
        stamps.append({"name": box.name, "r_ohm": 0.0, "l_h": inductance})
        return
    values = {}
    if resistance > 0:
        values["r_ohm"] = resistance
    if inductance > 0:
        values["l_h"] = inductance
    extras.append({"kind": "element", "box": box, "values": values})


def _place_part(part, box: PortBox, ports, extras, stamps) -> None:
    resistance = float(part.r_ohm or 0.0)
    inductance = float(part.l_nh or 0.0) * 1e-9
    capacitance = float(part.c_nf or 0.0) * 1e-9
    if inductance > 0 and capacitance <= 0:
        ports.append(box)
        stamps.append({"name": box.name, "r_ohm": resistance, "l_h": inductance})
        return
    values = {}
    if resistance > 0:
        values["r_ohm"] = resistance
    if inductance > 0:
        values["l_h"] = inductance
    if capacitance > 0:
        values["c_f"] = capacitance
    if not values:
        return
    extras.append({"kind": "element", "box": box, "values": values})
