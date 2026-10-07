"""Prometheus exporter for market prices: USD/PLN, BTC and gold.

Each source is polled on its own interval and its prices are exposed as
gauges on /metrics. Alerting (big moves, level breaks, stale data) is done
in Prometheus rules, not here: this process only fetches and exposes.

Sources:
  yahoo      USD/PLN and gold futures (GC=F, ~10 min delayed), unofficial API
  coingecko  BTC in USD and PLN, cached ~1-2 min on the free tier
  nbp        official NBP fixing for USD/PLN and gold (PLN per gram), once a
             business day around 12:00 Europe/Warsaw
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from prometheus_client import Counter, Gauge, start_http_server

log = logging.getLogger("market-exporter")

HTTP_TIMEOUT = 10
# Yahoo rejects requests that do not look like a browser.
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
WARSAW = ZoneInfo("Europe/Warsaw")

LABELS = ["asset", "quote", "unit", "source"]

PRICE = Gauge("asset_price", "Last known price of the asset in the quote currency.", LABELS)
PRICE_TS = Gauge(
    "asset_price_timestamp_seconds",
    "Unix time at which the source produced the price (not when it was scraped).",
    LABELS,
)
FETCH_ERRORS = Counter("exporter_fetch_errors_total", "Failed fetches per source.", ["source"])
LAST_SUCCESS = Gauge(
    "exporter_last_success_timestamp_seconds", "Unix time of the last successful fetch.", ["source"]
)

# (asset, quote, unit, yahoo symbol)
YAHOO_SYMBOLS = [
    ("USD", "PLN", "unit", "USDPLN=X"),
    ("XAU", "USD", "ounce", "GC=F"),
]


# --- parsers: pure functions, raw JSON in -> list of samples out -----------
# A sample is (labels dict, price, source timestamp).


def parse_yahoo(data, asset, quote, unit):
    meta = data["chart"]["result"][0]["meta"]
    labels = {"asset": asset, "quote": quote, "unit": unit, "source": "yahoo"}
    return [(labels, float(meta["regularMarketPrice"]), float(meta["regularMarketTime"]))]


def parse_coingecko(data):
    btc = data["bitcoin"]
    ts = float(btc["last_updated_at"])
    return [
        ({"asset": "BTC", "quote": q.upper(), "unit": "coin", "source": "coingecko"}, float(btc[q]), ts)
        for q in ("usd", "pln")
    ]


def _nbp_ts(day):
    # NBP publishes only a date; the fixing is announced around 12:00 Warsaw time.
    return datetime.fromisoformat(day).replace(hour=12, tzinfo=WARSAW).timestamp()


def parse_nbp_rate(data):
    rate = data["rates"][-1]
    labels = {"asset": "USD", "quote": "PLN", "unit": "unit", "source": "nbp"}
    return [(labels, float(rate["mid"]), _nbp_ts(rate["effectiveDate"]))]


def parse_nbp_gold(data):
    last = data[-1]
    labels = {"asset": "XAU", "quote": "PLN", "unit": "gram", "source": "nbp"}
    return [(labels, float(last["cena"]), _nbp_ts(last["data"]))]


# --- fetchers ---------------------------------------------------------------


def _get(url, **kwargs):
    resp = requests.get(url, timeout=HTTP_TIMEOUT, **kwargs)
    resp.raise_for_status()
    return resp.json()


def fetch_yahoo():
    samples = []
    for asset, quote, unit, symbol in YAHOO_SYMBOLS:
        data = _get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"interval": "1m", "range": "1d"},
            headers=YAHOO_HEADERS,
        )
        samples += parse_yahoo(data, asset, quote, unit)
    return samples


def fetch_coingecko():
    data = _get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "bitcoin", "vs_currencies": "usd,pln", "include_last_updated_at": "true"},
    )
    return parse_coingecko(data)


def fetch_nbp():
    rate = _get("https://api.nbp.pl/api/exchangerates/rates/a/usd/", params={"format": "json"})
    gold = _get("https://api.nbp.pl/api/cenyzlota/", params={"format": "json"})
    return parse_nbp_rate(rate) + parse_nbp_gold(gold)


def env_int(name, default):
    return int(os.environ.get(name, default))


SOURCES = {
    "yahoo": (fetch_yahoo, env_int("YAHOO_INTERVAL_SECONDS", 60)),
    "coingecko": (fetch_coingecko, env_int("COINGECKO_INTERVAL_SECONDS", 120)),
    "nbp": (fetch_nbp, env_int("NBP_INTERVAL_SECONDS", 3600)),
}


def poll(source, fetch):
    """Fetch one source and update gauges. On failure the old values stay,
    so staleness shows up in asset_price_timestamp_seconds, not as a gap."""
    try:
        samples = fetch()
    except Exception as exc:  # any failure of one source must not stop the others
        FETCH_ERRORS.labels(source=source).inc()
        log.warning("fetch failed source=%s error=%r", source, exc)
        return
    for labels, price, ts in samples:
        PRICE.labels(**labels).set(price)
        PRICE_TS.labels(**labels).set(ts)
        log.info("price %s/%s source=%s value=%s", labels["asset"], labels["quote"], source, price)
    LAST_SUCCESS.labels(source=source).set(time.time())


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    port = env_int("PORT", 8000)
    start_http_server(port)
    log.info("listening on :%d, intervals=%s", port, {s: i for s, (_, i) in SOURCES.items()})

    # Create the error counters at 0 so rate() works before the first failure.
    for source in SOURCES:
        FETCH_ERRORS.labels(source=source)

    running = True

    def stop(signum, _frame):
        nonlocal running
        log.info("received signal %d, shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    next_run = {source: 0.0 for source in SOURCES}
    while running:
        now = time.monotonic()
        for source, (fetch, interval) in SOURCES.items():
            if now >= next_run[source]:
                poll(source, fetch)
                next_run[source] = now + interval
        time.sleep(1)


if __name__ == "__main__":
    main()
