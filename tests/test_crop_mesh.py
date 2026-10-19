import pytest

from pcb_simulator.model.board import Board, CopperPoly, Pad, StackLayer, Via, assign_stackup_z
from pcb_simulator.model.crop import CropSettings, crop_board
from pcb_simulator.model.mesh import MeshSettings, PortBox, build_mesh


def _board() -> Board:
    board = Board(thickness_mm=0.2)
    board.stackup = [
        StackLayer("F.Cu", "copper", 0.035),
        StackLayer("dielectric 1", "dielectric", 0.2, eps_r=4.5, loss_tangent=0.02),
        StackLayer("B.Cu", "copper", 0.035),
    ]
    assign_stackup_z(board.stackup)
    board.outline = [(0, 0), (10, 0), (10, 6), (0, 6)]
    board.copper = [
        CopperPoly([(2, 2), (2.4, 2), (2.4, 4), (2, 4)], "F.Cu", "SIG", "trace", feature_mm=0.4),
        CopperPoly([(0, 0), (10, 0), (10, 6), (0, 6)], "B.Cu", "GND", "zone"),
        # 8.037 is a vertex, not a bbox corner. It must not become its own grid line.
        CopperPoly([(7, 5), (9, 5), (8.037, 5.4), (7, 5.5)], "B.Cu", "GND", "zone"),
        CopperPoly([(2.05, 3.0), (2.1, 3.0), (2.1, 3.05), (2.05, 3.05)], "B.Cu", "GND", "zone"),
    ]
    board.pads = [
        Pad("J1", "1", "SIG", ["F.Cu"], (2.2, 3.0), [(2.0, 2.8), (2.4, 2.8), (2.4, 3.2), (2.0, 3.2)]),
    ]
    board.vias = [Via(1, 1, 0.3, 0.6, ("F.Cu", "B.Cu"), "SIG")]
    return board


def test_crop_keeps_reference_and_drops_the_rest():
    board = _board()
    board.copper.append(CopperPoly([(1, 1), (1.2, 1), (1.2, 1.2), (1, 1.2)], "F.Cu", "OTHER", "trace", feature_mm=0.2))
    cropped = crop_board(board, CropSettings(mode="crop", rect=(1.5, 1.5, 3.5, 4.5), margin_mm=0.0, selected_nets=["SIG"], reference_net="GND"))
    nets = {poly.net for poly in cropped.copper}
    assert "SIG" in nets
    assert "GND" in nets
    assert "OTHER" not in nets
    assert all(poly.net != "GND" or _inside(poly, 1.5, 3.5, 1.5, 4.5) for poly in cropped.copper)


def _inside(poly, x0, x1, y0, y1):
    return all(x0 - 1e-6 <= x <= x1 + 1e-6 and y0 - 1e-6 <= y <= y1 + 1e-6 for x, y in poly.points)


def test_port_outside_region_is_flagged_by_crop_geometry():
    board = _board()
    cropped = crop_board(board, CropSettings(rect=(6, 0, 9, 6), margin_mm=0.0, selected_nets=["SIG"], reference_net="GND"))
    assert not any(pad.footprint == "J1" for pad in cropped.pads)


def test_mesh_is_monotonic_and_resolves_edges():
    board = _board()
    cropped = crop_board(board, CropSettings(mode="outline", reference_net="GND"))
    port = PortBox("P1", 50, "z", (2.04, 2.8, 0.0), (2.04, 3.2, 0.2))
    fine = MeshSettings(f_max_hz=1e9, cells_per_feature=4, min_cell_mm=0, copper_cell_mm=0)
    mesh = build_mesh(board, cropped, [port], fine)
    for lines in (mesh.x, mesh.y, mesh.z):
        assert lines == sorted(lines)
        assert all(b > a for a, b in zip(lines, lines[1:]))
    assert any(abs(x - 2.0) < 0.02 or abs(x - 2.4) < 0.02 for x in mesh.x)
    # A zone vertex next to the trace is resolved. One far from it must not
    # move the grid: the same pour without that vertex produces the same lines.
    assert any(abs(x - 2.05) < 0.02 for x in mesh.x)
    plain = _board()
    plain.copper = [
        poly if (8.037, 5.4) not in poly.points else CopperPoly([(7, 5), (9, 5), (9, 5.5), (7, 5.5)], "B.Cu", "GND", "zone")
        for poly in plain.copper
    ]
    plain_crop = crop_board(plain, CropSettings(mode="outline", reference_net="GND"))
    plain_mesh = build_mesh(plain, plain_crop, [port], fine)
    assert plain_mesh.x == mesh.x
    assert plain_mesh.y == mesh.y
    for lines in (mesh.x, mesh.y, mesh.z):
        gaps = [b - a for a, b in zip(lines, lines[1:])]
        for a, b in zip(gaps, gaps[1:]):
            assert min(a, b) > 0
            assert max(a, b) / min(a, b) <= 1.45
    nx, ny, nz = mesh.cells
    assert mesh.cell_count == nx * ny * nz
    snapped = port.snapped(mesh.x, mesh.y, mesh.z)
    assert snapped.start[0] in mesh.x
    assert snapped.start[2] in mesh.z
    assert snapped.stop[2] in mesh.z
    assert snapped.start[2] != snapped.stop[2]
    y_gaps = [b - a for a, b in zip(mesh.y, mesh.y[1:])]
    for a, b in zip(y_gaps, y_gaps[1:]):
        assert max(a, b) / min(a, b) <= 1.45


def test_dense_trace_vertices_do_not_fill_the_region():
    """Arc facets sit much closer than a cell. They must not become the grid."""
    board = Board(thickness_mm=1.6)
    board.stackup = [
        StackLayer("F.Cu", "copper", 0.035),
        StackLayer("dielectric 1", "dielectric", 0.1, eps_r=4.5, loss_tangent=0.02),
        StackLayer("core", "dielectric", 1.4, eps_r=4.5, loss_tangent=0.02),
        StackLayer("B.Cu", "copper", 0.035),
    ]
    assign_stackup_z(board.stackup)
    board.outline = [(0, 0), (40, 0), (40, 20), (0, 20)]
    edge = [(2 + i * 0.01, 7.0) for i in range(801)]
    edge += [(2 + i * 0.01, 7.15) for i in range(800, -1, -1)]
    board.copper = [
        CopperPoly(edge, "F.Cu", "SIG", "trace", feature_mm=0.15),
        CopperPoly([(0, 0), (40, 0), (40, 20), (0, 20)], "B.Cu", "GND", "zone"),
    ]
    cropped = crop_board(
        board,
        CropSettings(mode="crop", rect=(0, 4, 16, 12), margin_mm=0.0, selected_nets=["SIG"], reference_net="GND"),
    )
    mesh = build_mesh(
        board,
        cropped,
        [],
        MeshSettings(f_max_hz=5e9, cells_per_feature=4, min_cell_mm=0, copper_cell_mm=0),
    )
    assert mesh.cell_count < 500_000
    assert mesh.ram_bytes_rough() / 1e6 < 50
    x_gaps = sorted(b - a for a, b in zip(mesh.x, mesh.x[1:]))
    y_gaps = [b - a for a, b in zip(mesh.y, mesh.y[1:])]
    assert x_gaps[len(x_gaps) // 2] > 0.4
    assert min(y_gaps) < 0.08


def test_small_crop_grades_away_from_the_trace():
    """A 0.12 mm trace must not fill the air box at the trace cell size.

    That is what turned a small crop into tens of GB: every cell, including
    empty margin and the air above the board, was feature_mm / cells_per_feature.
    """
    board = Board(thickness_mm=1.6)
    board.stackup = [
        StackLayer("F.Cu", "copper", 0.035),
        StackLayer("prepreg", "dielectric", 0.05, eps_r=4.2, loss_tangent=0.02),
        StackLayer("In1.Cu", "copper", 0.035),
        StackLayer("core", "dielectric", 1.55, eps_r=4.5, loss_tangent=0.02),
        StackLayer("B.Cu", "copper", 0.035),
    ]
    assign_stackup_z(board.stackup)
    board.outline = [(0, 0), (80, 0), (80, 50), (0, 50)]
    board.copper = [
        CopperPoly([(10, 20), (10.12, 20), (10.12, 28), (10, 28)], "F.Cu", "SIG", "trace", feature_mm=0.12),
        CopperPoly([(0, 0), (80, 0), (80, 50), (0, 50)], "B.Cu", "GND", "zone"),
    ]
    cropped = crop_board(
        board,
        CropSettings(mode="crop", rect=(8, 18, 20, 32), margin_mm=0.0, selected_nets=["SIG"], reference_net="GND"),
    )
    port = PortBox("P1", 50, "z", (10.06, 22.0, 0.0), (10.06, 24.0, board.layer_z("F.Cu")))
    mesh = build_mesh(
        board,
        cropped,
        [port],
        MeshSettings(f_max_hz=5e9, cells_per_feature=4, min_cell_mm=0, copper_cell_mm=0),
    )
    # The old mesh for this crop was on the order of 50 million cells.
    assert mesh.cell_count < 5_000_000
    assert mesh.ram_bytes_rough() / 1e6 < 500
    _nx, _ny, nz = mesh.cells
    assert nz < 120
    xs = [x for x in mesh.x if 9.98 <= x <= 10.14]
    assert len(xs) >= 3
    assert max(xs) - min(xs) > 0.1
    x_gaps = [b - a for a, b in zip(mesh.x, mesh.x[1:])]
    assert max(x_gaps) > 1.0
    # Z may step at a copper sheet between a thin prepreg and a thick core.
    for lines in (mesh.x, mesh.y):
        gaps = [b - a for a, b in zip(lines, lines[1:])]
        assert all(gap > 0 for gap in gaps)
        for a, b in zip(gaps, gaps[1:]):
            assert max(a, b) / min(a, b) <= 1.45
    snapped = port.snapped(mesh.x, mesh.y, mesh.z)
    assert snapped.start[2] in mesh.z and snapped.stop[2] in mesh.z
    assert snapped.start[2] != snapped.stop[2]


def test_pad_corners_share_one_lattice():
    """Off-grid pad corners must not each become a grid line.

    A private line per corner, then the ratio pass, is what kept a small crop
    near two million cells after the feature floor.
    """
    board = Board(thickness_mm=0.2)
    board.stackup = [
        StackLayer("F.Cu", "copper", 0.035),
        StackLayer("dielectric 1", "dielectric", 0.2, eps_r=4.5, loss_tangent=0.02),
        StackLayer("B.Cu", "copper", 0.035),
    ]
    assign_stackup_z(board.stackup)
    board.outline = [(0, 0), (12, 0), (12, 8), (0, 8)]
    board.copper = [CopperPoly([(0, 0), (12, 0), (12, 8), (0, 8)], "B.Cu", "GND", "zone")]
    for x in (1.03, 1.47, 2.11, 2.66, 3.19, 4.02, 4.58, 5.91):
        board.copper.append(
            CopperPoly(
                [(x, 3.07), (x + 0.42, 3.07), (x + 0.42, 3.51), (x, 3.51)],
                "F.Cu",
                "SIG",
                "pad",
                feature_mm=0.42,
            )
        )
    cropped = crop_board(board, CropSettings(mode="outline", reference_net="GND"))
    mesh = build_mesh(board, cropped, [], MeshSettings())
    assert not any(abs(x - 1.03) < 0.01 for x in mesh.x)
    assert any(abs(x - 1.0) < 0.02 for x in mesh.x)
    x_gaps = [b - a for a, b in zip(mesh.x, mesh.x[1:])]
    y_gaps = [b - a for a, b in zip(mesh.y, mesh.y[1:])]
    assert min(x_gaps) > 0.15
    assert min(y_gaps) > 0.15
    for gaps in (x_gaps, y_gaps):
        for a, b in zip(gaps, gaps[1:]):
            assert max(a, b) / min(a, b) <= 1.45
    assert mesh.cell_count < 400_000
