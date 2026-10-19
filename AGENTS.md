# Agent notes

Desktop PCB simulator: import a board, crop it, attach lumped ports, run openEMS, plot S, Z, and TDR. `README.md` is the user-facing description of the assumptions. This file is how to change the code and how to investigate a report. When this file and the code disagree, trust the code, then update this file.

## Working agreement

- Keep solver assumptions in `README.md`. Update that section when one changes. Do not add other docs unless the user asks.
- User boards live outside this repo. Do not copy one in, do not commit one, and do not recursively list a large board directory.
- Do not start an openEMS solve unless the user asks. Preview mesh and the fast tests are the usual checks.
- A running solve is a separate process. Closing the app or Cancel kills it. Do not kill it from a shell unless the user asks. A job already running keeps the mesh, thread count, f max, and frequency points it started with.
- GUI changes appear after a full quit and `python -m pcb_simulator`, run from this directory. `python -m .\pcb_simulator\` fails with a relative-module error.
- The shell here is PowerShell 5.1. Chain commands with `;`. A double-quoted `py -3 -c` string treats `[...]` as a wildcard; write a temporary `.py` and delete it.

## Check your work

```
py -3 -m pytest tests -q --tb=line -m "not slow"
```

The GUI tests set `QT_QPA_PLATFORM=offscreen`. There is no browser. A layout or interaction change needs the matching offscreen test exercised, then the fast suite.

`tests/test_microstrip_openems.py` is the slow test. It runs openEMS on `examples/microstrip_50ohm.kicad_pcb` with `MeshSettings(f_max_hz=5e9, cells_per_feature=2, min_cell_mm=0, copper_cell_mm=0)`, which is exact feature edges. The app default is a 0.2 mm lattice. Leave that test alone unless the user asks to re-run or change it.

## Where the code lives

| Path | What it owns |
| --- | --- |
| `pcb_simulator/app.py` | Main window, crop, ports, pairs, run/cancel, solver log |
| `pcb_simulator/project.py` | `.pcbsim.json` beside the board. The KiCad file is never modified |
| `pcb_simulator/model/load.py` | KiCad, ODB++ folder or archive, IPC-2581. Native Altium `.PcbDoc` and Xpedition `.pcb` raise `NATIVE_MESSAGE` |
| `pcb_simulator/model/kicad.py` | KiCad parser and `kicad_rotate`. Coordinates stay KiCad-frame: +X right, +Y down |
| `pcb_simulator/model/board.py` | Stackup. `assign_stackup_z` advances z by dielectric thickness only |
| `pcb_simulator/model/ports.py` | One-click pad ports and two-click `port_between` |
| `pcb_simulator/model/mesh.py` | Lattice, z lines, vias, lumped-port snap |
| `pcb_simulator/model/crop.py` | Rectangle, margin, which nets stay |
| `pcb_simulator/sim/prepare_worker.py` | Mesh build in the solver Python, before the FDTD run |
| `pcb_simulator/sim/worker.py` | openEMS. One excitation per port. Writes `results.npz` |
| `pcb_simulator/sim/postprocess.py` | Renormalize, Sdd/Scc, TDR, Touchstone, CSV |
| `pcb_simulator/view/board_view.py` | Canvas. `model_to_scene` is identity |
| `pcb_simulator/view/results_view.py` | Plots. Display Z0 and εeff redraw saved data. `skrf.plotting.smith` draws on the axes it is given and returns None |
| `pcb_simulator/view/info_tip.py` | One `?` beside each option, one small popup |

Launch is `pcb_simulator/__main__.py`. A path argument opens a board or a `.pcbsim.json`.

## Where a user's files live

The solver interpreter is a machine setting: `QSettings("pcb-simulator", "pcb-simulator")`, key `solver_python`. Empty means the interpreter that launched the GUI. Do not run Test import on startup. The window and the solver may be different Pythons. openEMS is not bundled and the app does not download it.

The job directory is `<board parent>/.sim/current` (for an ODB++ folder, next to that folder). A new run deletes it and writes it again. `README.md` has the file table. The short version:

- `job.json` is the mesh, ports, f max, step cap, and `threads` (`null` is auto).
- `exc_0/`, `exc_1/`, … are one openEMS run per port. `openEMS_run_stats.txt` is the live timestep log. A header and no data rows means no step has been recorded. `openEMS_stats.txt` appears when that excitation stops.
- `results.npz` appears only after every port finishes. Arrays: `freq`, `S`, `z0`, `port_names`, `decayed`, plus `t_k`, `v_k`, `i_k` per excitation.
- The worker prints `PCB_SIM` plus one JSON object: `start`, `threads`, `excite`, `geometry`, `operator`, `done`, `error`. The mesh build prints `PCB_PREP` the same way (`crop`, `mesh`, `cells`, `triangles`, `write`, `ready`, `error`). The Solver log turns those into sentences as they arrive. The dock keeps the last 2000 lines. The status bar echoes a few of the events.
- openEMS prints `Multithreaded operator using N threads` and then walks every cell with no further line of its own. `operator` events every 10 seconds cover that wait. They stop when `openEMS_run_stats.txt` appears, which is the timestep loop. There is no cell-by-cell percentage inside that walk.

`S[i, k]` is `uf_ref[i] / uf_inc[k]` with the other ports terminated. Frequencies are `linspace(f_max / n_freq, f_max, n_freq)`. The source is `SetGaussExcite(0, f_max)` with f max the −20 dB frequency.

## Reports, by what the user sees

Read the job files and the port list before changing the solver. Say which file you used.

**The ohms look wrong, or the TDR is not a flat 50 Ω.** Port R is the instrument termination, default 50 Ω, not the trace impedance. Then check the crop, the checked nets, and the reference net: unchecked copper is omitted, and the reference is kept either way. With nothing checked, every net in the region is kept. Copper thickness is stored and drawn in the stackup and is not meshed, so sidewall capacitance is missing; the README table is the size of that miss. The copper lattice (cells / feature 2 → 0.2 mm) snaps width and gap, up to about half a pitch. A gap under one pitch is one cell. Dielectric κ is `2π f_center ε0 εr tanδ` at `f_center = f_max / 2`. Conductor loss and roughness are omitted.

**A differential pair looks inconsistent.** A pair is two existing ports at the same end. Ports at opposite ends of one trace are a through path. Adding or removing a pair does not rerun the solver. `Sdd = 0.5 (Spp − Spn − Snp + Snn)` and `Scc` uses plus signs, after both ports share one reference (their average, when Rp and Rn differ). Zdiff is referenced to Rp + Rn. Display Z0 renormalizes the single-ended network, and the differential curves use twice that value. Touchstone stays single-ended. The results list checks Sij after the solve. Sii is the reflection at port i. Sij is the wave leaving port i when port j is driven. Reflections start checked. Magnitude, phase, Smith, and Z follow those checks. CSV is one file with a frequency block (every S and Z, plus Sdd and Scc), a TDR block for every port, and a waveform block per excitation. There is no odd-mode source. Sheet copper and the lattice both move Zdiff.

**The TDR has few samples, or the line falls off the plot.** Sample spacing is about `1 / (2 × f max)`: 5 GHz is about 0.1 ns, about 9 mm on the distance axis at εeff 2.75. The ohms-plot 10–90% edge is the Hamming step of S, about 0.23 ns at 5 GHz (about 1.13 / f max, about 20 mm on that axis at εeff 2.75). The v(t) tab is the Gaussian source; integrating it gives a step of about 0.17 ns at 5 GHz (about 0.865 / f max). Raise f max and run again to put samples closer together and to shorten that edge. That also refines open space. The copper lattice stays put. Frequency points set the record length, about `n_freq / f max` (401 at 5 GHz is about 80 ns). The plot then zooms with `tdr_view_window` until the impedance has settled near its late-time value. A large step at the port does not crop a later change along the trace. The launch edge itself stays a few samples wide at any f max, because the rise time and the sample spacing both shrink as 1 / f max. εeff only scales the millimeter axis. Both controls are baked in at Run.

**A click places the port somewhere else.** The port tool acts on mouse release. `model_to_scene` is identity. One click uses `pad_at` (pad polygon buffered 0.08 mm, first STRtree hit) and builds a vertical port down to the reference. Two clicks call `copper_clicked` with the cursor coordinates and skip `pad_at`. `port_between` stores `marks` at the two clicks, and the canvas draws the line, halo, dots, and label from `marks`. On one layer the sheet is the box between the clicks; a short axis under 0.2 mm is padded to 0.2 mm, centered, so the long-axis ends stay on the clicks. On two layers the sheet is a 0.2 mm square on the first click and the second click only picks the layer. One-click ports have `marks is None`. `_set_tool` clears a stale two-click state. Do not snap the drawn ends to pad edges, and do not highlight a net on the first click.

**The window froze, or the board will not pan.** Preview mesh calls `build_mesh` on the GUI thread, so that button can stall the window until it returns. Run does not: it starts `prepare_worker` in the solver Python, then `sim.worker`. Pan and scroll are supposed to keep working during that prep. A white mist or a stuck scroll during a long run is a GUI bug, separate from the worker still stepping.

**The log stops at "Multithreaded operator using N threads" and the process dies.** Exit −1073740791 is Windows `0xC0000409`. `solver_exit_message` already describes it: the operator allocation ran out of memory. Rough RAM is `cells × 100` bytes. The confirm gate defaults to 8e6 cells. Crop, lower f max, or lower cells / feature. Threads set to 1 is the check for the multi-thread operator setup.

**The run is taking much longer than expected.** Cost is cells × timesteps × ports, divided by the cores the timestep loop is actually using. The smallest cell sets the timestep, so a finer grid costs twice. N ports means N full runs. FDTD often levels off around 4–8 threads. `threads` in the GUI uses the special value 0, shown as "auto"; the worker turns `None` or 0 into `os.cpu_count()` and passes that positive `numThreads`. openEMS 0.37 treats `numThreads` 0 as "start at 1 and search", and the loop then stays on one core. Do not omit `numThreads`. A header-only `openEMS_run_stats.txt` means the timestep loop has started and has not flushed a data row. The operator walk is the wait before that file exists. `LastWriteTime` on that file is the last flush. CSXCAD `Unused primitive (type: Polygon)` means a polygon missed every mesh line. It is not a timestep cost.

**The board or a footprint looks mirrored.** The canvas Y axis is the KiCad axis. Do not negate view Y. Footprint rotation goes through `kicad_rotate`, which matches KiCad 10. ODB++ and IPC-2581 flip Y once on import. Pads and zones are in the solve. Mask, silk, fab, courtyard, and 3D models are not.

**Altium or Xpedition will not open.** Native `.PcbDoc` and `.pcb` are not parsed. The user exports ODB++ or IPC-2581. The message is `NATIVE_MESSAGE` in `model/load.py`.

## Leave these alone unless the user asks

- Finite copper thickness, conductor loss, roughness, and sidewall capacitance. `assign_stackup_z` does not add copper thickness to z. Sheets are PEC (`AddMetal`).
- An odd-mode source, RLC or IBIS models, eye diagrams, field dumps, far field, and a 3D view.
- Bundling openEMS, or wrapping Gerber as the solver.
- The mesh defaults: cells / feature 2, `min_cell_mm` 0.05, `copper_cell_mm` 0.2. Pitch is `copper_cell_mm * 2 / cells_per_feature`, floored at `min_cell_mm`. `copper_cell_mm` 0 is the exact-edge path used by the slow test.
- Z discretization. If `thickness / n` is under `min_cell_mm`, `n` becomes `max(1, floor(thickness / min_cell_mm))`. Do not ratio-smooth dz across a copper sheet, and do not restore the old bisection mesher.
- Pour vertices are not each a grid line. The pour bounding box is. Vias are solid cylinders of the drill diameter plus annular disks, not plated tubes.
- Energy-limit default 1e-4, f max 5 GHz, 401 frequency points, 1_000_000 step cap, PML of 8, open space at λ/10, max grade 1.4.
- Click-to-click `marks`. The translucent rectangle is the sheet; the line the user follows is the marks.

## GUI constraints already paid for

The side panes track the window (splitter stretch about 1/3/1, children not collapsible, minimum width 280). The left pane scrolls vertically. A horizontal scrollbar means some child has a minimum width wider than the viewport. `FitLabel` wraps, and its minimum width is the longest word. A `?` beside a control uses `QSizePolicy.Ignored` and minimum width 0 so the button takes space from the control. A wrapping note uses `NoteWithInfo`, which reports `heightForWidth`. Crop and Whole outline stay radio siblings of the same group box; `autoExclusive` fails if they get different parents.

Help is one `?` per option in `view/info_tip.py`. The popup is a frameless tool window. Clicking the same button hides it. A press on an info button must not hide the popup in the application event filter, or the following `clicked` reopens it. Do not put a single help button back at the top of the pane.

Port graphics pass clicks through (`setAcceptedMouseButtons(NoButton)`). The list shows the net for a two-click port and the footprint, pad, and net for a one-click port.
