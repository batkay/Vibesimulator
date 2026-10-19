"""Plain-language lines for the mesh build and the silent operator setup."""

from __future__ import annotations


def describe_prep(payload: dict) -> str:
    """One log line for a PCB_PREP event. Empty when the event is not for the user."""
    event = payload.get("event")
    if event == "crop":
        if "copper" in payload:
            return (
                f"Crop kept {int(payload['copper']):,} copper shapes"
                f" and {int(payload.get('vias', 0)):,} vias."
            )
        if "done" in payload:
            return f"Cropping copper, {int(payload['done']):,} of {int(payload['total']):,} shapes."
        return "Cropping copper."
    if event == "mesh":
        return "Building the mesh lines."
    if event == "cells":
        nx, ny, nz = list(payload.get("cells") or [0, 0, 0])
        count = int(payload.get("count") or 0)
        mb = count * 100 / 1e6
        fine = payload.get("min_mm")
        fine_text = f" Finest cell {float(fine):.3f} mm." if fine else ""
        return (
            f"Mesh is {int(nx):,} × {int(ny):,} × {int(nz):,} = {count:,} cells."
            f" Rough memory {mb:.0f} MB.{fine_text} Tessellating the copper next."
        )
    if event == "triangles":
        return (
            f"Tessellating copper, {int(payload.get('done', 0)):,}"
            f" of {int(payload.get('total', 0)):,} shapes."
        )
    if event == "write":
        return "Writing the job file."
    if event == "ready":
        return "Mesh is ready."
    if event == "error":
        return str(payload.get("message") or "Mesh build failed.")
    return ""


def describe_sim(payload: dict) -> str:
    """One log line for a geometry or operator event. Empty for the other PCB_SIM events."""
    event = payload.get("event")
    if event == "geometry":
        if "done" in payload:
            return f"Adding geometry, {int(payload['done']):,} of {int(payload['total']):,} polygons."
        return (
            f"Adding geometry: {int(payload.get('metals', 0)):,} metal polygons, "
            f"{int(payload.get('vias', 0)):,} vias, "
            f"{int(payload.get('dielectric', 0)):,} dielectric polygons."
        )
    if event == "operator":
        cells = int(payload.get("cells") or 0)
        fine = payload.get("min_mm")
        detail = f"{cells:,} cells" if cells else "the mesh"
        if fine:
            detail += f", smallest cell {float(fine):.3f} mm"
        who = ""
        if int(payload.get("count") or 1) > 1:
            who = f" for {payload.get('port')} ({int(payload.get('index', 0)) + 1}/{payload.get('count')})"
        elapsed = int(payload.get("elapsed_s") or 0)
        if elapsed <= 0:
            return (
                f"Building the FDTD operator{who} ({detail}). "
                "Elapsed time follows every 10 seconds until the timestep log starts."
            )
        return f"Still building the FDTD operator{who}, {elapsed} s ({detail})."
    return ""


def prep_failure_message(text: str, exit_code: int) -> str:
    """The mesh-build error to show. Progress lines are not the error."""
    import json

    for line in reversed(text.splitlines()):
        if not line.startswith("PCB_PREP "):
            continue
        body = line[9:]
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return body
        if payload.get("event") == "error":
            return str(payload.get("message") or "Mesh build failed.")
    stripped = text.strip()
    return stripped or f"Mesh build failed (exit {exit_code})."
