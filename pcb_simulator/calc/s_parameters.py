"""Scattering matrix from the lumped-port waves.

S[i, k] is the wave leaving port i when port k is driven. The other ports
stay terminated at their port R. openEMS ``CalcPort`` fills ``uf_inc`` on the
driven port and ``uf_ref`` on every port.

S[i, k] = uf_ref[i] / uf_inc[k].
"""

from __future__ import annotations

import numpy as np


def s_column(uf_ref, uf_inc) -> np.ndarray:
    """One driven port. Shape is ``(n_freq, n_ports)``, ready for ``S[:, :, k]``.

    A sample whose incident magnitude is under 1e-18 is NaN.
    """
    incident = np.asarray(uf_inc, dtype=complex).reshape(-1)
    column = np.full((incident.size, len(uf_ref)), np.nan, dtype=complex)
    good = np.abs(incident) >= 1e-18
    if not np.any(good):
        return column
    for index, reflected in enumerate(uf_ref):
        values = np.asarray(reflected, dtype=complex).reshape(-1)
        column[good, index] = values[good] / incident[good]
    return column


def curve_label(i: int, j: int, names: list[str]) -> str:
    """Sij in words. Sii is the reflection at port i. Sij leaves i when j is driven."""
    into = names[i] if i < len(names) else f"P{i + 1}"
    driven = names[j] if j < len(names) else f"P{j + 1}"
    if i == j:
        return f"S{i + 1}{j + 1}  {into} reflection"
    return f"S{i + 1}{j + 1}  {into} from {driven}"
