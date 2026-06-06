import statistics


def monthly_total_moments(
    *,
    observed_total: float,
    forecast_daily: list[list[float]],
    clim_daily_mean: float,
    clim_daily_var: float,
    n_tail_days: int,
    floor: float,
) -> tuple[float, float]:
    """Mean and variance of the monthly total: observed-to-date (deterministic) +
    pooled forecast horizon + climatology tail. Daily precip is treated as
    independent across days (a stated approximation that understates variance)."""
    m_f = sum(statistics.fmean(day) for day in forecast_daily if day)
    v_f = sum(statistics.variance(day) for day in forecast_daily if len(day) >= 2)
    m = observed_total + m_f + n_tail_days * clim_daily_mean
    v = v_f + n_tail_days * clim_daily_var
    return m, max(v, floor)
