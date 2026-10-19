# PCB simulator

Desktop app for a cropped region of a board. It imports the copper, attaches lumped ports, and runs openEMS. Results are S parameters, Z parameters, and a single-ended TDR. Export is Touchstone and CSV.

```
python -m pcb_simulator
```

Open the board from the app. The solver Python is a machine setting (empty uses the same interpreter). This app does not download openEMS. Restart the app after code changes. One run at a time: closing the window stops the solver.

The controls on the left and the results on the right each take about a fifth of the window, and the board takes the rest. Growing or shrinking the window moves all three. Drag a splitter handle when you want a different split; after that, the handles stay where you put them. Notes in the left pane wrap to that pane, and the pane scrolls up and down. The stackup columns stretch with the left pane.

## Assumptions

These are the choices that change the impedance you read back.

### Copper thickness is not in the solve

Each copper layer is a perfect-conductor sheet on one z plane. The thickness in the stackup is stored and shown. It does not move the next layer, and it is not meshed.

Only dielectric thickness separates the sheets. On a board whose prepreg is 0.1 mm, F.Cu and the copper under it are 0.1 mm apart. A 35 µm foil is not added to that gap.

A real foil has sidewalls. That extra capacitance lowers the characteristic impedance. A closed-form microstrip estimate (not a result from this solver), εr 4.5:

| Height | Width | 35 µm copper | Zero-thickness sheet |
| --- | --- | --- | --- |
| 0.10 mm | 0.15 mm | 48 Ω | 58 Ω |
| 0.10 mm | 0.20 mm | 40 Ω | 47 Ω |
| 1.24 mm | 2.2 mm | 51 Ω | 52 Ω |

On the thin prepreg the miss is several ohms. On a thick core it is about an ohm. Differential pairs are more sensitive still, because the odd mode uses the facing sidewalls.

Conductor loss and roughness are also omitted. The metal is perfect, so series R is 0. Dielectric εr and loss tangent are included. Loss tangent is applied as a conductivity at f max / 2.

**Revisit:** extrude the foil, or widen the sheet by about the thickness, so this error is in the Z parameters. Meshing 35 µm directly needs cells smaller than the foil and pulls the timestep back down for the whole run.

### Port R is the instrument

The default 50 Ω is the termination of that lumped port, same role as a VNA or TDR. It is not the trace impedance. A 40 Ω trace on a 50 Ω port shows a reflection and about 40 Ω on the TDR. Each port can be edited.

A port is a sheet from the signal copper down to the reference net, or a sheet in one layer between two clicks. There is no modal port.

### Differential impedance

There is no odd-mode source. A differential result is computed from two normal ports placed on the two legs at the same end.

Each port still goes to the reference plane at its own R. The solver runs once per port and fills the full single-ended S matrix. In the Ports group, choose the positive leg and the negative leg and add a differential pair. That does not start a new solve. The results list then offers the pair:

- TDR shows Zdiff and Zcommon.
- Z shows Zdiff versus frequency.
- Smith shows Sdd.
- Magnitude and phase show Sdd and Scc. Uncheck a curve to hide it.

With the single-ended matrix normalized to one reference R (the usual 50 Ω):

- Sdd = 0.5 (Spp − Spn − Snp + Snn)
- Scc = 0.5 (Spp + Spn + Snp + Snn)

Zdiff is referenced to Rp + Rn, which is 100 Ω when both ports are 50 Ω. Zcommon is referenced to half of that. The Display Z0 box still renormalizes the single-ended network, and the differential curves use twice that value. Touchstone export stays the single-ended matrix. The CSV button above the plot, and File → Export CSV, write every single-ended S and Z, every port’s TDR, Sdd and Scc, the differential TDR, and the port voltages and currents. The file keeps the simulated port impedance.

Ports at opposite ends of a trace are a through path. Pairing those two ports does not measure the differential impedance of a coupled pair.

The sheet-copper miss and the 0.2 mm lattice both move Zdiff. The odd mode depends on the facing sidewalls, which the sheets leave out, and a gap under 0.2 mm becomes one cell.

### Mesh

Cells / feature defaults to 2. Every copper edge snaps to a 0.2 mm lattice. A trace or a gap thinner than that becomes one cell, so width and spacing can each be off by about 0.1 mm. Set cells / feature to 4 for a 0.1 mm lattice. The grid does not go below 0.05 mm.

That width error moves impedance on its own. A pair whose gap is under 0.2 mm is a poor fit for differential work until the lattice is finer.

Z uses up to 4 cells per dielectric, but not finer than 0.05 mm. A 0.1 mm prepreg is two cells of 0.05 mm. The core keeps 4 cells when they are coarser than that floor. Air above and below the board is graded out to a tenth of a wavelength.

Pour outlines are metal. Their vertices do not each become a grid line. The pour's bounding box does.

Vias are solid cylinders of the drill diameter, plus the annular copper already on the layers. Plating thickness is not a tube wall.

Solder mask, silk, fab, courtyard, and 3D shapes are not meshed.

### Region and runtime

Crop is the default. With no rectangle and no checked nets, the whole outline is used. Checked nets are simulated, and the reference net is always kept. With none checked, every net in the region is kept.

The timestep is set by the smallest cell in the whole mesh. Auto threads start the timestep loop on every core. The default step cap is 1,000,000. The run stops earlier when the energy in the volume falls by the energy limit (default 1e-4).

One simulation is one port excited. N ports means N runs. S[i, k] is the reflected wave at port i divided by the incident wave at port k.

### Speed and accuracy

The timestep loop scales as cells × timesteps × ports, divided by the cores it is actually using. Building the operator happens before the first step and grows with the same cell count. The smallest cell sets the timestep, so a finer grid costs twice: more cells, and more steps to cover the same nanoseconds. **Preview mesh** prints the cell count and a rough memory figure (about 100 bytes per cell) before a run. A run that is already going keeps the mesh and the thread count it started with.

These are the controls that change that cost, and what they do to the result. The copper-thickness miss in the table above is still there at every setting below. A finer lattice does not put the foil sidewalls back.

| Setting | Default | Effect on the solve | Effect on the result |
| --- | --- | --- | --- |
| Crop, or whole outline | Crop | The mesh is the rectangle plus an air border. Whole outline is the whole board. | Copper and dielectric outside the rectangle are left out. A tight crop around a short trace is the largest speed cut. |
| Margin | 2 mm | Grows the rectangle on every side, clipped to the outline. | Keeps ground and dielectric around the trace. A margin of 0 mm can end the reference plane at the trace and move both Z and the reflections. The absorbing boundary sits further out than this margin; the mesher adds that air on its own. |
| Checked nets | none | Unchecked copper is omitted, so its edges are not grid lines. The reference net is kept either way. | A neighbor or a return path you unchecked is absent. With nothing checked, every net in the region is in the solve. |
| Reference net | GND when the board has one | Changes which pour is kept with the checked nets. | One-click ports run down to this net. The wrong plane is a different return path. |
| Cells / feature | 2 | Sets the copper lattice. See the table under this one. Open space stays at a tenth of a wavelength. | Trace width and gap snap to that lattice. The error is up to about half a pitch. |
| f max | 5 GHz | Coarsens open space and the air box when lowered. The copper lattice stays put. A thin prepreg is already at the 0.05 mm z floor, so it does not get coarser. | The source is a Gaussian whose −20 dB frequency is f max, and the plots stop there. The TDR edge scales as 1 / f max. On the ohms plot the 10–90% rise is about 0.23 ns at 5 GHz, about 20 mm on the distance axis at εeff 2.75. The v(t) Gaussian, integrated, is a step of about 0.17 ns at 5 GHz. Doubling f max halves both. There is no S above f max. Loss tangent is matched at f max / 2, so this also picks the frequency where dielectric loss is right. |
| Frequency points | 401 | Applied after the timestep loop, when the recorded voltages are turned into S. A few hundred versus a few thousand is a small part of the run. | Spacing is f max / (frequency points). The TDR time record is about (frequency points) / f max long: 401 points at 5 GHz is about 80 ns. Fewer points shorten that record and hide a long line. The rise time follows f max. Frequency points do not add detail the timestep loop never computed. The first plotted frequency is one step above DC; the TDR extrapolates to DC. |
| Energy limit | 1e-4 | Stop when the energy left in the volume has fallen by this factor. A larger number (1e-3, 1e-2) stops sooner. A smaller one keeps going. | The early TDR edge is already in the record. The tail is what you cut: late reflections, a TDR that has not settled, and low-frequency S, which needs a long record. 1e-3 is a reasonable first look. Use 1e-4 or tighter when that tail is the result you want. |
| Max timesteps | 1,000,000 | Hard cap on steps. It does not make a step faster. A run that already stopped on the energy limit does not get faster if you lower this. | The cap is a step count. A finer mesh has a shorter timestep, so the same cap covers fewer nanoseconds. Hitting the cap truncates the record the same way a loose energy limit does, and the plot says the run did not decay. Raise it only when a run is hitting it with energy still in the box. |
| Threads | auto (every core) | Passed into the timestep loop. The loop often levels off around 4–8 cores; more cores then add little. | Same mesh and the same excitation. The ohms do not move. |
| How many ports | — | One full timestep run per port. A differential pair is computed from those runs and adds none. | You only get S, Z, and TDR for the ports you placed. |
| Port R | 50 Ω | No mesh change. | The termination at that port. A trace that is not 50 Ω reflects. This is separate from the trace impedance. |
| Stackup thickness, εr, tanδ | from the board | εr sets the wavelength, so a higher εr makes open space slightly finer. Dielectric thickness can change the z count: a slab uses up to 4 cells, and never finer than 0.05 mm. A 0.1 mm prepreg is two cells. Copper thickness is stored and is not meshed. | εr and dielectric height are the impedance. tanδ becomes a constant conductivity matched at f max / 2. More loss makes the energy limit arrive sooner and damps later reflections. It is dielectric loss, not conductor loss. |
| Display Z0 | first port's R | Redraws the plots from the saved S matrix. | Renormalizes the single-ended curves. Differential curves use twice this value. |
| εeff | from the top dielectric | Redraws the TDR distance axis. | Constant-velocity scale only. It moves the millimeter axis and leaves ohms and S alone. |

Copper lattice for **Cells / feature**. The pitch is 0.4 mm divided by that count, and it never goes below 0.05 mm.

| Cells / feature | Lattice | Practical effect |
| --- | --- | --- |
| 1 | 0.4 mm | Fastest copper grid. Width and gap can each be off by about 0.2 mm. |
| 2 | 0.2 mm | Default. About 0.1 mm of width or gap error. A gap under 0.2 mm is one cell. |
| 4 | 0.1 mm | About four times the cells on the copper, and about twice as many timesteps, in the refined part of the mesh. Open space stays coarse, so a small crop slows by less than that. |
| 8 and above | 0.05 mm | The floor. Higher counts do not refine further. |

A first look that should finish sooner: crop to the traces, leave the margin at 2 mm, check only those nets, place two ports, keep cells / feature at 1 or 2, set f max to the highest frequency you will read, and set the energy limit to 1e-3. Preview the mesh and read the cell count before Run. Move to cells / feature 4 and an energy limit of 1e-4 when that preview is a size you can stand and you need the width and the tail.

These are fixed in the mesher and are not on the panel. Open space is 10 cells per wavelength. The grid grows by at most 1.4× from one cell to the next. Each dielectric gets up to 4 cells. The boundary is a PML of 8 cells on all six sides, with 2 extra air cells. The air border around the crop is that PML plus the cells spent grading from the copper pitch out to the wavelength cell.

## Boards

KiCad 6–9 `.kicad_pcb`, and KiCad 10 files whose nets are names only. ODB++ job folder (or a zip of one) and IPC-2581 XML. Native Altium `.PcbDoc` and Xpedition `.pcb` are not read; export ODB++ or IPC-2581 from those tools.

Coordinates stay in the KiCad frame: +X right, +Y down, matching the KiCad canvas.

## Not in this version

Finite copper thickness and roughness, conductor loss, solder mask, an odd-mode source, editing copper, RLC or IBIS models, eye diagrams, field dumps, far field, and a 3D view. Differential impedance is derived from the single-ended S matrix, as described above.

## Debug

Nothing reads the window from outside. A running solve is the worker process plus the files it writes. The **Solver log** dock at the bottom of the window is that process’s stdout and stderr. The status bar only echoes a few of the `PCB_SIM` lines.

The worker prints one JSON object per event, flushed as it happens:

```
PCB_SIM {"event": "start", "cells": [106, 106, 50], "ports": 1}
PCB_SIM {"event": "threads", "count": 16}
PCB_SIM {"event": "excite", "index": 0, "count": 1, "port": "P1"}
PCB_SIM {"event": "geometry", "metals": 1200, "vias": 8, "dielectric": 2}
PCB_SIM {"event": "operator", "elapsed_s": 0, "cells": 561800}
PCB_SIM {"event": "operator", "elapsed_s": 10, "cells": 561800}
PCB_SIM {"event": "done", "results": "...", "decayed": true}
```

`threads` is the core count passed into the timestep loop. `cells` is nx, ny, nz. The mesh build also prints `PCB_PREP` lines while it crops, counts cells, and tessellates. The log shows those as plain sentences as they happen.

After the geometry is in, openEMS builds the FDTD operator by walking every cell. It prints `Multithreaded operator using N threads` and then nothing until that walk finishes. The app adds an elapsed-time line every 10 seconds until `openEMS_run_stats.txt` appears, which is when the timestep loop has started. The timestep, speed, and energy lines then show up in the same dock. The dock keeps the last 2000 lines, and unused-polygon warnings can push the engine lines out of view.

The job folder is `.sim\current` next to the board file (for an ODB++ folder, next to that folder). A new run deletes it and writes it again. While a run is open:

| Path | What it tells you |
| --- | --- |
| `.sim\current\job.json` | Mesh, port R, f max, step cap, `threads` (`null` means auto) |
| `.sim\current\exc_0\` | First port. `exc_1` is the second port, and so on |
| `.sim\current\exc_0\openEMS_run_stats.txt` | Live timestep log. A header and no data rows means the loop has not recorded a step yet |
| `.sim\current\exc_0\openEMS_stats.txt` | Written when that port’s excitation stops. Iteration count below the step cap means the energy limit stopped it |
| `.sim\current\results.npz` | The solved S, Z, TDR, and waveforms. The `.pcbsim.json` file does not contain them. Opening that board loads this file. A launch with no path starts empty |

`LastWriteTime` on `openEMS_run_stats.txt` is the last time the timestep loop flushed. The file appears after the operator walk, when the timestep loop starts. A file that stays on the header means that loop has not flushed a data row yet.

From PowerShell, the GUI and the worker are separate processes. Sample the worker twice. Cores are CPU-seconds divided by wall-clock seconds. About 1.0 means the timestep loop is on one core.

```
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*pcb_simulator*' } |
  Select-Object ProcessId, CommandLine

$p = Get-Process -Id <worker-pid>
$cpu = $p.CPU; $t = Get-Date
Start-Sleep -Seconds 8
$p.Refresh()
'{0:N2} cores, {1:N0} MB' -f (($p.CPU - $cpu) / ((Get-Date) - $t).TotalSeconds), ($p.WorkingSet64 / 1MB)
```

The worker command line ends in `.sim\current`. Closing the app, or Cancel, kills it. A solve that is already running keeps the mesh and thread count it started with.
