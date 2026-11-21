"""The Djordjevic–Sarkar fit keeps tanδ near the stackup value across the band."""

import math

import pytest

from pcb_simulator.sim.djordjevic import F_MEAS_HZ, debye_fit, dielectric_fields, permittivity


def _tand(eps: complex) -> float:
    return -eps.imag / eps.real


def test_ten_gigahertz_point_is_the_stackup_value():
    fields = dielectric_fields(4.4, 0.02)
    poles = [(item["delta"], item["tau"]) for item in fields["debye"]]
    at_meas = permittivity(fields["eps_inf"], poles, F_MEAS_HZ)
    assert at_meas.real == pytest.approx(4.4, rel=1e-6)
    assert _tand(at_meas) == pytest.approx(0.02, rel=0.02)
    assert fields["kappa"] == 0.0
    assert fields["f1_hz"] == 1e6
    assert fields["f2_hz"] == 200e9
    # Inside the corners the loss stays near the datasheet number. The old
    # constant-conductivity match at 40 GHz was about 4 here.
    low = permittivity(fields["eps_inf"], poles, 200e6)
    assert _tand(low) == pytest.approx(0.02, rel=0.15)
    assert _tand(low) < 0.05


def test_octave_example_point_matches_at_its_measurement_frequency():
    eps_inf, poles = debye_fit(4.2, 0.02, f_meas_hz=1e9, f1_hz=1e6, f2_hz=200e9)
    at_meas = permittivity(eps_inf, poles, 1e9)
    assert at_meas.real == pytest.approx(4.2, rel=1e-6)
    # The imaginary part is matched on the pole samples, so tanδ at the
    # stated frequency lands within a few percent of the typed value.
    assert _tand(at_meas) == pytest.approx(0.02, rel=0.05)
    # One pole per decade from 1 MHz to 200 GHz is seven terms.
    decades = math.log10(200e9 / 1e6)
    assert len(poles) == math.ceil(decades) + 1
    assert all(delta > 0 and tau > 0 for delta, tau in poles)


def test_debye_updater_is_recognized_by_its_extension_name():
    from pcb_simulator.sim.worker import engine_supports_debye

    mark = "Debye Dispersive Material Extension"
    assert engine_supports_debye(mark.encode("utf-8"))
    assert engine_supports_debye(mark.encode("utf-16le"))
    assert not engine_supports_debye(b"Drude/Lorentz Dispersive Material Extension")


def test_engine_search_includes_the_windows_dll(monkeypatch):
    from pcb_simulator.sim.worker import _engine_files

    monkeypatch.setenv("OPENEMS_INSTALL_PATH", r"C:\openEMS-test")
    monkeypatch.delenv("CSXCAD_INSTALL_PATH", raising=False)
    assert any(path.name == "openEMS.dll" for path in _engine_files())


def test_blank_or_zero_loss_tangent_stays_a_constant_permittivity():
    assert "debye" not in dielectric_fields(4.5, None)
    assert "debye" not in dielectric_fields(4.5, 0)
    assert dielectric_fields(None, None)["eps_r"] == 4.5
    with pytest.raises(ValueError):
        dielectric_fields(4.4, -0.01)
