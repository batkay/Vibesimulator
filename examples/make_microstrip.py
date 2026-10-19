"""Write a 2-layer 50 ohm microstrip coupon as a KiCad board.

The width is solved from the Hammerstad–Jensen analysis formula so the
design Z0 is 50 Ω for the εr and height below. Pads are the same width as
the trace, so the line does not step in width.
"""

from __future__ import annotations

import math
from pathlib import Path

ER = 4.5
H_MM = 0.2
Z_TARGET = 50.0
LENGTH_MM = 10.0
TAN_DELTA = 0.02


def microstrip_z0(width_mm: float, height_mm: float = H_MM, er: float = ER) -> tuple[float, float]:
    """Return (Z0 ohms, εeff) for a zero-thickness microstrip."""
    u = max(width_mm / height_mm, 1e-6)
    a = (
        1
        + (1 / 49) * math.log((u**4 + (u / 52) ** 2) / (u**4 + 0.432))
        + (1 / 18.7) * math.log(1 + (u / 18.1) ** 3)
    )
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    eeff = (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)
    f = 6 + (2 * math.pi - 6) * math.exp(-(30.666 / u) ** 0.7528)
    z0 = (376.730313461 / (2 * math.pi * math.sqrt(eeff))) * math.log(f / u + math.sqrt(1 + (2 / u) ** 2))
    return z0, eeff


def width_for_z0(z_target: float = Z_TARGET) -> tuple[float, float, float]:
    lo, hi = 0.02 * H_MM, 20 * H_MM
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        z0, _ = microstrip_z0(mid)
        if z0 > z_target:
            lo = mid
        else:
            hi = mid
    width = 0.5 * (lo + hi)
    z0, eeff = microstrip_z0(width)
    return width, z0, eeff


def board_text() -> str:
    width, z0, eeff = width_for_z0()
    pad_x = 0.8
    x0, x1 = 0.0, LENGTH_MM
    y0, y1 = -4.0, 4.0
    outline_x0, outline_x1 = -3.0, LENGTH_MM + 3.0
    return f"""(kicad_pcb
  (version 20240108)
  (generator "pcb_simulator")
  (general (thickness {H_MM}))
  (paper "A4")
  (title_block (title "50 ohm microstrip") (comment 1 "design Z0 {z0:.2f} ohm") (comment 2 "eeff {eeff:.3f}") (comment 3 "width {width:.4f} mm"))
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (9 "F.SilkS" user "F.Silkscreen")
    (11 "B.SilkS" user "B.Silkscreen")
    (25 "Edge.Cuts" user)
  )
  (setup
    (stackup
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core") (thickness {H_MM}) (material "FR4") (epsilon_r {ER}) (loss_tangent {TAN_DELTA}))
      (layer "B.Cu" (type "copper") (thickness 0.035))
    )
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (gr_rect (start {outline_x0} {y0}) (end {outline_x1} {y1}) (layer "Edge.Cuts") (width 0.05) (fill none))
  (segment (start {x0} 0) (end {x1} 0) (width {width:.6f}) (layer "F.Cu") (net 2))
  (footprint "sim:launch"
    (layer "F.Cu")
    (at {x0} 0)
    (property "Reference" "J1" (at 0 1 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size {pad_x} {width:.6f}) (layers "F.Cu") (net 2 "SIG"))
  )
  (footprint "sim:launch"
    (layer "F.Cu")
    (at {x1} 0)
    (property "Reference" "J2" (at 0 1 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size {pad_x} {width:.6f}) (layers "F.Cu") (net 2 "SIG"))
  )
  (zone
    (net 1)
    (net_name "GND")
    (layer "B.Cu")
    (filled_polygon
      (layer "B.Cu")
      (pts
        (xy {outline_x0} {y0}) (xy {outline_x1} {y0}) (xy {outline_x1} {y1}) (xy {outline_x0} {y1})
      )
    )
  )
)
"""


def write(path: str | Path | None = None) -> Path:
    path = Path(path) if path else Path(__file__).with_name("microstrip_50ohm.kicad_pcb")
    path.write_text(board_text(), encoding="utf-8")
    return path


if __name__ == "__main__":
    out = write()
    w, z0, eeff = width_for_z0()
    print(f"wrote {out}")
    print(f"width {w:.4f} mm  Z0 {z0:.2f} ohm  eeff {eeff:.3f}")
