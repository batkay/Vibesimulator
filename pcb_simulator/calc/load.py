"""Read a finished ``results.npz``.

Arrays are ``freq``, ``S``, ``z0``, ``port_names``, ``decayed``, and ``t_k``,
``v_k``, ``i_k`` for each excitation. This file does not compute them.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


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
