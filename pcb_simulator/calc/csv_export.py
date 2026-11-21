"""Wide CSV of every calculated result.

Frequency block, then TDR, then one waveform block per excitation. Formulas
live in the other calc modules. This file only writes their arrays. The
network keeps the simulated port impedance.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from .s_parameters import curve_label
from .tdr import tdr_impedance
from .z_parameters import z_matrix


def write_csv(path: str | Path, ntwk, port_names: list[str], pairs: list[dict] | None = None, excitations: list[dict] | None = None) -> None:
    path = Path(path)
    freq = np.asarray(ntwk.f, dtype=float)
    s = np.asarray(ntwk.s)
    z = z_matrix(ntwk)
    names = [str(name) for name in port_names]
    n = s.shape[1]
    pairs = list(pairs or [])
    header = ["freq_Hz"]
    note = [""]
    for i in range(n):
        for j in range(n):
            header += [f"S{i + 1}{j + 1}_real", f"S{i + 1}{j + 1}_imag"]
            label = curve_label(i, j, names)
            note += [label, label]
    for i in range(n):
        for j in range(n):
            header += [f"Z{i + 1}{j + 1}_real", f"Z{i + 1}{j + 1}_imag"]
            label = curve_label(i, j, names).replace("S", "Z", 1)
            note += [label, label]
    for pair in pairs:
        header += [f"Sdd_{pair['name']}_real", f"Sdd_{pair['name']}_imag", f"Scc_{pair['name']}_real", f"Scc_{pair['name']}_imag"]
        note += [f"Sdd {pair['name']}", f"Sdd {pair['name']}", f"Scc {pair['name']}", f"Scc {pair['name']}"]
    tdrs = [tdr_impedance(ntwk, index) for index in range(n)]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frequency"])
        writer.writerow(header)
        writer.writerow(note)
        for k in range(len(freq)):
            row: list = [freq[k]]
            for i in range(n):
                for j in range(n):
                    row += [s[k, i, j].real, s[k, i, j].imag]
            for i in range(n):
                for j in range(n):
                    row += [z[k, i, j].real, z[k, i, j].imag]
            for pair in pairs:
                row += [pair["sdd"][k].real, pair["sdd"][k].imag, pair["scc"][k].real, pair["scc"][k].imag]
            writer.writerow(row)
        writer.writerow([])
        time_s = tdrs[0][0]
        tdr_header = ["time_s"] + [f"TDR_{names[i]}_ohm" for i in range(n)]
        same = [
            pair
            for pair in pairs
            if len(pair["t_diff"]) == len(time_s)
            and len(pair["z_diff"]) == len(time_s)
            and len(pair["z_common"]) == len(time_s)
        ]
        for pair in same:
            tdr_header += [f"Zdiff_{pair['name']}_ohm", f"Zcommon_{pair['name']}_ohm"]
        writer.writerow(["tdr"])
        writer.writerow(tdr_header)
        for k in range(len(time_s)):
            row = [time_s[k]] + [tdrs[i][1][k] for i in range(n)]
            for pair in same:
                row += [pair["z_diff"][k], pair["z_common"][k]]
            writer.writerow(row)
        for pair in pairs:
            if any(pair is item for item in same):
                continue
            writer.writerow([])
            writer.writerow([f"tdr {pair['name']}"])
            writer.writerow(["time_s", f"Zdiff_{pair['name']}_ohm", f"Zcommon_{pair['name']}_ohm"])
            for t, zd, zc in zip(pair["t_diff"], pair["z_diff"], pair["z_common"]):
                writer.writerow([t, zd, zc])
        for index, sample in enumerate(excitations or []):
            driven = names[index] if index < len(names) else f"P{index + 1}"
            t = np.asarray(sample["t"], dtype=float).reshape(-1)
            voltage = np.asarray(sample["v"], dtype=float)
            current = np.asarray(sample["i"], dtype=float)
            if voltage.ndim == 1:
                voltage = voltage.reshape(1, -1)
            if current.ndim == 1:
                current = current.reshape(1, -1)
            writer.writerow([])
            writer.writerow([f"excitation {driven}"])
            columns = ["time_s"]
            for port in range(voltage.shape[0]):
                observed = names[port] if port < len(names) else f"P{port + 1}"
                columns += [f"v_{observed}", f"i_{observed}"]
            writer.writerow(columns)
            for step in range(t.size):
                row = [t[step]]
                for port in range(voltage.shape[0]):
                    row.append(voltage[port, step] if step < voltage.shape[1] else "")
                    row.append(current[port, step] if step < current.shape[1] else "")
                writer.writerow(row)
