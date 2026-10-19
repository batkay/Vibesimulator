"""Board-specific setup, stored next to the board as `.pcbsim.json`.

The KiCad file is never modified. The solver Python path lives in QSettings,
not here, because it is a machine setting.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .model.board import StackLayer
from .model.crop import CropSettings
from .model.mesh import MeshSettings, PortBox


@dataclass
class DiffPair:
    """Two single-ended ports at one end of a pair. Positive minus negative."""

    name: str
    positive: str
    negative: str


@dataclass
class Project:
    version: int = 1
    board: str = ""
    mode: str = "crop"
    rect: tuple[float, float, float, float] | None = None
    margin_mm: float = 2.0
    selected_nets: list[str] = field(default_factory=list)
    reference_net: str = "GND"
    ports: list[PortBox] = field(default_factory=list)
    pairs: list[DiffPair] = field(default_factory=list)
    f_max_hz: float = 5e9
    n_freq: int = 401
    end_criteria: float = 1e-4
    nr_ts: int = 1_000_000
    threads: int | None = None
    eps_eff: float | None = None
    cells_per_feature: int = 2
    stackup: list[dict] | None = None

    def crop_settings(self) -> CropSettings:
        return CropSettings(
            mode=self.mode,
            rect=self.rect,
            margin_mm=self.margin_mm,
            selected_nets=list(self.selected_nets),
            reference_net=self.reference_net,
        )

    def mesh_settings(self) -> MeshSettings:
        return MeshSettings(f_max_hz=self.f_max_hz, cells_per_feature=self.cells_per_feature)


def apply_stackup(board_layers: list[StackLayer], saved: list[dict] | None) -> None:
    if not saved:
        return
    by_name = {item["name"]: item for item in saved if "name" in item}
    for layer in board_layers:
        item = by_name.get(layer.name)
        if not item:
            continue
        if "thickness_mm" in item:
            layer.thickness_mm = float(item["thickness_mm"])
        if "eps_r" in item and item["eps_r"] is not None:
            layer.eps_r = float(item["eps_r"])
        if "loss_tangent" in item and item["loss_tangent"] is not None:
            layer.loss_tangent = float(item["loss_tangent"])


def stackup_payload(layers: list[StackLayer]) -> list[dict]:
    return [
        {
            "name": layer.name,
            "kind": layer.kind,
            "thickness_mm": layer.thickness_mm,
            "eps_r": layer.eps_r,
            "loss_tangent": layer.loss_tangent,
            "material": layer.material,
        }
        for layer in layers
    ]


def _port_dict(port: PortBox) -> dict:
    return asdict(port)


def _port_from(data: dict) -> PortBox:
    return PortBox(
        name=str(data["name"]),
        r_ohm=float(data["r_ohm"]),
        exc_dir=str(data["exc_dir"]),
        start=tuple(data["start"]),
        stop=tuple(data["stop"]),
        net=str(data.get("net", "")),
        reference_net=str(data.get("reference_net", "")),
        footprint=str(data.get("footprint", "")),
        pad=str(data.get("pad", "")),
        marks=_marks(data.get("marks")),
    )


def _marks(raw) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if not raw or len(raw) != 2:
        return None
    return (tuple(raw[0]), tuple(raw[1]))


def save_project(path: str | Path, project: Project) -> None:
    path = Path(path)
    payload = {
        "version": project.version,
        "board": project.board,
        "mode": project.mode,
        "rect": list(project.rect) if project.rect else None,
        "margin_mm": project.margin_mm,
        "selected_nets": project.selected_nets,
        "reference_net": project.reference_net,
        "ports": [_port_dict(port) for port in project.ports],
        "pairs": [asdict(pair) for pair in project.pairs],
        "f_max_hz": project.f_max_hz,
        "n_freq": project.n_freq,
        "end_criteria": project.end_criteria,
        "nr_ts": project.nr_ts,
        "threads": project.threads,
        "eps_eff": project.eps_eff,
        "cells_per_feature": project.cells_per_feature,
        "stackup": project.stackup,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_project(path: str | Path) -> Project:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rect = data.get("rect")
    return Project(
        version=int(data.get("version", 1)),
        board=str(data.get("board", "")),
        mode=str(data.get("mode", "crop")),
        rect=tuple(rect) if rect else None,
        margin_mm=float(data.get("margin_mm", 2.0)),
        selected_nets=list(data.get("selected_nets") or []),
        reference_net=str(data.get("reference_net", "GND")),
        ports=[_port_from(item) for item in data.get("ports") or []],
        pairs=[
            DiffPair(name=str(item["name"]), positive=str(item["positive"]), negative=str(item["negative"]))
            for item in data.get("pairs") or []
            if item.get("name") and item.get("positive") and item.get("negative")
        ],
        f_max_hz=float(data.get("f_max_hz", 5e9)),
        n_freq=int(data.get("n_freq", 401)),
        end_criteria=float(data.get("end_criteria", 1e-4)),
        nr_ts=int(data.get("nr_ts", 1_000_000)),
        threads=data.get("threads"),
        eps_eff=data.get("eps_eff"),
        cells_per_feature=int(data.get("cells_per_feature", 2)),
        stackup=data.get("stackup"),
    )


def board_path_for(project_path: Path, project: Project) -> Path:
    board = Path(project.board)
    if board.is_absolute():
        return board
    return (project_path.parent / board).resolve()
