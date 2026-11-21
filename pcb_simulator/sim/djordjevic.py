"""Djordjevic–Sarkar dielectric as a multi-term Debye fit.

openEMS Python accepts a Debye material and does not ship the fitter. This is
that fit: one (frequency, εr, tanδ) point, poles placed logarithmically from
the low corner to the high corner, and the imaginary part matched there. The
stackup loss tangent is the value at 10 GHz. The corners are 1 MHz and 200 GHz,
one pole per decade, and the DC conductivity is 0. A blank or 0 stays a
constant εr.

The algebra follows CSXCAD's Octave helper CalcDjordjevicSarkarApprox, v1.0
(Tobias Ammann, 2025-06-30), which is the wideband model in Djordjevic et al.,
IEEE Trans. Electromagn. Compat., 2001.
"""

from __future__ import annotations

import math

import numpy as np

# The Octave helper's ε0. DC conductivity is 0, so this value does not move the fit.
EPS0 = 8.8541878128e-12
F_MEAS_HZ = 10e9
F1_HZ = 1e6
F2_HZ = 200e9
N_TERMS_PER_DEC = 1


def dielectric_fields(eps_r: float | None, loss_tangent: float | None) -> dict:
    """Job fields for one dielectric slab.

    ``eps_r`` is the stackup value, read at 10 GHz when tanδ is set. ``kappa``
    stays 0. Poles are present only when tanδ is above 0.
    """
    eps = 4.5 if eps_r is None else float(eps_r)
    if eps <= 0:
        raise ValueError("Relative permittivity must be positive.")
    tand = 0.0 if loss_tangent is None else float(loss_tangent)
    if tand < 0:
        raise ValueError("Loss tangent must be zero or positive.")
    if tand == 0.0:
        return {"eps_r": eps, "kappa": 0.0, "loss_tangent": 0.0}
    eps_inf, poles = debye_fit(eps, tand)
    return {
        "eps_r": eps,
        "kappa": 0.0,
        "loss_tangent": tand,
        "eps_inf": eps_inf,
        "f_meas_hz": F_MEAS_HZ,
        "f1_hz": F1_HZ,
        "f2_hz": F2_HZ,
        "debye": [{"delta": delta, "tau": tau} for delta, tau in poles],
    }


def debye_fit(
    eps_r: float,
    loss_tangent: float,
    f_meas_hz: float = F_MEAS_HZ,
    f1_hz: float = F1_HZ,
    f2_hz: float = F2_HZ,
    n_terms_per_dec: float = N_TERMS_PER_DEC,
    sigma_dc: float = 0.0,
) -> tuple[float, list[tuple[float, float]]]:
    """Return ``(eps_inf, [(Δε, τ), ...])`` for one measurement point.

    ``τ`` is the relaxation time in seconds, ``1 / ω`` of that pole.
    """
    if eps_r <= 0 or f_meas_hz <= 0 or f1_hz <= 0 or f2_hz <= f1_hz:
        raise ValueError("Permittivity and the fit frequencies must be positive, with f2 above f1.")
    if loss_tangent < 0 or sigma_dc < 0 or n_terms_per_dec < 1:
        raise ValueError("Loss tangent and DC conductivity must be zero or positive.")
    if not f1_hz < f_meas_hz < f2_hz:
        raise ValueError("The measurement frequency must sit between the two corners.")

    m1 = math.log10(f1_hz * 2.0 * math.pi)
    m2 = math.log10(f2_hz * 2.0 * math.pi)
    w1 = 10.0 ** m1
    w2 = 10.0 ** m2
    w_meas = 2.0 * math.pi * f_meas_hz
    ratio = (w2 + 1j * w_meas) / (w1 + 1j * w_meas)
    k = math.log(10.0) * (-loss_tangent * eps_r - sigma_dc / (EPS0 * w_meas))
    k = k / float(np.angle(ratio))
    eps_inf_s = eps_r - k * math.log10(abs(ratio))
    delta_s = k * (m2 - m1)

    n_terms = math.ceil((m2 - m1) * n_terms_per_dec) + 1
    mi = np.linspace(m1, m2, n_terms)
    wi = 10.0 ** mi
    step = (math.log10(float(wi[1])) - math.log10(float(wi[0]))) / 4.0
    w_fit = wi * (10.0 ** step)
    span = wi[None, :] / w_fit[:, None] + w_fit[:, None] / wi[None, :]
    samples = _sarkar(w_fit, eps_inf_s, delta_s, m2 - m1, w1, w2, sigma_dc)
    delta = np.linalg.solve(1.0 / span, -np.imag(samples))

    real_sum = float(np.sum(delta / (1.0 + (w_meas ** 2) / (wi ** 2))))
    measured = _sarkar(np.array([w_meas]), eps_inf_s, delta_s, m2 - m1, w1, w2, sigma_dc)[0]
    eps_inf = float(np.real(measured) - real_sum)
    if not math.isfinite(eps_inf) or eps_inf <= 0:
        raise ValueError("The Debye fit produced a non-positive high-frequency permittivity.")
    return eps_inf, [(float(value), float(1.0 / omega)) for value, omega in zip(delta, wi)]


def permittivity(
    eps_inf: float,
    poles: list[tuple[float, float]],
    freq_hz: float,
    sigma_dc: float = 0.0,
) -> complex:
    """Complex relative permittivity of a fitted Debye model at one frequency."""
    omega = 2.0 * math.pi * freq_hz
    value = complex(eps_inf)
    for delta, tau in poles:
        value += delta / (1.0 + 1j * omega * tau)
    if sigma_dc:
        value -= 1j * sigma_dc / (omega * EPS0)
    return value


def _sarkar(omega, eps_inf: float, delta: float, decades: float, w1: float, w2: float, sigma_dc: float):
    return (
        eps_inf
        + delta / decades * np.log10((w2 + 1j * omega) / (w1 + 1j * omega))
        - 1j * sigma_dc / (omega * EPS0)
    )
