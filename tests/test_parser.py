import math

import pytest
from shapely.geometry import Point

from pcb_simulator.model.geom import as_polygon
from pcb_simulator.model.kicad import parse_kicad_pcb

FIXTURE = r"""
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (25 "Edge.Cuts" user)
  )
  (setup
    (stackup
      (layer "F.SilkS" (type "Top Silk Screen"))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core") (thickness 0.2) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.035))
      (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
    )
  )
  (net 1 "GND")
  (future_thing (nested 1 "still fine"))
  (gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 10 0) (end 10 5) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 10 5) (end 0 5) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 0 5) (end 0 0) (layer "Edge.Cuts") (width 0.05))
  (segment (start 1 1) (end 4 1) (width 0.25) (layer "F.Cu") (net "ONLY"))
  (via (at 1 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (zone (net 1) (net_name "GND") (layer "F.Cu") (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))
  (zone (net 1) (net_name "GND") (layer "B.Cu")
    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 10 0) (xy 10 5) (xy 0 5))))
  (footprint "pkg:pad"
    (layer "F.Cu")
    (at 10 20 90)
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 2 0 90) (size 1 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "flat" smd rect (at 0 0) (size 2 0.4) (layers "F.Cu") (net 1 "GND"))
  )
  (footprint "pkg:back"
    (layer "B.Cu")
    (at 0 0 0)
    (property "Reference" "U2" (at 0 0 0) (layer "B.SilkS"))
    (pad "1" smd rect (at 1 0) (size 0.4 0.2) (layers "B.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 1 0 45) (size 2 0.4) (layers "B.Cu") (net 1 "GND"))
  )
  (arc (start 1 0) (mid 0.7071 0.7071) (end 0 1) (width 0.2) (layer "F.Cu") (net "ONLY"))
)
"""


def test_parser_fixture():
    board = parse_kicad_pcb(FIXTURE, name="fixture.kicad_pcb")
    assert board.layer_z("B.Cu") == pytest.approx(0.0)
    assert board.layer_z("F.Cu") == pytest.approx(0.2)
    assert any("no fill" in warning for warning in board.warnings)
    assert "ONLY" in board.net_names()
    pad = next(pad for pad in board.pads if pad.footprint == "U1")
    # Footprint at +90°. KiCad's positive angle is CCW on the Y-down canvas, so local +X goes to -Y.
    assert pad.center[0] == pytest.approx(10.0)
    assert pad.center[1] == pytest.approx(18.0)
    xs = [p[0] for p in pad.points]
    ys = [p[1] for p in pad.points]
    assert min(xs) == pytest.approx(9.75)
    assert max(xs) == pytest.approx(10.25)
    assert min(ys) == pytest.approx(17.5)
    assert max(ys) == pytest.approx(18.5)
    flat = next(pad for pad in board.pads if pad.name == "flat")
    # No angle in the file means the pad stays on the board axes, even though the footprint is at 90°.
    assert flat.center == pytest.approx((10.0, 20.0))
    assert max(p[0] for p in flat.points) - min(p[0] for p in flat.points) == pytest.approx(2.0)
    assert max(p[1] for p in flat.points) - min(p[1] for p in flat.points) == pytest.approx(0.4)
    back = next(pad for pad in board.pads if pad.footprint == "U2" and pad.name == "1")
    assert back.center == pytest.approx((-1.0, 0.0))
    turned = next(pad for pad in board.pads if pad.footprint == "U2" and pad.name == "2")
    assert turned.center == pytest.approx((-1.0, 0.0))
    turned_poly = as_polygon(turned.points)
    assert turned_poly is not None
    # 45° is KiCad's canvas angle: the long axis goes toward math -45° (down and right on screen).
    assert turned_poly.covers(Point(-1.0 + 0.8 * math.cos(math.radians(-45)), 0.8 * math.sin(math.radians(-45))))
    assert not turned_poly.covers(Point(-1.0 + 0.8 * math.cos(math.radians(45)), 0.8 * math.sin(math.radians(45))))
    assert board.outline is not None
    assert abs(_area(board.outline)) == pytest.approx(50.0)
    via = board.vias[0]
    assert (via.x, via.y, via.drill_mm) == pytest.approx((1.0, 2.0, 0.3))
    assert any(poly.kind == "trace" and poly.net == "ONLY" for poly in board.copper)
    assert any(poly.kind == "zone" and poly.layer == "B.Cu" for poly in board.copper)
    assert not any(poly.kind == "zone" and poly.layer == "F.Cu" for poly in board.copper)


def _area(points):
    acc = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        acc += x1 * y2 - x2 * y1
    return acc / 2


def test_rotated_footprint_uses_the_board_pad_angle():
    # Trace-antenna style: the footprint is at -90°, and each pad's (at) angle is 270°,
    # which is that same orientation written in the board frame. Adding the footprint
    # angle again turns the long axis the wrong way, so the segments no longer meet.
    board = parse_kicad_pcb(
        r"""
        (kicad_pcb (version 20240108)
          (layers (0 "F.Cu" signal))
          (footprint "Personal:Trace Antenna" (layer "F.Cu") (at 106.1 98.8 -90)
            (property "Reference" "AE1" (at 0 0 0) (layer "F.SilkS"))
            (pad "1" smd rect (at 0.6 -2.7 270) (size 5 0.5) (layers "F.Cu") (net "ANT"))
            (pad "1" smd rect (at 0.65 0 270) (size 0.5 4.9) (layers "F.Cu") (net "ANT"))
          )
          (footprint "Diode_SMD:D_SMC" (layer "F.Cu") (at 99.04 139.4 -90)
            (property "Reference" "D1" (at 0 0 0) (layer "F.SilkS"))
            (pad "1" smd rect (at -3.4 0) (size 3.3 2.5) (layers "F.Cu") (net "D"))
          )
          (footprint "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical" (layer "F.Cu") (at 68.87 104.55 90)
            (property "Reference" "J7" (at 0 0 0) (layer "F.SilkS"))
            (pad "2" thru_hole circle (at 0 2.54 90) (size 1.7 1.7) (drill 1) (layers "F.Cu") (net "P"))
          )
        )
        """
    )
    pads = [pad for pad in board.pads if pad.footprint == "AE1"]
    long = next(pad for pad in pads if _span(pad.points)[1] > _span(pad.points)[0])
    wide = next(pad for pad in pads if pad is not long)
    assert _span(long.points) == pytest.approx((0.5, 5.0))
    assert _span(wide.points) == pytest.approx((4.9, 0.5))
    assert min(p[0] for p in long.points) == pytest.approx(max(p[0] for p in wide.points))
    long_center = (
        sum(p[0] for p in long.points) / len(long.points),
        sum(p[1] for p in long.points) / len(long.points),
    )
    # Local (0.6, -2.7) at footprint angle -90° lands on the +X side of the anchor.
    assert long_center == pytest.approx((108.8, 99.4))
    diode = next(pad for pad in board.pads if pad.footprint == "D1")
    assert diode.center == pytest.approx((99.04, 136.0))
    assert _span(diode.points) == pytest.approx((3.3, 2.5))
    # A pin at local +Y on a +90° header goes to +X, onto the board, not off the left edge.
    header = next(pad for pad in board.pads if pad.footprint == "J7")
    assert header.center == pytest.approx((71.41, 104.55))


def test_custom_pad_primitives_follow_the_board_angle():
    board = parse_kicad_pcb(
        r"""
        (kicad_pcb (version 20240108)
          (layers (0 "F.Cu" signal))
          (footprint "ant:poly" (layer "F.Cu") (at 0 0)
            (property "Reference" "AE" (at 0 0 0) (layer "F.SilkS"))
            (pad "1" smd custom (at 10 0 90) (size 0.2 0.2) (layers "F.Cu") (net "ANT")
              (options (clearance outline) (anchor circle))
              (primitives
                (gr_poly (pts (xy 0 0) (xy 2 0) (xy 0 1)) (width 0) (fill yes))
                (gr_circle (center 0 -1) (end 0.4 -1) (width 0.2) (fill no))
              )
            )
          )
        )
        """
    )
    assert not any("Skipped" in warning for warning in board.warnings)
    copper = [poly for poly in board.copper if poly.kind == "pad"]
    assert len(copper) == 2
    covered = as_polygon(copper[0].points, copper[0].holes)
    other = as_polygon(copper[1].points, copper[1].holes)
    assert covered is not None and other is not None
    # (0.3, 0.2) through KiCad's +90° (y, -x), then moved to (10, 0).
    assert covered.union(other).covers(Point(10.2, -0.3))
    # Stroked circle, center (0, -1) rotated +90°, stays a ring: the middle is open.
    assert other.covers(Point(9.0, 0.4)) or covered.covers(Point(9.0, 0.4))
    assert not covered.union(other).covers(Point(9.0, 0.0))


def _span(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (max(xs) - min(xs), max(ys) - min(ys))


def test_arc_quarter_circle_is_round():
    from pcb_simulator.model.geom import arc_points_three

    pts = arc_points_three((1, 0), (0.7071, 0.7071), (0, 1), segments=8)
    for x, y in pts:
        assert abs((x**2 + y**2) - 1) < 0.02
