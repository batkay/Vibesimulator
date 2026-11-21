"""Differential and common quantities from two single-ended ports.

Sdd = 0.5 (Spp − Spn − Snp + Snn). Scc uses the plus signs. Both columns of
S must share one reference impedance. When the two port R values differ, both
ports move to their average first. Zdiff is referenced to Rp + Rn. Zcommon is
referenced to half of that. Zdiff and Zcommon in time come from the TDR of
those one-port networks.
"""

from __future__ import annotations

import numpy as np

from .network import network, renormalize
from .tdr import tdr_impedance


def differential_s(S, positive: int, negative: int) -> tuple[np.ndarray, np.ndarray]:
    """Sdd and Scc of two single-ended ports at one end."""
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
