"""Recorded lumped-port voltage and current.

v(t) is ``ut_tot`` and i(t) is ``it_tot`` from the excitation. These are the
port totals, not the TDR.
"""

from __future__ import annotations

import numpy as np


def port_waveforms(ports, driven_index: int):
    """Time, voltage, and current for one driven port.

    Voltage and current have shape ``(n_ports, n_time)``. Time is the driven
    port's ``u_time``. A shorter record on a listening port is padded with zeros.
    """
    driven = ports[driven_index]
    time_s = np.asarray(driven.u_time, dtype=float)
    n_time = len(driven.u_time)
    voltage = np.zeros((len(ports), n_time), dtype=float)
    current = np.zeros((len(ports), n_time), dtype=float)
    for index, port in enumerate(ports):
        samples_v = np.asarray(port.ut_tot, dtype=float)
        samples_i = np.asarray(port.it_tot, dtype=float)
        count = min(n_time, len(samples_v), len(samples_i))
        voltage[index, :count] = samples_v[:count]
        current[index, :count] = samples_i[:count]
    return time_s, voltage, current
