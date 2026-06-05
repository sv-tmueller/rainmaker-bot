from scipy.stats import norm

from rainmaker.polymarket.markets import Bucket, BucketKind
from rainmaker.probability.distribution import Gaussian


def bucket_probability(g: Gaussian, bucket: Bucket) -> float:
    """P(settled value falls in this bucket), continuity-corrected.

    Settlement rounds to whole degrees F, so bucket "A-B" captures true temperatures
    in [A-0.5, B+0.5); "X or below" is (-inf, X+0.5]; "Y or higher" is [Y-0.5, +inf).
    """

    def cdf(x: float) -> float:
        return float(norm.cdf(x, loc=g.mu, scale=g.sigma))

    if bucket.kind == "below":
        # parse_bucket guarantees a "below" bucket always has a threshold set.
        assert bucket.threshold is not None
        return cdf(bucket.threshold + 0.5)
    if bucket.kind == "above":
        assert bucket.threshold is not None
        return 1.0 - cdf(bucket.threshold - 0.5)
    # parse_bucket guarantees a "range" bucket always has lo and hi set.
    assert bucket.lo is not None and bucket.hi is not None
    return cdf(bucket.hi + 0.5) - cdf(bucket.lo - 0.5)


def settles(
    kind: BucketKind,
    lo: int | None,
    hi: int | None,
    threshold: int | None,
    actual_value: float,
) -> bool:
    """Whether a settled actual lands in the parsed bucket.

    Settlement rounds to whole degrees F (Python round, half-to-even), then
    compares. Single source of truth shared by live tracking and the backtest.
    """
    v = round(actual_value)
    if kind == "below":
        assert threshold is not None
        return v <= threshold
    if kind == "above":
        assert threshold is not None
        return v >= threshold
    assert lo is not None and hi is not None
    return lo <= v <= hi
