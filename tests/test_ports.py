"""Two-click ports stay on the copper named in the port list."""

import pytest

from pcb_simulator.model.board import Board, CopperPoly, StackLayer, assign_stackup_z
from pcb_simulator.model.ports import port_between


def _usb_corner() -> Board:
    """A D_N pad, the D_N trace leaving it, and a VBUS trace just past the pad."""
    board = Board(thickness_mm=0.2)
    board.stackup = [
        StackLayer("F.Cu", "copper", 0.035),
        StackLayer("dielectric 1", "dielectric", 0.2, eps_r=4.5, loss_tangent=0.02),
        StackLayer("B.Cu", "copper", 0.035),
    ]
    assign_stackup_z(board.stackup)
    board.copper = [
        CopperPoly(
            [(73.4, 142.5), (73.7, 142.5), (73.7, 143.6), (73.4, 143.6)],
            "F.Cu",
            "D_N",
            "pad",
        ),
        # Same net, overlapping the pad and running up to the VBUS trace. The pad is smaller, so it wins.
        CopperPoly(
            [(73.45, 140.0), (73.65, 140.0), (73.65, 145.0), (73.45, 145.0)],
            "F.Cu",
            "D_N",
            "trace",
        ),
        CopperPoly(
            [(72.8, 144.5), (75.8, 144.5), (75.8, 144.8), (72.8, 144.8)],
            "F.Cu",
            "VBUS",
            "trace",
        ),
        CopperPoly([(70.0, 140.0), (78.0, 140.0), (78.0, 146.0), (70.0, 146.0)], "B.Cu", "GND", "zone"),
    ]
    return board


def test_same_layer_ends_stay_on_the_clicks():
    board = _usb_corner()
    port = port_between(
        board,
        (73.55, 143.0, "F.Cu"),
        (72.95, 143.0, "F.Cu"),
        "GND",
        [],
        signal_net="D_N",
    )
    assert port is not None
    assert port.net == "D_N"
    assert port.exc_dir == "x"
    assert port.marks == ((73.55, 143.0), (72.95, 143.0))
    assert port.start[0] == pytest.approx(72.95)
    assert port.stop[0] == pytest.approx(73.55)
    assert port.start[1] == pytest.approx(142.9)
    assert port.stop[1] == pytest.approx(143.1)


def test_a_second_click_does_not_snap_to_the_pad_edge():
    board = _usb_corner()
    port = port_between(
        board,
        (73.55, 143.0, "F.Cu"),
        (73.0, 144.7, "F.Cu"),
        "GND",
        [],
        signal_net="D_N",
    )
    assert port is not None
    assert port.exc_dir == "y"
    assert port.marks[0] == pytest.approx((73.55, 143.0))
    assert port.marks[1] == pytest.approx((73.0, 144.7))
    assert min(port.start[1], port.stop[1]) == pytest.approx(143.0)
    assert max(port.start[1], port.stop[1]) == pytest.approx(144.7)
    assert min(port.start[0], port.stop[0]) == pytest.approx(73.0)
    assert max(port.start[0], port.stop[0]) == pytest.approx(73.55)


def test_different_layer_port_stays_on_the_first_click():
    board = _usb_corner()
    port = port_between(
        board,
        (73.55, 143.0, "F.Cu"),
        (73.0, 144.7, "B.Cu"),
        "GND",
        [],
        signal_net="D_N",
    )
    assert port is not None
    assert port.exc_dir == "z"
    assert port.net == "D_N"
    assert port.marks == ((73.55, 143.0), (73.0, 144.7))
    assert port.start[0] == pytest.approx(73.45)
    assert port.stop[0] == pytest.approx(73.65)
    assert port.start[1] == pytest.approx(142.9)
    assert port.stop[1] == pytest.approx(143.1)
    assert sorted((port.start[2], port.stop[2])) == pytest.approx([0.0, 0.2])
