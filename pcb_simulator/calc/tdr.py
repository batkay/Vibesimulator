"""Step-response impedance of one reflection.

``extrapolate_to_dc``, then the Hamming step of ``S[port, port]``. That step
is the reflection coefficient Γ. Z = Z0 (1+Γ)/(1−Γ), with Γ clipped to ±0.999.
The distance axis is velocity * t / 2. ``tdr_view_window`` only chooses the
plot limits. A record that ends on a plateau keeps that settled level. A tail
that keeps moving stops after the last flat stretch of the trace.
"""

from __future__ import annotations

import numpy as np

C0 = 299_792_458.0


def tdr_impedance(ntwk, port: int = 0, window: str = "hamming"):
    """Time in seconds and impedance in ohms for ``S[port, port]``."""
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


def tdr_ohm_limits(impedance: np.ndarray) -> tuple[float, float]:
    """Vertical limits that follow the trace. A rail at the end leaves the chart.

    The scale is taken from the first three quarters of the samples, which is
    the line. The open at the far end is allowed to run off the top.
    """
    z = np.asarray(impedance, dtype=float).reshape(-1)
    z = z[np.isfinite(z)]
    if z.size == 0:
        return 0.0, 1.0
    early = z[: max(4, (z.size * 3) // 4)]
    median = float(np.median(early))
    q1, q3 = (float(value) for value in np.percentile(early, [25, 75]))
    spread = max(q3 - q1, 0.0)
    room = max(5.0, 2.0 * spread, 0.4 * abs(median))
    hi = median + room
    peak = float(np.max(early))
    if peak <= max(median + 2.0 * room, 3.0 * max(median, 1.0)):
        hi = max(hi, peak)
    lo = float(np.min(early))
    if hi <= lo:
        hi = lo + 1.0
    pad = max(2.0, 0.1 * (hi - lo))
    return lo - pad, hi + pad


def distance_m(time_s: np.ndarray, eps_eff: float) -> np.ndarray:
    velocity = C0 / float(np.sqrt(eps_eff))
    return velocity * np.asarray(time_s, dtype=float) / 2


def tdr_view_window(time_s: np.ndarray, impedance: np.ndarray) -> tuple[float, float]:
    """Time limits around the launched step.

    The step response is a two-sided IFFT, and the record is much longer than
    the trace. The window starts just before t = 0. When the record ends on a
    plateau, it runs until the impedance has settled near that late-time value.
    Comparing with that level, rather than with the size of each step, keeps a
    later change that follows a large launch. A trace that settles a little off
    the port impedance still counts as settled. When the tail keeps moving, the
    window stops after the last flat stretch of the trace plus a short step.
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
    tol = max(1.0, 0.04 * swing)
    if not _tail_is_settled(z_pos, dt, tol):
        t_end = _end_of_trace(z_pos, t_pos, dt, tol)
        if t_end is not None:
            return max(t_min, -lead), min(t_max, t_end)
    tail_n = max(5, t_pos.size // 7)
    settled = float(np.median(z_pos[-tail_n:]))
    departed = np.flatnonzero(np.abs(z_pos - settled) >= tol)
    if departed.size == 0:
        last = min(3, t_pos.size - 1)
    else:
        last = int(departed[-1])
    t_last = float(t_pos[min(last, t_pos.size - 1)])
    span = max(t_last - float(t_pos[0]), dt)
    pad = max(4.0 * dt, 0.15 * span)
    return max(t_min, -lead), min(t_max, t_last + pad)


def _hold_n(dt: float) -> int:
    """Samples that have to stay still before a stretch counts as flat.

    About 50 ps, and at least four samples. Capped near 80 ps so a 100 ps
    stretch on a fine record still counts.
    """
    hold_time = min(80e-12, max(8.0 * dt, 50e-12))
    return max(4, int(np.ceil(hold_time / max(dt, 1e-15))))


def _flat_runs(impedance: np.ndarray, tol: float, hold_n: int) -> list[tuple[int, int]]:
    """Inclusive ranges where every window of ``hold_n`` samples stays within ``tol``."""
    count = int(impedance.size)
    if hold_n < 2 or count < hold_n:
        return []
    windows = count - hold_n + 1
    flat = np.empty(windows, dtype=bool)
    for index in range(windows):
        chunk = impedance[index : index + hold_n]
        flat[index] = float(np.max(chunk) - np.min(chunk)) <= tol
    runs: list[tuple[int, int]] = []
    index = 0
    while index < windows:
        if not flat[index]:
            index += 1
            continue
        end_window = index
        while end_window < windows and flat[end_window]:
            end_window += 1
        runs.append((index, end_window - 1 + hold_n - 1))
        index = end_window
    return runs


def _tail_is_settled(impedance: np.ndarray, dt: float, tol: float) -> bool:
    """True when the record finishes on a plateau long enough to be the late level."""
    hold_n = _hold_n(dt)
    runs = _flat_runs(impedance, tol, hold_n)
    if not runs:
        return True
    start, end = runs[-1]
    if end < impedance.size - 1:
        return False
    settle_n = max(hold_n, int(np.ceil(0.25e-9 / max(dt, 1e-15))))
    return (end - start + 1) >= settle_n


def _end_of_trace(impedance: np.ndarray, time_s: np.ndarray, dt: float, tol: float) -> float | None:
    """Time just after the last long flat stretch, including one short step off it."""
    hold_n = _hold_n(dt)
    runs = _flat_runs(impedance, tol, hold_n)
    if not runs:
        return None
    longest = max(end - start + 1 for start, end in runs)
    need = max(hold_n, int(np.ceil(0.75 * longest)))
    substantial = [run for run in runs if run[1] - run[0] + 1 >= need]
    _start, end = (substantial or runs)[-1]
    pad = max(4.0 * dt, 0.15e-9)
    return float(time_s[end]) + pad


def default_eps_eff(eps_r: float) -> float:
    return (eps_r + 1.0) / 2
