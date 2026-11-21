"""Impedance matrix from S.

Z is scikit-rf ``s2z`` on this network's S and port impedance (``Network.z``),
using the power-wave definition. A matched port, Sii = 0, has Zii = z0.
"""

from __future__ import annotations

import numpy as np


def z_matrix(ntwk) -> np.ndarray:
    """Impedance parameters, shape ``(n_freq, n_ports, n_ports)``."""
    return np.asarray(ntwk.z)
