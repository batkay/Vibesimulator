import pytest

from pcb_simulator.model.board import Board, CopperPoly, Pad, StackLayer, Via, assign_stackup_z
from pcb_simulator.model.crop import CropSettings, auto_crop_nets, bounds_of_nets, crop_board
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


def test_crop_keeps_every_port_reference():
    board = _board()
    board.copper.append(CopperPoly([(1, 1), (1.4, 1), (1.4, 1.4), (1, 1.4)], "F.Cu", "AGND", "zone"))
    board.copper.append(CopperPoly([(3, 1), (3.2, 1), (3.2, 1.2), (3, 1.2)], "F.Cu", "OTHER", "trace", feature_mm=0.2))
    cropped = crop_board(
        board,
        CropSettings(
            mode="crop",
            rect=(0.5, 0.5, 4.0, 4.5),
            margin_mm=0.0,
            selected_nets=["SIG"],
            reference_net="GND",
            reference_nets=["AGND"],
        ),
    )
    nets = {poly.net for poly in cropped.copper}
    assert "SIG" in nets
    assert "GND" in nets
    assert "AGND" in nets
    assert "OTHER" not in nets
    assert auto_crop_nets(["SIG", "GND", "AGND"], [], ["GND", "AGND"]) == ["SIG"]


def test_auto_crop_bounds_follow_the_signal_and_skip_the_ground_pour():
    board = _board()
    assert auto_crop_nets(["SIG", "GND"], ["OTHER"], "GND") == ["SIG"]
    assert auto_crop_nets([], ["SIG", "GND"], "GND") == ["SIG"]
    assert auto_crop_nets([], ["GND"], "GND") == []
    bounds = bounds_of_nets(board, {"SIG"})
    assert bounds == pytest.approx((0.85, 0.85, 2.4, 4.0))
    cropped = crop_board(board, CropSettings(mode="crop", rect=bounds, margin_mm=0.5, reference_net="GND"))
    minx, miny, maxx, maxy = cropped.region.bounds
    assert maxx == pytest.approx(2.9)
    assert maxy == pytest.approx(4.5)
    assert minx == pytest.approx(0.35)
    assert miny == pytest.approx(0.35)
    assert maxx < 10
    assert {poly.net for poly in cropped.copper} >= {"SIG", "GND"}


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


def test_source_bandwidth_does_not_change_the_mesh():
    board = _board()
    cropped = crop_board(board, CropSettings(mode="outline", reference_net="GND"))
    port = PortBox("P1", 50, "z", (2.04, 2.8, 0.0), (2.04, 3.2, 0.2))
    low = build_mesh(board, cropped, [port], MeshSettings(f_max_hz=5e9))
    high = build_mesh(board, cropped, [port], MeshSettings(f_max_hz=80e9))
    assert low.x == high.x
    assert low.y == high.y
    assert low.z == high.z
    assert low.dx_max_mm == pytest.approx(high.dx_max_mm)


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


def test_port_nets_drop_a_far_net_and_a_far_return_via():
    board = _board()
    board.copper.append(CopperPoly([(7, 1), (7.3, 1), (7.3, 1.4), (7, 1.4)], "F.Cu", "OTHER", "trace", feature_mm=0.3))
    board.copper.append(CopperPoly([(2.45, 2.9), (2.65, 2.9), (2.65, 3.1), (2.45, 3.1)], "B.Cu", "GND", "via"))
    board.copper.append(CopperPoly([(8.5, 4.5), (8.8, 4.5), (8.8, 4.8), (8.5, 4.8)], "B.Cu", "GND", "via"))
    board.vias.append(Via(2.55, 3.0, 0.2, 0.4, ("F.Cu", "B.Cu"), "GND"))
    board.vias.append(Via(9.0, 5.0, 0.2, 0.4, ("F.Cu", "B.Cu"), "GND"))
    settings = CropSettings(mode="outline", selected_nets=["SIG"], reference_net="GND", signal_only=True)
    cropped = crop_board(board, settings)
    nets = {poly.net for poly in cropped.copper}
    assert "SIG" in nets
    assert "GND" in nets
    assert "OTHER" not in nets
    assert any(poly.kind == "via" and poly.points[0][0] < 3 for poly in cropped.copper)
    assert not any(poly.kind == "via" and poly.points[0][0] > 8 for poly in cropped.copper)
    assert any(abs(via.x - 2.55) < 1e-6 for via in cropped.vias)
    assert not any(abs(via.x - 9.0) < 1e-6 for via in cropped.vias)
    assert any(via.net == "SIG" for via in cropped.vias)
    assert any("1.5 mm" in note for note in cropped.warnings)
    kept = crop_board(board, CropSettings(mode="outline", selected_nets=["SIG"], reference_net="GND"))
    assert any(abs(via.x - 9.0) < 1e-6 for via in kept.vias)
    assert any(poly.kind == "via" and poly.points[0][0] > 8 for poly in kept.copper)


def test_mur_wall_is_shorter_than_pml():
    board = _board()
    cropped = crop_board(board, CropSettings(mode="outline", reference_net="GND"))
    port = PortBox("P1", 50, "z", (2.04, 2.8, 0.0), (2.04, 3.2, 0.2))
    pml = build_mesh(board, cropped, [port], MeshSettings(boundary="pml"))
    mur = build_mesh(board, cropped, [port], MeshSettings(boundary="mur"))
    extra = 16 * pml.dx_max_mm
    assert pml.dx_max_mm == pytest.approx(mur.dx_max_mm)
    assert (pml.x[-1] - pml.x[0]) - (mur.x[-1] - mur.x[0]) == pytest.approx(extra, abs=0.05)
    assert (pml.z[-1] - pml.z[0]) - (mur.z[-1] - mur.z[0]) == pytest.approx(extra, abs=0.05)
    assert mur.cell_count < pml.cell_count
    assert MeshSettings().boundary == "pml"


def test_project_port_nets_follow_the_ports_until_a_net_is_checked():
    from pcb_simulator.project import Project

    project = Project(
        ports=[PortBox("P1", 50, "z", (2, 2, 0), (2, 2.2, 0.2), net="SIG", reference_net="GND")]
    )
    crop = project.crop_settings()
    assert crop.selected_nets == ["SIG"]
    assert crop.signal_only is True
    assert project.boundary == "mur"
    assert project.stop == "crossing"
    assert project.mesh_settings().boundary == "mur"
    project.selected_nets = ["OTHER"]
    assert project.crop_settings().selected_nets == ["OTHER"]
    project.signal_only = False
    project.selected_nets = []
    assert project.crop_settings().selected_nets == []
    assert project.crop_settings().signal_only is False


def test_crossing_stop_covers_two_passes_and_the_source_pulse(capsys):
    import math

    from pcb_simulator.sim.job import prepare
    from pcb_simulator.sim.worker import apply_stop, crossing_steps

    job = {
        "stop": "crossing",
        "nr_ts": 1_000_000,
        "f_max_hz": 5e9,
        "materials": [{"eps_r": 4.0, "eps_inf": 3.8}],
        "mesh": {"x": [0.0, 0.2, 10.0], "y": [0.0, 0.2, 10.0], "z": [0.0, 0.2, 1.0]},
    }
    dx = 0.2e-3
    span = 10e-3
    c = 299_792_458.0
    dt = 0.99 / (c * math.sqrt(3.0 / dx / dx))
    travel = 2.0 * span / (c / math.sqrt(4.0))
    pulse = 9.0 / (math.pi * 5e9)
    expected = max(1, math.ceil((travel + pulse) / dt))
    assert crossing_steps(job) == expected
    assert expected < 20_000
    stepped = apply_stop(job)
    assert job["nr_ts"] == 1_000_000
    assert stepped["nr_ts"] == expected
    text = capsys.readouterr().out
    assert "PCB_SIM" not in text
    assert f"{expected:,}" in text

    capped = apply_stop({**job, "nr_ts": 50})
    assert capped["nr_ts"] == 50
    assert "PCB_SIM" not in capsys.readouterr().out

    assert apply_stop({**job, "stop": "energy", "nr_ts": 12345})["nr_ts"] == 12345
    assert capsys.readouterr().out == ""
    assert apply_stop({"stop": "crossing", "nr_ts": 1000})["nr_ts"] == 1000

    board = _board()
    port = PortBox("P1", 50, "z", (2.2, 2.9, 0.0), (2.2, 3.1, 0.2), net="SIG", reference_net="GND")
    written, _mesh, _warnings = prepare(
        board,
        CropSettings(mode="outline", reference_net="GND"),
        [port],
        MeshSettings(boundary="mur"),
        nr_ts=1_000_000,
        stop="crossing",
    )
    assert written["nr_ts"] == 1_000_000
    assert written["stop"] == "crossing"
    assert written["boundary"] == "mur"
    live = apply_stop(written)
    assert written["nr_ts"] == 1_000_000
    assert live["nr_ts"] < 1_000_000
    assert live["nr_ts"] == crossing_steps(written)
    assert written["route_mm"] < 5
    assert live["stop_kind"] == "trace"


def test_trace_stop_waits_for_the_open_to_rise():
    import math

    from pcb_simulator.sim.worker import crossing_steps

    job = {
        "stop": "crossing",
        "nr_ts": 1_000_000,
        "f_max_hz": 80e9,
        "materials": [{"eps_r": 4.5}],
        "mesh": {"x": [0.0, 0.2, 10.0], "y": [0.0, 0.2, 10.0], "z": [0.0, 0.05, 1.0]},
        "route_mm": 48.0,
    }
    dx = 0.2e-3
    dz = 0.05e-3
    c = 299_792_458.0
    dt = 0.99 / (c * math.sqrt(2.0 / dx / dx + 1.0 / dz / dz))
    travel = 2.0 * 0.048 / (c / math.sqrt(4.5))
    pulse = 9.0 / (math.pi * 80e9)
    settle = 0.15 * travel + 2.0 / 80e9
    expected = max(1, math.ceil((travel + pulse + settle) / dt))
    assert crossing_steps(job) == expected
    short = {**job, "route_mm": 5.0}
    assert crossing_steps(job) > 4 * crossing_steps(short)
    slower = {**job, "f_max_hz": 5e9}
    assert crossing_steps(slower) > expected


def test_trace_round_trip_counts_as_finished():
    from pcb_simulator.sim.worker import _completed, apply_stop, crossing_steps

    job = {
        "stop": "crossing",
        "nr_ts": 1_000_000,
        "f_max_hz": 5e9,
        "materials": [{"eps_r": 4.0}],
        "mesh": {"x": [0.0, 0.2, 10.0], "y": [0.0, 0.2, 10.0], "z": [0.0, 0.2, 1.0]},
        "route_mm": 2.0,
    }
    stepped = apply_stop(job)
    assert job["nr_ts"] == 1_000_000
    assert stepped["nr_ts"] == crossing_steps(job)
    assert stepped["stop_kind"] == "trace"
    assert _completed(False, stepped) is True
    assert _completed(True, stepped) is True
    assert _completed(False, {}) is False
    assert _completed(None, stepped) is None
