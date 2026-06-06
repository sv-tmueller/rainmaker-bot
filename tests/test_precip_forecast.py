import pytest

from rainmaker.forecasts.precip import monthly_total_moments


def test_moments_sum_observed_forecast_climatology():
    m, v = monthly_total_moments(
        observed_total=1.20,
        forecast_daily=[[0.1, 0.2, 0.0], [0.0, 0.0, 0.1]],
        clim_daily_mean=0.12,
        clim_daily_var=0.04,
        n_tail_days=10,
        floor=0.01,
    )
    assert m == pytest.approx(1.20 + 0.10 + (0.1 / 3) + 1.20, abs=1e-6)
    assert v > 10 * 0.04


def test_early_month_is_wider_than_late_month():
    _, early_v = monthly_total_moments(
        observed_total=0.0,
        forecast_daily=[[0.1, 0.1]],
        clim_daily_mean=0.12,
        clim_daily_var=0.05,
        n_tail_days=28,
        floor=0.01,
    )
    _, late_v = monthly_total_moments(
        observed_total=3.0,
        forecast_daily=[[0.05, 0.05]],
        clim_daily_mean=0.12,
        clim_daily_var=0.05,
        n_tail_days=0,
        floor=0.01,
    )
    assert early_v > late_v


def test_var_floor_applied():
    _, v = monthly_total_moments(
        observed_total=3.0,
        forecast_daily=[],
        clim_daily_mean=0.0,
        clim_daily_var=0.0,
        n_tail_days=0,
        floor=0.01,
    )
    assert v == pytest.approx(0.01)
