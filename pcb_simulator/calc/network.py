"""Saved S wrapped as a scikit-rf Network.

Display Z0 uses ``renormalize`` and does not rerun the solver. Touchstone
keeps the port impedance that was simulated.
"""

from __future__ import annotations

import numpy as np


def network(freq_hz, S, z0):
    import skrf as rf

    freq = rf.Frequency.from_f(np.asarray(freq_hz, dtype=float), unit="Hz")
    z0_arr = np.asarray(z0, dtype=float)
    return rf.Network(frequency=freq, s=np.asarray(S, dtype=complex), z0=z0_arr)


def renormalize(ntwk, z0):
    """Return a copy normalized to a new scalar reference impedance."""
    changed = ntwk.copy()
    changed.renormalize(z0)
    return changed
