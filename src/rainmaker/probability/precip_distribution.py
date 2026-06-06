from pydantic import BaseModel, ConfigDict


class Gamma(BaseModel):
    """A gamma distribution for a monthly precipitation total (inches).

    shape k and scale, or a degenerate bone-dry spike at 0 when the forecast
    mean is non-positive (the one-sided analogue of the MIN_SIGMA_F guard).
    """

    model_config = ConfigDict(frozen=True)

    k: float
    scale: float
    degenerate: bool = False


def fit_gamma(mean: float, var: float, *, floor: float) -> Gamma:
    """Method-of-moments gamma: k = mean^2/var, scale = var/mean.

    `var` is floored at `floor`. If `mean <= 0` the month is forecast bone-dry;
    return a degenerate distribution (all mass at 0) rather than dividing by zero.
    """
    v = max(var, floor)
    if mean <= 0:
        return Gamma(k=1.0, scale=v, degenerate=True)
    return Gamma(k=mean * mean / v, scale=v / mean)
