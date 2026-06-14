import pytest

from rainmaker.backtest import (
    BacktestResult,
    DayScore,
    ReliabilityBin,
    aggregate,
    combine,
    reliability_bins,
    score_day,
    standard_buckets,
)
from rainmaker.polymarket.markets import parse_bucket_label
from rainmaker.probability.distribution import Gaussian
from rainmaker.probability.outcomes import bucket_probability


def test_standard_buckets_partition_sums_to_one():
    buckets = standard_buckets(70.0)
    g = Gaussian(mu=71.3, sigma=4.0)
    total = sum(bucket_probability(g, b) for b in buckets)
    assert total == pytest.approx(1.0, abs=1e-9)


def test_standard_buckets_shape_and_center():
    buckets = standard_buckets(70.0, span=10)
    assert len(buckets) == 12  # below tail + 10 ranges + above tail
    assert buckets[0].kind == "below" and buckets[0].threshold == 59
    assert buckets[-1].kind == "above" and buckets[-1].threshold == 80
    # a range bucket contains the center
    assert any(b.kind == "range" and b.lo <= 70 <= b.hi for b in buckets)


def test_standard_buckets_labels_round_trip():
    for b in standard_buckets(85.0):
        kind, lo, hi, threshold = parse_bucket_label(b.label)
        assert (kind, lo, hi, threshold) == (b.kind, b.lo, b.hi, b.threshold)


def test_score_day_hit_is_well_covered():
    g = Gaussian(mu=70.0, sigma=2.0)
    score = score_day(g, standard_buckets(70.0), actual=70.0)
    assert score.modal_won is True
    # actual at the mean: cdf = 0.5, inside every central interval
    assert score.coverage == {0.5: True, 0.8: True, 0.9: True}


def test_score_day_one_sigma_coverage():
    g = Gaussian(mu=70.0, sigma=2.0)
    score = score_day(g, standard_buckets(70.0), actual=72.0)  # +1 sigma
    # cdf(+1 sigma) ~= 0.841, |0.841-0.5| = 0.341: outside 50%, inside 80%/90%
    assert score.coverage == {0.5: False, 0.8: True, 0.9: True}


def test_score_day_far_miss_loses_modal():
    g = Gaussian(mu=70.0, sigma=2.0)
    score = score_day(g, standard_buckets(70.0), actual=64.0)
    assert score.modal_won is False
    assert score.coverage[0.9] is False


def test_reliability_bins_group_and_frequency():
    pairs = [(0.05, False), (0.15, True), (0.95, True), (0.92, False)]
    bins = reliability_bins(pairs)
    by_lo = {b.lo: b for b in bins}
    assert set(by_lo) == {0.0, 0.1, 0.9}
    assert by_lo[0.0].count == 1 and by_lo[0.0].observed_freq == 0.0
    assert by_lo[0.1].count == 1 and by_lo[0.1].observed_freq == 1.0
    assert by_lo[0.9].count == 2
    assert by_lo[0.9].observed_freq == pytest.approx(0.5)
    assert by_lo[0.9].predicted_mean == pytest.approx((0.95 + 0.92) / 2)


def test_aggregate_rolls_up_metrics():
    day1 = DayScore(
        modal_p=0.6,
        modal_won=True,
        brier=0.2,
        coverage={0.5: True, 0.8: True, 0.9: True},
        pairs=[(0.6, True), (0.4, False)],
    )
    day2 = DayScore(
        modal_p=0.5,
        modal_won=False,
        brier=0.5,
        coverage={0.5: False, 0.8: True, 0.9: True},
        pairs=[(0.5, False), (0.5, True)],
    )
    res = aggregate([day1, day2])
    assert res.n == 2
    assert res.modal_hit_rate == pytest.approx(0.5)
    assert res.mean_modal_p == pytest.approx(0.55)
    assert res.mean_brier == pytest.approx(0.35)
    assert res.coverage == {
        0.5: pytest.approx(0.5),
        0.8: pytest.approx(1.0),
        0.9: pytest.approx(1.0),
    }


def test_aggregate_empty_raises():
    with pytest.raises(ValueError):
        aggregate([])


def _result(n: int, *, bin_lo: float, predicted: float, observed: float, bin_n: int):
    return BacktestResult(
        n=n,
        modal_hit_rate=0.5,
        mean_modal_p=0.6,
        mean_brier=0.2,
        coverage={0.5: 0.4, 0.8: 0.7, 0.9: 0.8},
        reliability=[
            ReliabilityBin(
                lo=bin_lo,
                hi=bin_lo + 0.1,
                predicted_mean=predicted,
                observed_freq=observed,
                count=bin_n,
            )
        ],
    )


def test_combine_n_weights_metrics_and_merges_bins():
    r1 = _result(10, bin_lo=0.5, predicted=0.52, observed=0.6, bin_n=10)
    r2 = _result(30, bin_lo=0.5, predicted=0.56, observed=0.4, bin_n=30)
    out = combine([r1, r2])
    assert out.n == 40
    # all per-result metrics are equal here, so the n-weighted mean is unchanged
    assert out.modal_hit_rate == pytest.approx(0.5)
    assert out.mean_modal_p == pytest.approx(0.6)
    assert out.mean_brier == pytest.approx(0.2)
    assert out.coverage[0.5] == pytest.approx(0.4)
    assert out.coverage[0.8] == pytest.approx(0.7)
    assert out.coverage[0.9] == pytest.approx(0.8)
    # the two bins at the same lo merge into one, count-weighted
    assert len(out.reliability) == 1
    merged = out.reliability[0]
    assert merged.lo == 0.5 and merged.count == 40
    assert merged.predicted_mean == pytest.approx((0.52 * 10 + 0.56 * 30) / 40)
    assert merged.observed_freq == pytest.approx((0.6 * 10 + 0.4 * 30) / 40)


def test_combine_empty_raises():
    with pytest.raises(ValueError):
        combine([])
