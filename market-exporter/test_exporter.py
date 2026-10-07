"""Parser tests on real responses captured on 2026-10-07."""

from exporter import parse_coingecko, parse_nbp_gold, parse_nbp_rate, parse_yahoo

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


def test_nbp_rate_timestamp_is_noon_warsaw():
    [(labels, price, ts)] = parse_nbp_rate(NBP_RATE)
    assert labels["source"] == "nbp" and price == 3.8901
    assert ts == 1791280800  # 2026-10-06 12:00 CEST = 10:00 UTC


def test_nbp_gold_is_pln_per_gram():
    [(labels, price, _)] = parse_nbp_gold(NBP_GOLD)
    assert labels == {"asset": "XAU", "quote": "PLN", "unit": "gram", "source": "nbp"}
    assert price == 523.96
