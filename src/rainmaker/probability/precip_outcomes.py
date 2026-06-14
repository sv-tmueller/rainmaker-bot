from scipy.stats import gamma as _gamma

from rainmaker.domain import SETTLEMENT_DECIMALS, BucketKind, PrecipBracket
from rainmaker.probability.precip_distribution import Gamma


def bracket_probability(g: Gamma, bracket: PrecipBracket) -> float:
    """P(monthly total in this inch bracket) via the gamma CDF.

    Half-open [lo, hi): low tail [0, lo), interior [lo, hi), high tail [hi, inf).
    A degenerate (bone-dry) gamma puts all mass in the lowest bracket.
    """

    def cdf(x: float) -> float:
        if g.degenerate:
            return 1.0 if x > 0 else 0.0
        return float(_gamma.cdf(x, a=g.k, scale=g.scale))

    if bracket.kind == "below":
        assert bracket.threshold is not None
        return cdf(bracket.threshold)
    if bracket.kind == "above":
        assert bracket.threshold is not None
        return 1.0 - cdf(bracket.threshold)
    assert bracket.lo is not None and bracket.hi is not None
    return cdf(bracket.hi) - cdf(bracket.lo)


def precip_settles(
    kind: BucketKind,
    lo: float | None,
    hi: float | None,
    threshold: float | None,
    actual_value: float,
) -> bool:
    """Whether the settled monthly total lands in this bracket. A boundary value
    resolves to the higher bracket (half-open intervals encode the round-up)."""
    v = round(actual_value, SETTLEMENT_DECIMALS)
    if kind == "below":
        assert threshold is not None
        return v < threshold
    if kind == "above":
        assert threshold is not None
        return v >= threshold
    assert lo is not None and hi is not None
    return lo <= v < hi
