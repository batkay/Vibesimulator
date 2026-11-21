import numpy as np
import pytest
import skrf as rf

from pcb_simulator.project import DiffPair
from pcb_simulator.calc.csv_export import write_csv
from pcb_simulator.calc.differential import differential_export, differential_s, mixed_mode_networks
from pcb_simulator.calc.network import network
from pcb_simulator.calc.s_parameters import s_column
from pcb_simulator.calc.tdr import tdr_impedance, tdr_ohm_limits, tdr_view_window


def test_s_column_is_reflected_over_incident():
    incident = np.array([2 + 0j, 1e-20, 4 + 0j])
    reflected = [
        np.array([1 + 0j, 3 + 0j, 8 + 0j]),
        np.array([0 + 1j, 5 + 0j, 0 + 4j]),
    ]
    column = s_column(reflected, incident)
    assert column.shape == (3, 2)
    assert column[0, 0] == pytest.approx(0.5)
    assert column[0, 1] == pytest.approx(0.5j)
    assert np.isnan(column[1, 0])
    assert np.isnan(column[1, 1])
    assert column[2, 0] == pytest.approx(2)
    assert column[2, 1] == pytest.approx(1j)


def test_s_to_z_roundtrip_and_flat_tdr():
    freq = np.linspace(1e6, 5e9, 401)
    phase = np.exp(-1j * 2 * np.pi * freq * 100e-12)
    S = np.zeros((len(freq), 2, 2), dtype=complex)
    S[:, 0, 1] = phase
    S[:, 1, 0] = phase
    ntwk = network(freq, S, [50.0, 50.0])
    rebuilt = rf.Network(frequency=ntwk.frequency, z=ntwk.z, z0=50)
    assert np.allclose(ntwk.s, rebuilt.s, atol=1e-6)
    _t, z = tdr_impedance(ntwk, port=0)
    mid = z[len(z) // 4 : 3 * len(z) // 4]
    assert mid.size > 10
    assert np.allclose(mid, 50.0, atol=2.5)


def test_tdr_view_window_frames_the_step():
    dt = 1e-10
    time_s = np.arange(-50, 51) * dt
    impedance = np.full(time_s.shape, 50.0)
    impedance[(time_s >= 0.0) & (time_s < 0.4e-9)] = 70.0
    t0, t1 = tdr_view_window(time_s, impedance)
    assert t0 <= 0.0 < t1
    assert t1 > 0.4e-9
    assert t1 < 2e-9

    flat0, flat1 = tdr_view_window(time_s, np.full(time_s.shape, 50.0))
    assert flat0 <= 0.0
    assert flat1 < time_s[-1] / 2


def test_tdr_view_window_keeps_the_trace_after_a_large_launch():
    dt = 6.25e-12
    time_s = np.arange(-40, 800) * dt
    impedance = np.full(time_s.shape, 2.0)
    impedance[(time_s >= 0.0) & (time_s < 40e-12)] = 45.0
    impedance[(time_s >= 40e-12) & (time_s < 140e-12)] = 8.0
    t0, t1 = tdr_view_window(time_s, impedance)
    assert t0 <= 0.0
    assert t1 > 140e-12
    assert t1 < 1e-9


def test_tdr_ohm_limits_keep_the_line_and_let_the_open_leave():
    impedance = np.concatenate([np.full(80, 100.0), np.linspace(100.0, 20000.0, 20)])
    lo, hi = tdr_ohm_limits(impedance)
    assert lo < 100.0 < hi
    assert hi < 400.0
    assert hi < float(np.max(impedance))
    low = np.concatenate([np.full(80, 20.0), np.full(20, 8000.0)])
    low_lo, low_hi = tdr_ohm_limits(low)
    assert low_lo < 20.0 < low_hi < 80.0
    high = np.concatenate([np.full(80, 250.0), np.full(20, 20000.0)])
    high_lo, high_hi = tdr_ohm_limits(high)
    assert high_lo < 250.0 < high_hi < 800.0


def test_tdr_view_window_stops_after_the_trace_when_the_tail_wanders():
    dt = 6.25e-12
    time_s = np.arange(-40, 800) * dt
    impedance = np.full(time_s.shape, 50.0)
    trace = (time_s >= 0.0) & (time_s < 0.30e-9)
    impedance[trace] = 50.0 + np.sin(2 * np.pi * time_s[trace] / 0.05e-9)
    step = (time_s >= 0.30e-9) & (time_s < 0.42e-9)
    impedance[step] = 90.0
    wander = time_s >= 0.42e-9
    impedance[wander] = 70.0 + 40.0 * np.sin(2 * np.pi * (time_s[wander] - 0.42e-9) / 0.35e-9)
    impedance[wander] += 25.0 * (time_s[wander] - 0.42e-9) / 1e-9
    t0, t1 = tdr_view_window(time_s, impedance)
    assert t0 <= 0.0
    assert t1 > 0.42e-9
    assert t1 < 0.6e-9


def test_differential_match_and_open():
    freq = np.linspace(1e6, 5e9, 401)
    matched = np.zeros((len(freq), 2, 2), dtype=complex)
    sdd, scc = differential_s(matched, 0, 1)
    assert np.allclose(sdd, 0)
    assert np.allclose(scc, 0)
    ntwk = network(freq, matched, [50.0, 50.0])
    diff, common = mixed_mode_networks(ntwk, 0, 1)
    assert float(np.real(diff.z0.reshape(-1)[0])) == pytest.approx(100.0)
    assert float(np.real(common.z0.reshape(-1)[0])) == pytest.approx(50.0)
    _time, impedance = tdr_impedance(diff, 0)
    mid = impedance[len(impedance) // 4 : 3 * len(impedance) // 4]
    assert np.allclose(mid, 100.0, atol=5.0)

    opened = np.zeros((8, 2, 2), dtype=complex)
    opened[:, 0, 0] = 1
    opened[:, 1, 1] = 1
    sdd, scc = differential_s(opened, 0, 1)
    assert np.allclose(sdd, 1)
    assert np.allclose(scc, 1)

    sample = np.zeros((4, 2, 2), dtype=complex)
    sample[:, 0, 0] = 0.1
    sample[:, 1, 1] = 0.2
    sample[:, 0, 1] = 0.3
    sample[:, 1, 0] = 0.4
    sdd, scc = differential_s(sample, 0, 1)
    assert np.allclose(sdd, -0.2)
    assert np.allclose(scc, 0.5)


def test_differential_csv_rows(tmp_path):
    freq = np.linspace(1e6, 1e9, 16)
    s = np.zeros((len(freq), 2, 2), dtype=complex)
    ntwk = network(freq, s, [50.0, 50.0])
    rows = differential_export(ntwk, [DiffPair("D1", "P1", "P2")], ["P1", "P2"])
    path = tmp_path / "results.csv"
    excitations = [{"t": np.linspace(0, 1e-9, 8), "v": np.ones((2, 8)), "i": np.zeros((2, 8))}]
    write_csv(path, ntwk, ["P1", "P2"], rows, excitations)
    text = path.read_text(encoding="utf-8")
    assert "S11_real" in text
    assert "S21_real" in text
    assert "Z22_real" in text
    assert "Sdd_D1_real" in text
    assert "Scc_D1_real" in text
    assert "TDR_P1_ohm" in text
    assert "TDR_P2_ohm" in text
    assert "Zdiff_D1_ohm" in text
    assert "Zcommon_D1_ohm" in text
    assert "excitation P1" in text
    assert "v_P2" in text
