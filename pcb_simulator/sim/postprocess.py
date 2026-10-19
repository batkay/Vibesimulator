"""S to Z to TDR, in the GUI process, using scikit-rf.

Changing the displayed reference impedance renormalizes the network. It does
not rerun the field solver. The Touchstone file keeps the z0 that was simulated.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

C0 = 299_792_458.0


def load_results(path: str | Path) -> dict:
    data = np.load(path, allow_pickle=False)
    excitations = []
    k = 0
    while f"t_{k}" in data:
        excitations.append(
            {
                "t": np.asarray(data[f"t_{k}"], dtype=float),
                "v": np.asarray(data[f"v_{k}"], dtype=float),
                "i": np.asarray(data[f"i_{k}"], dtype=float),
            }
        )
        k += 1
    decayed = True
    if "decayed" in data:
        decayed = bool(np.asarray(data["decayed"]).reshape(-1)[0])
    names = [str(name) for name in np.asarray(data["port_names"])]
    return {
        "freq": np.asarray(data["freq"], dtype=float),
        "S": np.asarray(data["S"], dtype=complex),
        "z0": np.asarray(data["z0"], dtype=float),
        "port_names": names,
        "decayed": decayed,
        "excitations": excitations,
    }


def network(freq_hz, S, z0):
    import skrf as rf

    freq = rf.Frequency.from_f(np.asarray(freq_hz, dtype=float), unit="Hz")
    z0_arr = np.asarray(z0, dtype=float)
    return rf.Network(frequency=freq, s=np.asarray(S, dtype=complex), z0=z0_arr)


def renormalize(ntwk, z0):
    """Return a copy normalized to a new scalar reference impedance."""
    import skrf as rf

    changed = ntwk.copy()
    changed.renormalize(z0)
    return changed


def tdr_impedance(ntwk, port: int = 0, window: str = "hamming"):
    """Step-response impedance of S[port, port].

    Returns time in seconds and impedance in ohms. scikit-rf's step response
    is the reflection coefficient of a step; Z = Z0 (1+Γ)/(1-Γ), which is the
    same conversion as ``plot_z_time_step``.
    """
    n2 = ntwk.extrapolate_to_dc(kind="linear")
    one = _single(n2, port)
    time_s, gamma = one.step_response(window=window, squeeze=True)
    gamma = np.asarray(gamma, dtype=float)
    gamma = np.clip(gamma, -0.999, 0.999)
    z0 = float(np.real(np.asarray(one.z0).reshape(-1)[0]))
    return np.asarray(time_s, dtype=float), z0 * (1 + gamma) / (1 - gamma)


def _single(ntwk, port: int):
    """1-port network of S[port, port], including after DC extrapolation."""
    import skrf as rf

    s = np.asarray(ntwk.s[:, port, port], dtype=complex).reshape(-1, 1, 1)
    z0 = float(np.real(np.asarray(ntwk.z0).reshape(-1)[0]))
    return rf.Network(frequency=ntwk.frequency, s=s, z0=z0)


def distance_m(time_s: np.ndarray, eps_eff: float) -> np.ndarray:
    velocity = C0 / math_sqrt(eps_eff)
    return velocity * np.asarray(time_s, dtype=float) / 2


def tdr_view_window(time_s: np.ndarray, impedance: np.ndarray) -> tuple[float, float]:
    """Time limits around the launched step.

    The step response is a two-sided IFFT, and the record is much longer than
    the trace. The window starts just before t = 0 and ends once the impedance
    has settled near its late-time value. Comparing with that level, rather
    than with the size of each step, keeps a later change that follows a large
    launch. A trace that settles a little off the port impedance still counts
    as settled.
    """
    time_s = np.asarray(time_s, dtype=float).reshape(-1)
    impedance = np.asarray(impedance, dtype=float).reshape(-1)
    if time_s.size == 0:
        return 0.0, 0.0
    order = np.argsort(time_s)
    time_s = time_s[order]
    impedance = impedance[order]
    if time_s.size == 1:
        return float(time_s[0]), float(time_s[0])
    dt = abs(float(np.median(np.diff(time_s)))) or 1e-12
    t_min = float(time_s[0])
    t_max = float(time_s[-1])
    lead = min(2.0 * dt, 0.2e-9)
    positive = np.flatnonzero(time_s >= 0.0)
    if positive.size == 0:
        return t_min, t_max
    z_pos = impedance[positive]
    t_pos = time_s[positive]
    swing = float(np.max(z_pos) - np.min(z_pos))
    if swing < 0.5 or t_pos.size < 2:
        return max(t_min, -lead), min(t_max, float(t_pos[0]) + max(1e-9, 20.0 * dt))
    tail_n = max(5, t_pos.size // 7)
    settled = float(np.median(z_pos[-tail_n:]))
    tol = max(1.0, 0.04 * swing)
    departed = np.flatnonzero(np.abs(z_pos - settled) >= tol)
    if departed.size == 0:
        last = min(3, t_pos.size - 1)
    else:
        last = int(departed[-1])
    t_last = float(t_pos[min(last, t_pos.size - 1)])
    span = max(t_last - float(t_pos[0]), dt)
    pad = max(4.0 * dt, 0.15 * span)
    return max(t_min, -lead), min(t_max, t_last + pad)


def math_sqrt(value: float) -> float:
    return float(np.sqrt(value))


def default_eps_eff(eps_r: float) -> float:
    return (eps_r + 1.0) / 2


def write_touchstone(ntwk, path: str | Path) -> None:
    ntwk.write_touchstone(str(path))


def differential_s(S, positive: int, negative: int) -> tuple[np.ndarray, np.ndarray]:
    """Differential and common reflection of two single-ended ports at one end.

    Both columns of ``S`` must already be normalized to the same reference
    impedance. Sdd = 0.5 (Spp − Spn − Snp + Snn). Scc uses the plus signs.
    """
    block = np.asarray(S, dtype=complex)
    spp = block[:, positive, positive]
    snn = block[:, negative, negative]
    spn = block[:, positive, negative]
    snp = block[:, negative, positive]
    sdd = 0.5 * (spp - spn - snp + snn)
    scc = 0.5 * (spp + spn + snp + snn)
    return sdd, scc


def _port_z0(ntwk, index: int) -> float:
    z0 = np.real(np.asarray(ntwk.z0, dtype=complex))
    if z0.ndim == 0:
        return float(z0)
    if z0.ndim == 1:
        return float(z0[index])
    return float(z0.reshape(-1, z0.shape[-1])[0, index])


def mixed_mode_networks(ntwk, positive: int, negative: int):
    """One-port networks for Sdd and Scc.

    The differential reference is the sum of the two port references. When
    those references differ, both ports are first moved to their average so
    the single-ended formula applies, and the sum is still Rp + Rn.
    """
    rp = _port_z0(ntwk, positive)
    rn = _port_z0(ntwk, negative)
    shared = 0.5 * (rp + rn)
    source = ntwk
    if abs(rp - shared) > 1e-6 or abs(rn - shared) > 1e-6:
        source = renormalize(ntwk, shared)
    sdd, scc = differential_s(source.s, positive, negative)
    return network(source.f, sdd.reshape(-1, 1, 1), [rp + rn]), network(source.f, scc.reshape(-1, 1, 1), [shared])


def differential_export(ntwk, pairs, port_names: list[str]) -> list[dict]:
    """Curves for each pair whose ports are both in ``port_names``."""
    rows = []
    for pair in pairs:
        if pair.positive not in port_names or pair.negative not in port_names:
            continue
        positive = port_names.index(pair.positive)
        negative = port_names.index(pair.negative)
        diff, common = mixed_mode_networks(ntwk, positive, negative)
        t_diff, z_diff = tdr_impedance(diff, 0)
        t_common, z_common = tdr_impedance(common, 0)
        rows.append(
            {
                "name": pair.name,
                "sdd": np.asarray(diff.s[:, 0, 0], dtype=complex),
                "scc": np.asarray(common.s[:, 0, 0], dtype=complex),
                "t_diff": t_diff,
                "z_diff": z_diff,
                "t_common": t_common,
                "z_common": z_common,
            }
        )
    return rows


def curve_label(i: int, j: int, names: list[str]) -> str:
    """Sij in words. Sii is the reflection at port i. Sij leaves i when j is driven."""
    into = names[i] if i < len(names) else f"P{i + 1}"
    driven = names[j] if j < len(names) else f"P{j + 1}"
    if i == j:
        return f"S{i + 1}{j + 1}  {into} reflection"
    return f"S{i + 1}{j + 1}  {into} from {driven}"


def write_csv(path: str | Path, ntwk, port_names: list[str], pairs: list[dict] | None = None, excitations: list[dict] | None = None) -> None:
    """Every S, Z, TDR, and port waveform. The network keeps the simulated port impedance."""
    import csv

    path = Path(path)
    freq = np.asarray(ntwk.f, dtype=float)
    s = np.asarray(ntwk.s)
    z = np.asarray(ntwk.z)
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
