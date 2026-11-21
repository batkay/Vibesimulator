"""Load impedance from a finished PDN field run.

The worker writes S for the load ports and for any inductor that was kept out
of the mesh. Z at a load is that port with the other load ports open. An
inductor port is then closed through its R and L.
"""

from __future__ import annotations

import math

import numpy as np

from ..calc.network import network
from ..calc.z_parameters import z_matrix


def reduce_impedance(impedance, keep: list[int], terminations: dict[int, np.ndarray]) -> np.ndarray:
    """Z of the kept ports after the terminated ports are closed.

    ``impedance`` has shape ``(n_freq, n, n)`` and is the open-circuit matrix.
    A termination is the impedance connected across that port, so its voltage
    is minus that impedance times the port current. A port that is neither
    kept nor terminated stays open.
    """
    data = np.asarray(impedance, dtype=complex)
    n_freq = data.shape[0]
    reduced = np.full((n_freq, len(keep), len(keep)), np.nan, dtype=complex)
    closing = list(terminations)
    if not closing:
        for row, i in enumerate(keep):
            for col, j in enumerate(keep):
                reduced[:, row, col] = data[:, i, j]
        return reduced
    for index in range(n_freq):
        block = data[index]
        aa = block[np.ix_(keep, keep)]
        ab = block[np.ix_(keep, closing)]
        ba = block[np.ix_(closing, keep)]
        bb = block[np.ix_(closing, closing)].copy()
        for column, port in enumerate(closing):
            bb[column, column] += complex(terminations[port][index])
        try:
            correction = ab @ np.linalg.solve(bb, ba)
        except np.linalg.LinAlgError:
            continue
        reduced[index] = aa - correction
    return reduced


def field_impedance(freq_hz, s_matrix, z0, names: list[str], load_names: list[str], stamps: list[dict]) -> dict[str, np.ndarray]:
    """Complex Z at each load. ``stamps`` are ``{name, r_ohm, l_h}`` ports to close."""
    freq = np.asarray(freq_hz, dtype=float)
    empty = {name: np.full(freq.shape, np.nan, dtype=complex) for name in load_names}
    if freq.size == 0 or not load_names:
        return empty
    indexed = {str(name): index for index, name in enumerate(names)}
    keep = [indexed[name] for name in load_names if name in indexed]
    if len(keep) != len(load_names):
        return empty
    omega = 2 * math.pi * freq
    closing: dict[int, np.ndarray] = {}
    for stamp in stamps:
        port = indexed.get(str(stamp.get("name", "")))
        if port is None:
            continue
        resistance = float(stamp.get("r_ohm") or 0.0)
        inductance = float(stamp.get("l_h") or 0.0)
        closing[port] = resistance + 1j * omega * inductance
    try:
        impedance = z_matrix(network(freq, s_matrix, z0))
    except Exception:
        return empty
    reduced = reduce_impedance(impedance, keep, closing)
    return {name: reduced[:, index, index] for index, name in enumerate(load_names)}
