"""Shared pieces for ODB++ and IPC-2581 import.

Both formats are Y-up. The board view draws model +Y downward so a KiCad file
matches KiCad's canvas, so these importers negate Y after the geometry is built.
The KiCad loader does not use this module.
"""

from __future__ import annotations

from .board import Board, StackLayer, assign_stackup_z

ASSUMED_WARNING = (
    "No copper stackup in the file. Assuming 1.6 mm FR-4 (εr 4.5, tanδ 0.02) "
    "and 35 µm copper. Edit the stackup before trusting a result."
)


def flip_y(board: Board) -> Board:
    """Negate Y on an exchange-format board. Call once, after geometry is built."""

    def fy(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(x, -y) for x, y in points]

    for poly in board.copper:
        poly.points = fy(poly.points)
        poly.holes = [fy(ring) for ring in poly.holes]
    for pad in board.pads:
        pad.points = fy(pad.points)
        pad.center = (pad.center[0], -pad.center[1])
    for via in board.vias:
        via.y = -via.y
    if board.outline:
        board.outline = fy(board.outline)
    board.holes = [fy(ring) for ring in board.holes]
    return board


def assumed_stackup(copper_names: list[str], thickness: float = 1.6) -> list[StackLayer]:
    names = copper_names or ["top", "bottom"]
    assumed: list[StackLayer] = []
    n_gaps = max(len(names) - 1, 1)
    diel = thickness / n_gaps
    for index, name in enumerate(names):
        assumed.append(StackLayer(name=name, kind="copper", thickness_mm=0.035))
        if index != len(names) - 1:
            assumed.append(
                StackLayer(
                    name=f"dielectric {index + 1}",
                    kind="dielectric",
                    thickness_mm=diel,
                    material="FR4",
                    eps_r=4.5,
                    loss_tangent=0.02,
                )
            )
    return assumed


def finish_stackup(board: Board, stackup: list[StackLayer], *, real: bool) -> None:
    """Install a stackup. `real` is False when every dielectric thickness was missing."""
    if real and any(layer.kind == "dielectric" and layer.thickness_mm > 0 for layer in stackup):
        board.stackup = stackup
        board.assumed_stackup = False
        board.thickness_mm = sum(
            layer.thickness_mm for layer in stackup if layer.kind == "dielectric" and layer.thickness_mm > 0
        )
    else:
        names = [layer.name for layer in stackup if layer.kind == "copper"]
        board.stackup = assumed_stackup(names)
        board.assumed_stackup = True
        board.thickness_mm = 1.6
        board.warnings.append(ASSUMED_WARNING)
    assign_stackup_z(board.stackup)


def remember_net(board: Board, name: str) -> None:
    if name and name not in board.nets.values():
        board.nets[len(board.nets)] = name
