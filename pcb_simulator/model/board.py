"""In-memory board. Units are millimeters, Y positive up, matching KiCad files."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StackLayer:
    name: str
    kind: str  # copper, dielectric, mask, other
    thickness_mm: float = 0.0
    material: str | None = None
    eps_r: float | None = None
    loss_tangent: float | None = None
    # Copper: z of the sheet. Dielectric: z of the bottom of the slab.
    z_mm: float | None = None


@dataclass
class Layer:
    id: int
    name: str
    kind: str  # signal or user
    user_name: str | None = None


@dataclass
class CopperPoly:
    points: list[tuple[float, float]]
    layer: str
    net: str
    kind: str  # trace, zone, pad, via
    holes: list[list[tuple[float, float]]] = field(default_factory=list)
    feature_mm: float | None = None
    source: str = ""


@dataclass
class Via:
    x: float
    y: float
    drill_mm: float
    size_mm: float
    layers: tuple[str, str]
    net: str
    source: str = ""


@dataclass
class Pad:
    footprint: str
    name: str
    net: str
    layers: list[str]
    center: tuple[float, float]
    points: list[tuple[float, float]]
    drill_mm: float | None = None
    source: str = ""


@dataclass
class Board:
    name: str = ""
    thickness_mm: float = 1.6
    layers: list[Layer] = field(default_factory=list)
    stackup: list[StackLayer] = field(default_factory=list)
    nets: dict[int, str] = field(default_factory=dict)
    copper: list[CopperPoly] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    pads: list[Pad] = field(default_factory=list)
    outline: list[tuple[float, float]] | None = None
    holes: list[list[tuple[float, float]]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    assumed_stackup: bool = False

    def copper_names_top_to_bottom(self) -> list[str]:
        names = [s.name for s in self.stackup if s.kind == "copper"]
        if names:
            return names
        return [layer.name for layer in self.layers if layer.kind == "signal" or layer.name.endswith(".Cu")]

    def layer_z(self, name: str) -> float | None:
        for layer in self.stackup:
            if layer.kind == "copper" and layer.name == name:
                return layer.z_mm
        return None

    def net_names(self) -> list[str]:
        names = {n for n in self.nets.values() if n}
        for item in (*self.copper, *self.vias, *self.pads):
            if item.net:
                names.add(item.net)
        return sorted(names)

    def dielectrics(self) -> list[StackLayer]:
        return [s for s in self.stackup if s.kind == "dielectric" and s.z_mm is not None and s.thickness_mm > 0]

    def top_eps(self) -> float:
        """εr of the dielectric just under the top copper, else 4.5."""
        coppers = [s for s in self.stackup if s.kind == "copper" and s.z_mm is not None]
        if not coppers:
            return 4.5
        top = max(coppers, key=lambda s: s.z_mm or 0.0)
        below = [
            d for d in self.dielectrics()
            if d.z_mm is not None and abs((d.z_mm + d.thickness_mm) - (top.z_mm or 0.0)) < 1e-6
        ]
        if below and below[0].eps_r:
            return float(below[0].eps_r)
        for d in self.dielectrics():
            if d.eps_r:
                return float(d.eps_r)
        return 4.5


def assign_stackup_z(stackup: list[StackLayer]) -> None:
    """Place copper sheets. KiCad lists the stack top to bottom; z = 0 is the bottom copper.

    Copper thickness is stored and is not added to z. The sheets miss sidewall
    capacitance, which is several ohms on a 0.1 mm prepreg. Left for later; see README.
    """
    for layer in stackup:
        layer.z_mm = None
    z = 0.0
    started = False
    for layer in reversed(stackup):
        if layer.kind == "copper":
            layer.z_mm = z
            started = True
        elif layer.kind == "dielectric" and started:
            layer.z_mm = z
            z += max(layer.thickness_mm, 0.0)


def default_reference_net(names: list[str]) -> str:
    for candidate in ("GND", "GNDA", "VSS", "AGND", "DGND"):
        if candidate in names:
            return candidate
    for name in names:
        if name.upper().startswith("GND"):
            return name
    return names[0] if names else ""
