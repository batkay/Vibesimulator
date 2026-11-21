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
class PdnTerminalRecord:
    """A VRM or a load. Power is (x, y). Ground is (gx, gy). The S-parameter run ignores it."""

    name: str
    x: float
    y: float
    net: str
    layer: str = ""
    footprint: str = ""
    pad: str = ""
    volts: float = 1.0
    amps: float = 0.0
    r_ohm: float = 0.0
    l_nh: float = 0.0
    gx: float | None = None
    gy: float | None = None
    g_layer: str = ""
    g_footprint: str = ""
    g_pad: str = ""


@dataclass
class PdnPartRecord:
    """An RLC between two pads. A missing element is absent. It does not change copper."""

    name: str
    ax: float
    ay: float
    net_a: str
    bx: float
    by: float
    net_b: str
    layer_a: str = ""
    layer_b: str = ""
    footprint: str = ""
    r_ohm: float | None = None
    l_nh: float | None = None
    c_nf: float | None = None


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
    # Missing keys in an older project file load as these. The S-parameter run
    # uses them. The PDN field run keeps PML, the energy limit, and the typed cap.
    boundary: str = "mur"
    stop: str = "crossing"
    signal_only: bool = True
    stackup: list[dict] | None = None
    vrm: PdnTerminalRecord | None = None
    loads: list[PdnTerminalRecord] = field(default_factory=list)
    parts: list[PdnPartRecord] = field(default_factory=list)
    conductivity: float = 5.8e7
    plating_mm: float = 0.025
    pitch_mm: float = 0.5
    power_net: str = ""
    ac_power_layer: str = ""
    ac_ground_layer: str = ""
    ripple_mv: float | None = None
    step_a: float | None = None
    pdn_f_max_hz: float = 1e9
    pdn_n_freq: int = 201
    pdn_signal_mesh: bool = False

    def crop_settings(self) -> CropSettings:
        extra: list[str] = []
        seen = {self.reference_net}
        for port in self.ports:
            name = port.reference_net
            if name and name not in seen:
                seen.add(name)
                extra.append(name)
        selected = list(self.selected_nets)
        # An empty checklist plus Port nets follows the nets the ports sit on.
        # Checked nets stay as the user left them. References are added by CropSettings.
        if self.signal_only and not selected:
            omitted = {self.reference_net, *extra}
            picked: set[str] = set()
            for port in self.ports:
                name = port.net
                if name and name not in omitted and name not in picked:
                    picked.add(name)
                    selected.append(name)
        return CropSettings(
            mode=self.mode,
            rect=self.rect,
            margin_mm=self.margin_mm,
            selected_nets=selected,
            reference_net=self.reference_net,
            reference_nets=extra,
            signal_only=self.signal_only,
        )

    def mesh_settings(self) -> MeshSettings:
        boundary = self.boundary if self.boundary in ("mur", "pml") else "pml"
        return MeshSettings(
            f_max_hz=self.f_max_hz,
            cells_per_feature=self.cells_per_feature,
            boundary=boundary,
        )


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


def project_document(project: Project) -> dict:
    """The object `save_project` writes. The window compares this to decide Saved."""
    return {
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
        "boundary": project.boundary,
        "stop": project.stop,
        "signal_only": project.signal_only,
        "stackup": project.stackup,
        "vrm": None if project.vrm is None else asdict(project.vrm),
        "loads": [asdict(item) for item in project.loads],
        "parts": [asdict(item) for item in project.parts],
        "conductivity": project.conductivity,
        "plating_mm": project.plating_mm,
        "pitch_mm": project.pitch_mm,
        "power_net": project.power_net,
        "ac_power_layer": project.ac_power_layer,
        "ac_ground_layer": project.ac_ground_layer,
        "ripple_mv": project.ripple_mv,
        "step_a": project.step_a,
        "pdn_f_max_hz": project.pdn_f_max_hz,
        "pdn_n_freq": project.pdn_n_freq,
        "pdn_signal_mesh": project.pdn_signal_mesh,
    }


def project_fingerprint(project: Project) -> str:
    """One string for the saved document. Key order does not matter."""
    return json.dumps(project_document(project), sort_keys=True, separators=(",", ":"))


def save_project(path: str | Path, project: Project) -> None:
    path = Path(path)
    path.write_text(json.dumps(project_document(project), indent=2), encoding="utf-8")


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
        boundary=_choice(data.get("boundary", "mur"), ("mur", "pml"), "mur"),
        stop=_choice(data.get("stop", "crossing"), ("crossing", "energy"), "crossing"),
        signal_only=bool(data.get("signal_only", True)),
        stackup=data.get("stackup"),
        vrm=_terminal_from(data.get("vrm")),
        loads=[item for item in (_terminal_from(raw) for raw in data.get("loads") or []) if item is not None],
        parts=[item for item in (_part_from(raw) for raw in data.get("parts") or []) if item is not None],
        conductivity=float(data.get("conductivity", 5.8e7)),
        plating_mm=float(data.get("plating_mm", 0.025)),
        pitch_mm=float(data.get("pitch_mm", 0.5)),
        power_net=str(data.get("power_net", "")),
        ac_power_layer=str(data.get("ac_power_layer", "")),
        ac_ground_layer=str(data.get("ac_ground_layer", "")),
        ripple_mv=_optional_number(data.get("ripple_mv")),
        step_a=_optional_number(data.get("step_a")),
        pdn_f_max_hz=float(data.get("pdn_f_max_hz", 1e9)),
        pdn_n_freq=int(data.get("pdn_n_freq", 201)),
        pdn_signal_mesh=bool(data.get("pdn_signal_mesh", False)),
    )


def _choice(value, allowed: tuple[str, ...], fallback: str) -> str:
    text = str(value)
    if text in allowed:
        return text
    return fallback


def suggest_power_net(names: list[str]) -> str:
    """One obvious rail, or blank when several names match. A blank asks the user."""
    hits = []
    for name in names:
        upper = name.upper()
        if "GND" in upper:
            continue
        if any(hint in upper for hint in ("VCC", "VDD", "VBUS", "PP", "+")):
            hits.append(name)
    if len(hits) == 1:
        return hits[0]
    return ""


def _terminal_from(raw) -> PdnTerminalRecord | None:
    if not isinstance(raw, dict) or not raw.get("name"):
        return None
    return PdnTerminalRecord(
        name=str(raw["name"]),
        x=float(raw.get("x", 0.0)),
        y=float(raw.get("y", 0.0)),
        net=str(raw.get("net", "")),
        layer=str(raw.get("layer", "")),
        footprint=str(raw.get("footprint", "")),
        pad=str(raw.get("pad", "")),
        volts=float(raw.get("volts", 1.0)),
        amps=float(raw.get("amps", 0.0)),
        r_ohm=float(raw.get("r_ohm", 0.0)),
        l_nh=float(raw.get("l_nh", 0.0)),
        gx=_optional_coord(raw.get("gx")),
        gy=_optional_coord(raw.get("gy")),
        g_layer=str(raw.get("g_layer", "")),
        g_footprint=str(raw.get("g_footprint", "")),
        g_pad=str(raw.get("g_pad", "")),
    )


def _part_from(raw) -> PdnPartRecord | None:
    if not isinstance(raw, dict) or not raw.get("name"):
        return None
    return PdnPartRecord(
        name=str(raw["name"]),
        ax=float(raw.get("ax", 0.0)),
        ay=float(raw.get("ay", 0.0)),
        net_a=str(raw.get("net_a", "")),
        bx=float(raw.get("bx", 0.0)),
        by=float(raw.get("by", 0.0)),
        net_b=str(raw.get("net_b", "")),
        layer_a=str(raw.get("layer_a", "")),
        layer_b=str(raw.get("layer_b", "")),
        footprint=str(raw.get("footprint", "")),
        r_ohm=_optional_number(raw.get("r_ohm")),
        l_nh=_optional_number(raw.get("l_nh")),
        c_nf=_optional_number(raw.get("c_nf")),
    )


def _optional_coord(value) -> float | None:
    """A missing ground coordinate means the return is at the power point."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_number(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number == 0:
        return None
    return number


def board_path_for(project_path: Path, project: Project) -> Path:
    board = Path(project.board)
    if board.is_absolute():
        return board
    return (project_path.parent / board).resolve()
