import pytest

from rainmaker.probability.calibration import (
    Calibration,
    CalibrationPair,
    apply_calibration,
    compute_accuracy,
    fit_calibration,
)
from rainmaker.probability.distribution import Gaussian
from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import load_calibration
from rainmaker.store.record import save_calibration


def _pairs(bias: float, dvals: list[float], sigma: float) -> list[CalibrationPair]:
    # actual = mu - bias + d, with mu varying so fit cannot trivially collapse.
    return [
        CalibrationPair(mu=70.0 + i, sigma=sigma, actual=(70.0 + i) - bias + d)
        for i, d in enumerate(dvals)
    ]


def test_fit_recovers_bias_and_unit_spread():
    # d = +/-2, sigma = 2 -> bias 1, standardized residuals +/-1 -> spread_scale 1
    cal = fit_calibration("KLGA", "TMAX", 1, _pairs(1.0, [2, -2, 2, -2], 2.0))
    assert cal.bias == pytest.approx(1.0)
    assert cal.spread_scale == pytest.approx(1.0)
    assert cal.n_samples == 4


def test_fit_detects_overconfidence():
    # d = +/-4, sigma = 2 -> standardized residuals +/-2 -> spread_scale 2 (2x too tight)
    cal = fit_calibration("KLGA", "TMAX", 1, _pairs(0.0, [4, -4, 4, -4], 2.0))
    assert cal.bias == pytest.approx(0.0)
    assert cal.spread_scale == pytest.approx(2.0)


def test_fit_empty_raises():
    with pytest.raises(ValueError, match="no pairs"):
        fit_calibration("KLGA", "TMAX", 1, [])


def test_apply_corrects_mu_and_sigma():
    g = Gaussian(mu=70.0, sigma=2.0)
    cal = Calibration(
        station="KLGA", variable="TMAX", lead_time=1, bias=1.0, spread_scale=2.0, n_samples=50
    )
    out, calibrated = apply_calibration(g, cal, min_sigma=1.5, min_samples=30)
    assert calibrated is True
    assert out.mu == pytest.approx(69.0)
    assert out.sigma == pytest.approx(4.0)


def test_apply_floors_sigma():
    g = Gaussian(mu=70.0, sigma=2.0)
    cal = Calibration(
        station="KLGA", variable="TMAX", lead_time=1, bias=0.0, spread_scale=0.1, n_samples=50
    )
    out, _ = apply_calibration(g, cal, min_sigma=1.5, min_samples=30)
    assert out.sigma == 1.5  # 2.0 * 0.1 = 0.2 floored to 1.5


def test_apply_falls_back_when_too_few_samples():
    g = Gaussian(mu=70.0, sigma=2.0)
    cal = Calibration(
        station="KLGA", variable="TMAX", lead_time=1, bias=5.0, spread_scale=2.0, n_samples=5
    )
    out, calibrated = apply_calibration(g, cal, min_sigma=1.5, min_samples=30)
    assert calibrated is False
    assert out.mu == 70.0  # bias not applied
    assert out.sigma > 2.0  # widened


def test_apply_none_falls_back():
    out, calibrated = apply_calibration(Gaussian(mu=70.0, sigma=2.0), None, min_sigma=1.5)
    assert calibrated is False
    assert out.mu == 70.0
    assert out.sigma == pytest.approx(2.5)  # 2.0 * UNCALIBRATED_WIDEN 1.25, above min_sigma 1.5


def test_fit_detects_underdispersion():
    # d = +/-0.2, sigma = 2 -> standardized residuals +/-0.1 -> spread_scale 0.1 (too wide)
    cal = fit_calibration("KLGA", "TMAX", 1, _pairs(0.0, [0.2, -0.2, 0.2, -0.2], 2.0))
    assert 0 < cal.spread_scale < 1
    assert cal.spread_scale == pytest.approx(0.1)


def test_fit_perfect_floors_spread_scale():
    # zero residuals would give spread_scale 0; the 1e-6 floor keeps it positive.
    cal = fit_calibration("KLGA", "TMAX", 1, _pairs(0.0, [0, 0, 0, 0], 2.0))
    assert cal.spread_scale == pytest.approx(1e-6)


def test_calibration_save_load_round_trip():
    conn = connect(":memory:")
    init_schema(conn)
    cal = Calibration(
        station="KLGA", variable="TMAX", lead_time=1, bias=1.0, spread_scale=1.3, n_samples=40
    )
    save_calibration(conn, cal, updated_at="2026-05-31T10:00:00Z")
    assert load_calibration(conn, "KLGA", "TMAX", 1) == cal

    # upsert overwrites the same cell
    save_calibration(conn, cal.model_copy(update={"bias": 2.0}), updated_at="2026-05-31T11:00:00Z")
    reloaded = load_calibration(conn, "KLGA", "TMAX", 1)
    assert reloaded is not None and reloaded.bias == 2.0
    assert load_calibration(conn, "KLGA", "TMAX", 2) is None
    conn.close()


def test_compute_accuracy_mae_and_bias():
    pairs = [
        CalibrationPair(mu=70.0, sigma=2.0, actual=68.0),  # error +2
        CalibrationPair(mu=70.0, sigma=2.0, actual=73.0),  # error -3
        CalibrationPair(mu=70.0, sigma=2.0, actual=69.0),  # error +1
    ]
    acc = compute_accuracy(pairs)
    assert acc.n == 3
    assert acc.mae_f == pytest.approx(2.0)  # (2 + 3 + 1) / 3
    assert acc.bias_f == pytest.approx(0.0)  # (2 - 3 + 1) / 3


def test_compute_accuracy_bias_direction():
    # all forecasts overshoot: bias is positive (mu - actual)
    pairs = [
        CalibrationPair(mu=72.0, sigma=2.0, actual=70.0),  # error +2
        CalibrationPair(mu=74.0, sigma=2.0, actual=70.0),  # error +4
    ]
    acc = compute_accuracy(pairs)
    assert acc.bias_f == pytest.approx(3.0)  # mean(+2, +4)
    assert acc.mae_f == pytest.approx(3.0)


def test_compute_accuracy_empty_raises():
    with pytest.raises(ValueError, match="no pairs"):
        compute_accuracy([])
