"""Pinned regression tests for #291 (Student-t for TMIN, family core).

Captured against the pre-#291 code (exact float repr, not pytest.approx) so any
future change to apply_calibration or bucket_probability that moves a TMAX
output even slightly is caught. TMAX never reaches the new Student-t path (the
family map defaults every variable except TMIN to Gaussian, and pre-refit
calibration rows have df=None regardless of variable), so these numbers must
never change as a result of #291 or any later family-core work.

Do not "fix" a failure here by updating the expected value: a change means the
Gaussian path moved, which is the one thing #291 promises not to do.
"""

from rainmaker.domain import Bucket
from rainmaker.probability.calibration import Calibration, apply_calibration
from rainmaker.probability.distribution import Gaussian
from rainmaker.probability.outcomes import bucket_probability

RAW = Gaussian(mu=70.3, sigma=2.7)


def _cal(n_samples: int) -> Calibration:
    return Calibration(
        station="KLGA",
        variable="TMAX",
        lead_time=1,
        bias=1.1,
        var_a=0.8,
        var_b=1.6,
        n_samples=n_samples,
    )


def _bucket(kind: str, *, lo=None, hi=None, threshold=None) -> Bucket:
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


def test_pinned_apply_calibration_full_regime():
    out, state = apply_calibration(
        RAW, _cal(50), min_sigma=1.5, min_samples=30, min_bias_samples=10
    )
    assert state == "full"
    assert out.mu == 69.2
    assert out.sigma == 3.5304390661785967
    assert out.df is None


def test_pinned_apply_calibration_bias_only_regime():
    out, state = apply_calibration(
        RAW, _cal(15), min_sigma=1.5, min_samples=30, min_bias_samples=10
    )
    assert state == "bias_only"
    assert out.mu == 69.2
    assert out.sigma == 3.375
    assert out.df is None


def test_pinned_apply_calibration_uncalibrated_regime():
    out, state = apply_calibration(RAW, _cal(5), min_sigma=1.5, min_samples=30, min_bias_samples=10)
    assert state == "uncalibrated"
    assert out.mu == 70.3
    assert out.sigma == 3.375
    assert out.df is None


def test_pinned_apply_calibration_none_falls_back():
    out, state = apply_calibration(RAW, None, min_sigma=1.5, min_samples=30, min_bias_samples=10)
    assert state == "uncalibrated"
    assert out.mu == 70.3
    assert out.sigma == 3.375
    assert out.df is None


def test_pinned_bucket_probability_range():
    out, _ = apply_calibration(RAW, _cal(50), min_sigma=1.5, min_samples=30, min_bias_samples=10)
    assert bucket_probability(out, _bucket("range", lo=69, hi=71)) == 0.32121599172192744


def test_pinned_bucket_probability_below():
    out, _ = apply_calibration(RAW, _cal(50), min_sigma=1.5, min_samples=30, min_bias_samples=10)
    assert bucket_probability(out, _bucket("below", threshold=65)) == 0.14731278765365236


def test_pinned_bucket_probability_above():
    out, _ = apply_calibration(RAW, _cal(50), min_sigma=1.5, min_samples=30, min_bias_samples=10)
    assert bucket_probability(out, _bucket("above", threshold=75)) == 0.06664807959447971
