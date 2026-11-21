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
| `pcb_simulator/app.py` | Main window, crop, ports, pairs, the S-parameter run, the PDN thread, solver log |
| `pcb_simulator/project.py` | `.pcbsim.json` beside the board. Optional PDN keys: VRM, loads, parts, conductivity, plating, pitch, power net, AC layers. S-parameter speed keys, missing on an older file: `boundary` `mur`, `stop` `crossing`, `signal_only` true. The KiCad file is never modified |
| `pcb_simulator/model/load.py` | KiCad, ODB++ folder or archive, IPC-2581. Native Altium `.PcbDoc` and Xpedition `.pcb` raise `NATIVE_MESSAGE` |
| `pcb_simulator/model/kicad.py` | KiCad parser and `kicad_rotate`. Coordinates stay KiCad-frame: +X right, +Y down |
| `pcb_simulator/model/board.py` | Stackup. `assign_stackup_z` advances z by dielectric thickness only |
| `pcb_simulator/model/ports.py` | `port_at_click` for a placed port. `vertical_port_on_pad` for Port every pad. `port_between` for PDN boxes |
| `pcb_simulator/model/mesh.py` | Lattice, z lines, vias, lumped-port snap. `MeshSettings.boundary` defaults to `pml`. `mur` uses `air_cells` outside the grade. `pml` uses `pml_cells` plus `air_cells` |
| `pcb_simulator/model/crop.py` | Rectangle, margin, which nets stay. `signal_only` keeps a reference via, and its annular ring, only within 1.5 mm of non-reference copper. The reference pour stays |
| `pcb_simulator/sim/prepare_worker.py` | Mesh build in the solver Python, before the FDTD run |
| `pcb_simulator/sim/worker.py` | openEMS. One excitation per port. Writes `results.npz`. After `CalcPort`, calls `calc/s_parameters.py` and `calc/waveforms.py`. `boundary` `mur` is `SetBoundaryCond(["MUR"] * 6)`. Anything else is PML. `stop` `crossing` lowers in-memory `nr_ts` to one round trip of `route_mm` (the longest port route) at `c/sqrt(max eps)`, plus 15% of that trip and `2/f_max` so the end reflection can rise, plus the openEMS pulse `9/(pi*f_max)`, and never above the typed cap. The TDR ohms scale comes from `tdr_ohm_limits` on the first three quarters of the window, with headroom from the median of that part, so a rail at the end leaves the chart. With no `route_mm` the distance stays the longest mesh span. Reaching the trace trip sets `stop_kind` `trace` and counts as finished. Hitting the typed cap before that trip is not decayed. `job.json` keeps the typed cap and stores `route_mm`. Debye poles become `CSPropDebyeMaterial` only when the openEMS binary contains `Debye Dispersive Material Extension`. On Windows that string is in `openEMS.dll` beside `OPENEMS_INSTALL_PATH`. The `.pyd` does not contain it. Otherwise the layer stays at the stackup εr and kappa 0 |
| `pcb_simulator/sim/djordjevic.py` | Djordjevic–Sarkar fit. The stackup tanδ is the 10 GHz point. Corners are 1 MHz and 200 GHz. Blank or 0 stays a constant εr. The worker decides whether the installed openEMS can timestep the poles |
| `pcb_simulator/calc/` | One file per calculated result. Not a directory a run writes into. `s_parameters.py` is `S[i, k] = uf_ref[i] / uf_inc[k]`. `z_parameters.py` is scikit-rf `s2z`. Also `tdr.py`, `differential.py`, `waveforms.py`, `touchstone.py`, `csv_export.py`, `network.py`, `load.py` |
| `pcb_simulator/pdn/circuit.py` | DC IR drop and AC plane impedance on a sparse grid. Does not run openEMS |
| `pcb_simulator/pdn/io.py` | `.sim/pdn/result.npz` and `meta.json`. `allow_pickle` stays off |
| `pcb_simulator/view/board_view.py` | Canvas. `model_to_scene` is identity |
| `pcb_simulator/view/results_view.py` | Plots, including the PDN page. Display Z0 and εeff redraw saved data. `skrf.plotting.smith` draws on the axes it is given and returns None. `decayed` false sets `decay_notice`. The window appends that text to the solver log. The results banner stays for PDN notes |
| `pcb_simulator/view/shelf.py` | The one tab row: File, Place, Components, View, Board, Run, Settings. The menu bar stays hidden |
| `pcb_simulator/view/flow_layout.py` | Wrapping rows on the shelf |
| `pcb_simulator/view/info_tip.py` | One `?` beside each option, one small popup |

Launch is `pcb_simulator/__main__.py`. A path argument opens a board or a `.pcbsim.json`.

## Where a user's files live

The solver interpreter is a machine setting: `QSettings("pcb-simulator", "pcb-simulator")`, key `solver_python`. Empty means the interpreter that launched the GUI. Do not run Test import on startup. The window and the solver may be different Pythons. openEMS is not bundled and the app does not download it.

The S-parameter job directory is `<board parent>/.sim/current` (for an ODB++ folder, next to that folder). A new S-parameter run deletes that folder and writes it again. PDN writes `<board parent>/.sim/pdn` and does not delete `.sim/current`. Opening a board reloads both when they exist. A launch with no path stays empty. `README.md` has the file table. The short version:

- `job.json` is the mesh, ports, f max, the typed step cap, `boundary`, `stop`, `route_mm` when a port route can be measured, and `threads` (`null` is auto). A crossing stop lowers the cap in the worker's memory and leaves this file at the typed cap.
- `exc_0/`, `exc_1/`, … are one openEMS run per port. `openEMS_run_stats.txt` is the live timestep log. A header and no data rows means no step has been recorded. `openEMS_stats.txt` appears when that excitation stops.
- `results.npz` appears only after every port finishes. Arrays: `freq`, `S`, `z0`, `port_names`, `decayed`, plus `t_k`, `v_k`, `i_k` per excitation.
- The worker prints `PCB_SIM` plus one JSON object: `start`, `threads`, `excite`, `geometry`, `operator`, `done`, `error`. The mesh build prints `PCB_PREP` the same way (`crop`, `mesh`, `cells`, `triangles`, `write`, `ready`, `error`). The Solver log turns those into sentences as they arrive. The dock keeps the last 2000 lines. The status bar echoes a few of the events.
- openEMS prints `Multithreaded operator using N threads` and then walks every cell with no further line of its own. `operator` events every 10 seconds cover that wait. They stop when `openEMS_run_stats.txt` appears, which is the timestep loop. There is no cell-by-cell percentage inside that walk.

`S[i, k]` is `uf_ref[i] / uf_inc[k]` in `calc/s_parameters.py`, with the other ports terminated. Frequencies are `linspace(f_max / n_freq, f_max, n_freq)`. The source is `SetGaussExcite(0, f_max)` with f max the −20 dB frequency.

## Reports, by what the user sees

Read the job files and the port list before changing the solver. Say which file you used.

**The ohms look wrong, or the TDR is not a flat 50 Ω.** Port R is the instrument termination, default 50 Ω, not the trace impedance. Then check the crop, the checked nets, and each port's reference: unchecked copper is omitted, and those reference nets are kept either way. With nothing checked and Port nets off, every net in the region is kept. Port nets, the default when the key is missing, keeps the port nets and each reference, and drops reference vias farther than 1.5 mm from the signal copper. The reference pour stays. Copper thickness is stored and drawn in the stackup and is not meshed, so sidewall capacitance is missing; the README table is the size of that miss. The copper lattice (cells / feature 2 → 0.2 mm) snaps width and gap, up to about half a pitch. A gap under one pitch is one cell. Dielectric tanδ on the stackup is the 10 GHz value. A layer with tanδ above 0 is a Djordjevic–Sarkar Debye fit (`sim/djordjevic.py`), written on the job as `debye` poles. The worker applies `CSPropDebyeMaterial` only when the openEMS binary contains `Debye Dispersive Material Extension`. On Windows that string is in `openEMS.dll`. v0.37.0-rc1 does not. Its Lorentz updater would read a pole strength as a plasma frequency and a relaxation time as a resonant frequency, so the worker keeps the stackup εr and kappa 0. A blank or 0 is a constant εr. A conductivity matched at f max / 2 makes a uniform trace fall along the TDR. A real tanδ of about 0.02 does not, once that updater is the one stepping. Conductor loss and roughness are omitted.

**A differential pair looks inconsistent.** A pair is two existing ports at the same end. Ports at opposite ends of one trace are a through path. Adding or removing a pair does not rerun the solver. `Sdd = 0.5 (Spp − Spn − Snp + Snn)` and `Scc` uses plus signs, after both ports share one reference (their average, when Rp and Rn differ). Zdiff is referenced to Rp + Rn. Display Z0 renormalizes the single-ended network, and the differential curves use twice that value. Touchstone stays single-ended. The results list checks Sij after the solve. Sii is the reflection at port i. Sij is the wave leaving port i when port j is driven. Reflections start checked. Magnitude, phase, Smith, and Z follow those checks. CSV is one file with a frequency block (every S and Z, plus Sdd and Scc), a TDR block for every port, and a waveform block per excitation. There is no odd-mode source. Sheet copper and the lattice both move Zdiff.

**The TDR has few samples, or the line falls off the plot.** Sample spacing is about `1 / (2 × f max)`: 5 GHz is about 0.1 ns, about 9 mm on the distance axis at εeff 2.75. The ohms-plot 10–90% edge is the Hamming step of S, about 0.23 ns at 5 GHz (about 1.13 / f max, about 20 mm on that axis at εeff 2.75). The v(t) tab is the Gaussian source; integrating it gives a step of about 0.17 ns at 5 GHz (about 0.865 / f max). Raise f max and run again to put samples closer together and to shorten that edge. The mesh does not follow f max. Open space stays at λ/10 of 5 GHz, and the copper lattice stays on its own pitch. Frequency points set the record length, about `n_freq / f max` (401 at 5 GHz is about 80 ns). The plot then zooms with `tdr_view_window`. When the record ends on a plateau, the window runs until the impedance has settled near that late-time value, and a large step at the port does not crop a later change along the trace. When the tail keeps moving, the window stops after the last flat stretch of the trace plus a short step, so samples past the end of the trace stay off the plot. A run that hits the typed step cap before the trace round trip sets `decay_notice`, and `MainWindow._announce_decay` appends it to the solver log. The results tab does not keep that sentence. The launch edge itself stays a few samples wide at any f max, because the rise time and the sample spacing both shrink as 1 / f max. εeff only scales the millimeter axis. Both controls are baked in at Run.

**A click places the port somewhere else.** The port tool acts on mouse release. `model_to_scene` is identity. The port tool is two clicks and calls `copper_clicked` with the cursor coordinates. It skips `pad_at`, so a pad under the cursor does not move the click to the pad center. `port_at_click` builds the sheet at the first click, on that net. The sheet is 0.2 mm across and narrows when that is what keeps a third net out. The second click names that port's reference. If that net is already under the first click, the sheet stays there and stops on the nearest layer of that net. Otherwise it runs toward the click and stops once it is on that copper. A failed second click keeps the first point. The reason is printed on the launch console and appended to the solver log. There is no reference dropdown. Crop keeps every port's reference. A PDN ground click sets the return when no port reference and no earlier ground click has chosen one. A later ground click has to match that return. A saved default of GND does not reject a different first ground click. `marks` are the two clicks. The canvas draws the line from the marks, and the rectangle is the sheet. `_set_tool("port")` arms the two clicks. Other tools clear them. Do not snap the drawn ends to pad edges, and do not highlight a net on the first click. `Port every pad` still uses `vertical_port_on_pad` down to the reference the last port named.

**The PDN drop or |Z| looks wrong.** The grid is `pdn/circuit.py`. Pitch defaults to 0.5 mm and is not the FDTD lattice. Foil thickness is the stackup copper thickness. A cell is metal only when an inset square intersects the polygon, so a shared boundary is not copper. A VRM or a load is two clicks: power, then ground. `gx` and `gy` are the ground click. The first ground click sets the return. A later one has to match a reference a port or an earlier ground click already chose. A missing `gx` means the return is at the power point, which is how an older project file loads. The typed VRM voltage is held between those clicks. Loop drop is Vsrc − (Vp − Vg). A part with C is open at DC. AC |Z| with a VRM short matches the DC loop resistance at the low end. The bare 1/(ωC) law is the circuit with no VRM. Loop L is on the power branch only. Skin effect is Rdc × max(1, t/δ). A one-cell board whose VRM is a short and whose load sits on the same nodes has no free AC node, so Z stays NaN. Do not delete `.sim/current` from the PDN path. `prepare_worker` deletes only `.sim/current`.

**The window froze, or the board will not pan.** Preview mesh calls `build_mesh` on the GUI thread, so that button can stall the window until it returns. Run does not: it starts `prepare_worker` in the solver Python, then `sim.worker`. Pan and scroll are supposed to keep working during that prep. A white mist or a stuck scroll during a long run is a GUI bug, separate from the worker still stepping.

**The log stops at "Multithreaded operator using N threads" and the process dies.** Exit −1073740791 is Windows `0xC0000409`. `solver_exit_message` already describes it: the operator allocation ran out of memory. Rough RAM is `cells × 100` bytes. The confirm gate defaults to 8e6 cells. Crop or lower cells / feature. f max does not change the cell count. Threads set to 1 is the check for the multi-thread operator setup.

**The run is taking much longer than expected.** A job that has already started keeps the mesh, the boundary, and the step cap it was given. Boundary MUR, Stop Crossing, and Port nets apply on the next Run. Cost is cells × timesteps × ports, divided by the cores the timestep loop is actually using. The smallest cell sets the timestep, so a finer grid costs twice. N ports means N full runs. FDTD often levels off around 4–8 threads. `threads` in the GUI uses the special value 0, shown as "auto"; the worker turns `None` or 0 into `os.cpu_count()` and passes that positive `numThreads`. openEMS 0.37 treats `numThreads` 0 as "start at 1 and search", and the loop then stays on one core. Do not omit `numThreads`. A header-only `openEMS_run_stats.txt` means the timestep loop has started and has not flushed a data row. The operator walk is the wait before that file exists. `LastWriteTime` on that file is the last flush. CSXCAD `Unused primitive (type: Polygon)` means a polygon missed every mesh line. It is not a timestep cost.

**The board or a footprint looks mirrored.** The canvas Y axis is the KiCad axis. Do not negate view Y. Footprint rotation goes through `kicad_rotate`, which matches KiCad 10. ODB++ and IPC-2581 flip Y once on import. Pads and zones are in the solve. Mask, silk, fab, courtyard, and 3D models are not.

**Altium or Xpedition will not open.** Native `.PcbDoc` and `.pcb` are not parsed. The user exports ODB++ or IPC-2581. The message is `NATIVE_MESSAGE` in `model/load.py`.

## Leave these alone unless the user asks

- Finite copper thickness, conductor loss, roughness, and sidewall capacitance. `assign_stackup_z` does not add copper thickness to z. Sheets are PEC (`AddMetal`).
- An odd-mode source, IBIS models, eye diagrams, field dumps, far field, and a 3D view. Placed RLC parts are the PDN grid only. The FDTD run ignores them. Do not put RLC into the openEMS mesh.
- Bundling openEMS, or wrapping Gerber as the solver.
- The mesh defaults: cells / feature 2, `min_cell_mm` 0.05, `copper_cell_mm` 0.2. Pitch is `copper_cell_mm * 2 / cells_per_feature`, floored at `min_cell_mm`. `copper_cell_mm` 0 is the exact-edge path used by the slow test.
- Z discretization. If `thickness / n` is under `min_cell_mm`, `n` becomes `max(1, floor(thickness / min_cell_mm))`. Do not ratio-smooth dz across a copper sheet, and do not restore the old bisection mesher.
- Pour vertices are not each a grid line. The pour bounding box is. Vias are solid cylinders of the drill diameter plus annular disks, not plated tubes.
- Energy-limit default 1e-4, f max 5 GHz, 401 frequency points, 1_000_000 step cap as the typed ceiling. A project file with the keys missing uses Boundary MUR, Stop Crossing, and Port nets. `MeshSettings()` with no arguments stays PML. Open space at λ/10 of 5 GHz (`mesh_f_hz`, not the source f max), max grade 1.4. PML of 8 is the absorber when Boundary is PML.
- Click-to-click `marks`. The translucent rectangle is the sheet; the line the user follows is the marks.

## GUI constraints already paid for

The window is one shelf across the top, then the board and the results. Do not put the menu bar back. File, Place, Components, View, Board, Run, and Settings are tabs on that shelf. Place only places. The Components tab opens a window for ports, differential pairs, the VRM, loads, and parts. Close hides that window. Preview mesh, S parameters, PDN, and Cancel stay on Run. The splitter has two panes, stretch about 3/1, children not collapsible. Until the user drags the handle, the board keeps about three quarters of the width. A drag sticks. The shelf is a tab widget with maximum height 320. Clicking the open tab hides the panel and leaves the tab bar. Clicking a tab shows that panel again. Do not add a second collapse control. Each tab is a scroll area with the horizontal bar forced off. A horizontal scrollbar at a 1000 px window is a failure. The Board tab may scroll vertically. `FitLabel` wraps, and its minimum width is the longest word. Controls use `QSizePolicy.Ignored` and minimum width 0. A wrapping note uses `NoteWithInfo`, which reports `heightForWidth`. Crop and Whole outline stay radio siblings of the same parent; `autoExclusive` fails if they get different parents. Ctrl+O, Ctrl+S, and Ctrl+R stay on Open, Save, and S parameters. The window title ends with `*` while the project differs from the `.pcbsim.json` beside the board, and the status bar says Saved or Unsaved. The File tab repeats that sentence. Opening a board that has no project file starts unsaved. Putting an edited value back clears the star. Measurements and which layers are drawn are not part of that file. `model_to_scene` stays the identity.

Help is one `?` per option in `view/info_tip.py`. The popup is a frameless tool window. Clicking the same button hides it. A press on an info button must not hide the popup in the application event filter, or the following `clicked` reopens it. Do not put a single help button back at the top of the window.

Port graphics pass clicks through (`setAcceptedMouseButtons(NoButton)`). The list shows the footprint and pad when the first click hit a pad, the signal net, and that port's reference.
