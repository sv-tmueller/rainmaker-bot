import pytest

from rainmaker.polymarket.markets import Bucket
from rainmaker.probability.distribution import Gaussian
from rainmaker.probability.outcomes import bucket_probability, settles


def _bucket(kind, lo=None, hi=None, threshold=None) -> Bucket:
    return Bucket(
        label="x",
        kind=kind,
        lo=lo,
        hi=hi,
        threshold=threshold,
        yes_token_id="t",
        best_ask=None,
        best_bid=None,
        yes_price=0.0,
    )


def test_range_probability_continuity_corrected():
    g = Gaussian(mu=70.0, sigma=2.0)
    # [69.5, 71.5): CDF(71.5) - CDF(69.5) for N(70,2)
    p = bucket_probability(g, _bucket("range", lo=70, hi=71))
    assert p == pytest.approx(0.37208, abs=1e-4)


def test_below_and_above_are_complementary_at_shared_edge():
    g = Gaussian(mu=70.0, sigma=2.0)
    # "70 or below" -> CDF(70.5); "71 or higher" -> 1 - CDF(70.5); they share edge 70.5
    p_below = bucket_probability(g, _bucket("below", threshold=70))
    p_above = bucket_probability(g, _bucket("above", threshold=71))
    assert p_below + p_above == pytest.approx(1.0)


def test_full_bucket_partition_sums_to_one():
    g = Gaussian(mu=70.5, sigma=3.0)
    buckets = [
        _bucket("below", threshold=59),
        *[_bucket("range", lo=lo, hi=lo + 1) for lo in range(60, 78, 2)],
        _bucket("above", threshold=78),
    ]
    total = sum(bucket_probability(g, b) for b in buckets)
    assert total == pytest.approx(1.0, abs=1e-9)


def test_mode_bucket_has_highest_probability():
    g = Gaussian(mu=70.5, sigma=2.0)
    buckets = [
        _bucket("below", threshold=59),
        *[_bucket("range", lo=lo, hi=lo + 1) for lo in range(60, 78, 2)],
        _bucket("above", threshold=78),
    ]
    probs = {(b.kind, b.lo, b.hi, b.threshold): bucket_probability(g, b) for b in buckets}
    mode_key = max(probs, key=probs.get)
    assert mode_key == ("range", 70, 71, None)


def test_settles_range_inclusive_both_ends():
    assert settles("range", 70, 71, None, 70.0)
    assert settles("range", 70, 71, None, 71.4)  # rounds to 71
    assert not settles("range", 70, 71, None, 71.6)  # rounds to 72


def test_settles_below_and_above_thresholds():
    assert settles("below", None, None, 59, 59.0)
    assert settles("below", None, None, 59, 58.9)
    assert not settles("below", None, None, 59, 59.6)  # rounds to 60
    assert settles("above", None, None, 78, 78.0)
    assert not settles("above", None, None, 78, 77.4)  # rounds to 77


def test_settles_uses_half_to_even_rounding():
    # Python round() is banker's rounding: 70.5 -> 70 (not 71), 72.5 -> 72.
    assert settles("range", 70, 71, None, 70.5)  # 70.5 -> 70, in [70, 71]
    # half-up would round 70.5 to 71 and win here; half-to-even gives 70 and loses.
    assert not settles("range", 71, 72, None, 70.5)
    assert settles("range", 72, 73, None, 72.5)  # 72.5 -> 72, in [72, 73]

