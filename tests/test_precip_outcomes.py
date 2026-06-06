import json
from pathlib import Path

import pytest

from rainmaker.polymarket.precip_markets import parse_precip_event
from rainmaker.probability.precip_distribution import fit_gamma
from rainmaker.probability.precip_outcomes import bracket_probability, precip_settles

FIXTURES = Path(__file__).parent / "fixtures"


def _nyc_market():
    return parse_precip_event(
        json.loads((FIXTURES / "polymarket_precip_monthly_nyc.json").read_text())
    )


def test_partition_sums_to_one():
    g = fit_gamma(3.0, 4.0, floor=0.01)
    total = sum(bracket_probability(g, b) for b in _nyc_market().buckets)
    assert total == pytest.approx(1.0, abs=1e-9)


def test_mode_bracket_has_highest_probability():
    g = fit_gamma(2.5, 0.5, floor=0.01)
    probs = {b.label: bracket_probability(g, b) for b in _nyc_market().buckets}
    assert max(probs, key=probs.get) == '2-3"'


def test_open_tails_use_cdf_complement():
    from scipy.stats import gamma as sgamma

    g = fit_gamma(3.0, 4.0, floor=0.01)
    buckets = {b.label: b for b in _nyc_market().buckets}
    cdf = lambda x: float(sgamma.cdf(x, a=g.k, scale=g.scale))  # noqa: E731
    assert bracket_probability(g, buckets['<2"']) == pytest.approx(cdf(2.0))
    assert bracket_probability(g, buckets['>6"']) == pytest.approx(1 - cdf(6.0))


def test_degenerate_dry_puts_mass_in_lowest_bracket():
    g = fit_gamma(0.0, 0.5, floor=0.01)
    buckets = {b.label: b for b in _nyc_market().buckets}
    assert bracket_probability(g, buckets['<2"']) == pytest.approx(1.0)
    assert bracket_probability(g, buckets['2-3"']) == pytest.approx(0.0)


def test_precip_settles_round_up_between_brackets():
    assert precip_settles("below", None, None, 2.0, 1.99) is True
    assert precip_settles("below", None, None, 2.0, 2.00) is False
    assert precip_settles("range", 2.0, 3.0, None, 2.00) is True
    assert precip_settles("range", 2.0, 3.0, None, 3.00) is False
    assert precip_settles("above", None, None, 6.0, 6.00) is True
    assert precip_settles("above", None, None, 6.0, 5.99) is False


def test_brackets_tile_so_exactly_one_settles():
    buckets = _nyc_market().buckets
    for actual in (0.0, 1.99, 2.0, 3.5, 6.0, 9.9):
        hits = [b for b in buckets if precip_settles(b.kind, b.lo, b.hi, b.threshold, actual)]
        assert len(hits) == 1
