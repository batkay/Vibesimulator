"""Pour triangles stay inside the copper that produced them."""

import pytest
from shapely.geometry import Polygon

from pcb_simulator.model.geom import solid_triangles


def test_a_clearance_is_not_filled_with_pour_triangles():
    outer = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    slot = [(2.0, 4.0), (8.0, 4.0), (8.0, 6.0), (2.0, 6.0)]
    pour = Polygon(outer, [slot])
    trace = Polygon([(2.2, 4.2), (7.8, 4.2), (7.8, 5.8), (2.2, 5.8)])
    pieces = solid_triangles(outer, [slot])
    assert pieces
    covered = 0.0
    for ring in pieces:
        piece = Polygon(ring)
        assert piece.intersection(trace).area < 1e-8
        assert piece.difference(pour).area < 1e-6
        covered += piece.area
    assert covered == pytest.approx(pour.area, rel=1e-6, abs=1e-6)


def test_a_polygon_without_holes_is_left_whole():
    ring = [(0.0, 0.0), (3.0, 0.0), (1.0, 2.0)]
    assert solid_triangles(ring, []) == [ring]
