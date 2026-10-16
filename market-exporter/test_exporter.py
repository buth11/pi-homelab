"""Parser tests on real responses captured on 2026-10-07."""

import time
from datetime import date

from exporter import LevelTracker, parse_coingecko, parse_nbp_gold, parse_nbp_rate, parse_yahoo

YAHOO_USDPLN = {
    "chart": {"result": [{"meta": {"symbol": "USDPLN=X", "regularMarketPrice": 3.8925,
                                   "regularMarketTime": 1791350250, "currency": "PLN"}}]}
}
COINGECKO = {"bitcoin": {"usd": 84160, "pln": 327629, "last_updated_at": 1791350020}}
NBP_RATE = {"table": "A", "code": "USD",
            "rates": [{"no": "194/A/NBP/2026", "effectiveDate": "2026-10-06", "mid": 3.8901}]}
NBP_GOLD = [{"data": "2026-10-06", "cena": 523.96}]


def test_yahoo():
    [(labels, price, ts)] = parse_yahoo(YAHOO_USDPLN, "USD", "PLN", "unit")
    assert labels == {"asset": "USD", "quote": "PLN", "unit": "unit", "source": "yahoo"}
    assert price == 3.8925
    assert ts == 1791350250


def test_coingecko_returns_usd_and_pln():
    samples = parse_coingecko(COINGECKO)
    assert {s[0]["quote"]: s[1] for s in samples} == {"USD": 84160.0, "PLN": 327629.0}
    assert all(s[2] == 1791350020 for s in samples)


def test_nbp_timestamp_is_start_of_day_warsaw():
    [(labels, price, ts)] = parse_nbp_rate(NBP_RATE)
    assert labels["source"] == "nbp" and price == 3.8901
    assert ts == 1791237600  # 2026-10-06 00:00 CEST = 2026-10-05 22:00 UTC


def test_nbp_timestamp_never_in_future():
    # Regression: today's gold price is published in the morning; a noon
    # timestamp made its age negative in Prometheus (2026-10-07).
    today = date.today().isoformat()
    [(_, _, ts)] = parse_nbp_gold([{"data": today, "cena": 519.24}])
    assert ts <= time.time()


def test_nbp_gold_is_pln_per_gram():
    [(labels, price, _)] = parse_nbp_gold(NBP_GOLD)
    assert labels == {"asset": "XAU", "quote": "PLN", "unit": "gram", "source": "nbp"}
    assert price == 523.96


def test_tracker_first_price_is_only_a_baseline():
    assert LevelTracker(1000).update(84900) is None


def test_tracker_reports_a_level_once_while_price_dances_around_it():
    t = LevelTracker(1000)
    events = [t.update(p) for p in (84900, 85100, 84950, 85050, 84980, 85200)]
    assert events == [None, (85000, "up"), None, None, None, None]


def test_tracker_reports_a_different_level_in_either_direction():
    t = LevelTracker(1000)
    events = [t.update(p) for p in (84900, 85100, 84950, 83900, 84100, 85100)]
    assert events == [None, (85000, "up"), None, (84000, "down"), None, (85000, "up")]


def test_tracker_big_jump_reports_the_furthest_level():
    t = LevelTracker(1000)
    assert [t.update(p) for p in (84900, 87100)] == [None, (87000, "up")]


def test_tracker_float_edge_case_usdpln():
    t = LevelTracker(0.05)
    assert [t.update(p) for p in (4.12, 4.08)] == [None, (4.1, "down")]
