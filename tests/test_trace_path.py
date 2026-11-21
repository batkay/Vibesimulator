"""Centerline length ignores pours and follows a bend."""

import pytest

from pcb_simulator.model.board import Board, CopperPoly
from pcb_simulator.model.trace_path import route_length, trace_point, trace_route


def _trace(points, net="SIG"):
    return CopperPoly(points, "F.Cu", net, "trace", feature_mm=0.4)


def _board():
    board = Board(thickness_mm=1.6)
    board.outline = [(0, -5), (30, -5), (30, 20), (0, 20)]
    board.copper = [
        _trace([(0, -0.2), (10, -0.2), (10, 0.2), (0, 0.2)]),
        _trace([(9.8, 0), (10.2, 0), (10.2, 6), (9.8, 6)]),
        CopperPoly([(0, -5), (30, -5), (30, 20), (0, 20)], "B.Cu", "SIG", "zone"),
    ]
    return board


def test_distance_follows_the_centerline_and_skips_the_pour():
    board = _board()
    point, clamped = trace_point(board, "SIG", (0.0, 0.0), 4.0)
    assert clamped is False
    assert point[0] == pytest.approx(4.0, abs=0.15)
    assert abs(point[1]) < 0.05
    corner, clamped = trace_point(board, "SIG", (0.0, 0.0), 13.0)
    assert clamped is False
    assert corner[0] == pytest.approx(10.0, abs=0.3)
    assert corner[1] == pytest.approx(3.0, abs=0.3)
    end, clamped = trace_point(board, "SIG", (0.0, 0.0), 100.0)
    assert clamped is True
    assert end[1] == pytest.approx(6.0, abs=0.3)


def _segment(a, b, kind="trace", net="D_P"):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = (dx * dx + dy * dy) ** 0.5
    px, py = -dy / length * 0.15, dx / length * 0.15
    points = [(ax + px, ay + py), (bx + px, by + py), (bx - px, by - py), (ax - px, ay - py)]
    return CopperPoly(points, "F.Cu", net, kind, feature_mm=0.3)


def test_two_pads_on_one_net_still_follow_the_long_trace():
    """A connector ties two pads together. The marker leaves that loop for the trace."""
    board = Board(thickness_mm=1.6)
    board.outline = [(-2, -2), (8, -2), (8, 32), (-2, 32)]
    board.copper = [
        _segment((0, 0), (0, 30)),
        _segment((0, 0), (3, 0)),
        _segment((3, 0), (3, 2), kind="pad"),
        _segment((3, 2), (5, 2)),
        _segment((5, 2), (5, 0), kind="pad"),
        _segment((5, 0), (0, 0)),
    ]
    near, clamped = trace_point(board, "D_P", (3.0, 2.0), 1.0)
    assert clamped is False
    assert near[0] == pytest.approx(3.0, abs=0.2)
    assert near[1] == pytest.approx(1.0, abs=0.2)
    point, clamped = trace_point(board, "D_P", (3.0, 2.0), 15.0)
    assert clamped is False
    assert point[0] == pytest.approx(0.0, abs=0.3)
    assert point[1] == pytest.approx(10.0, abs=0.4)


def test_route_length_reaches_the_far_end():
    board = _board()
    length = route_length(board, "SIG", (0.0, 0.0))
    assert length == pytest.approx(16.0, abs=0.4)


def test_route_length_is_the_copper_not_the_shortcut():
    board = _board()
    found = trace_route(board, "SIG", (0.0, 0.0), (10.0, 6.0))
    assert found is not None
    length, path = found
    assert length == pytest.approx(16.0, abs=0.4)
    assert path[0][0] == pytest.approx(0.0, abs=0.2)
    assert path[-1][1] == pytest.approx(6.0, abs=0.3)
    straight = ((path[-1][0] - path[0][0]) ** 2 + (path[-1][1] - path[0][1]) ** 2) ** 0.5
    assert length > straight
