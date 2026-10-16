# 10 -- Market Exporter: Price Monitoring and Level Alerts

> A small Prometheus exporter that tracks USD/PLN, BTC and gold, plus
> alert rules that push a phone notification (ntfy) when a price crosses a
> round level. Built end-to-end through the same pipeline as everything
> else in this repo: pull request, CI validation, pinned image, `kubectl
> apply` / `helm upgrade`, verification.

| | |
|---|---|
| **Owner** | Bartosz Suszko |
| **Status** | Production (level-crossing alerts with crossing memory). Jump and source-health alerts planned, see [Roadmap](#14-known-limitations-and-roadmap) |
| **Namespace** | `market` |
| **Code** | [`market-exporter/`](../market-exporter/) |
| **Manifests** | [`k8s/market-exporter/`](../k8s/market-exporter/) |
| **Alert routing** | [`helm-values/kube-prometheus-stack.yaml`](../helm-values/kube-prometheus-stack.yaml) (`ntfy-market` receiver) |
| **CI** | [`market-exporter.yml`](../.github/workflows/market-exporter.yml), [`validate.yml`](../.github/workflows/validate.yml) |
| **Delivered in** | PRs #1-#7 (2026-10-07), #9-#10 crossing memory (2026-10-16) |
| **Last reviewed** | 2026-10-16 |

## Contents

1. [Purpose and scope](#1-purpose-and-scope)
2. [Architecture](#2-architecture)
3. [Data sources](#3-data-sources)
4. [Metrics contract](#4-metrics-contract)
5. [Design decisions](#5-design-decisions)
6. [Security](#6-security)
7. [Repository layout](#7-repository-layout)
8. [CI/CD pipeline](#8-cicd-pipeline)
9. [Alerting design](#9-alerting-design)
10. [Deployment procedures](#10-deployment-procedures)
11. [Verification checklist](#11-verification-checklist)
12. [Operations runbook](#12-operations-runbook)
13. [Testing strategy](#13-testing-strategy)
14. [Known limitations and roadmap](#14-known-limitations-and-roadmap)
15. [Lessons learned during the build](#15-lessons-learned-during-the-build)
16. [Change history](#16-change-history)
17. [References](#17-references)

---

## 1. Purpose and scope

**Goal:** get a phone notification when USD/PLN, BTC or gold crosses a
round price level, in either direction, without watching charts.

**In scope (v1):**
- Fetch prices from three public sources and expose them as Prometheus metrics.
- Alert on crossing a price grid: USD/PLN every 0.05 PLN, BTC every
  1,000 USD, gold every 100 USD/oz.
- Notify about a level **once**: re-crossing the last crossed level, in
  either direction, does not notify again until a different level is
  crossed.
- Deliver notifications to a dedicated ntfy topic, separate from
  infrastructure alerts.
- Charts and history for free via the existing Prometheus/Grafana stack.

**Out of scope:**
- Trading, order execution or any write access to an exchange.
- Tick-level real-time data (free sources are 1-10 minutes behind, see §3).
- Investment advice. The alerts are informational.

## 2. Architecture

```
 ┌──────────────┐   HTTPS (poll)   ┌───────────────────────────┐
 │ Yahoo Finance│◄─────────────────┤                           │
 │ CoinGecko    │◄─────────────────┤  market-exporter (Pod)    │
 │ NBP API      │◄─────────────────┤  Python, :8000/metrics    │
 └──────────────┘                  └─────────────┬─────────────┘
                                                 │ Service (ClusterIP) "metrics"
                                                 │ ServiceMonitor, scrape every 60s
                                   ┌─────────────▼─────────────┐
                                   │        Prometheus         │
                                   │  recording rule           │
                                   │  market:asset_price:max   │
                                   │  2 level-crossing alerts  │
                                   └─────────────┬─────────────┘
                                                 │ firing alerts (category=market)
                                   ┌─────────────▼─────────────┐
                                   │       Alertmanager        │
                                   │  route category="market"  │──► ntfy (market topic) ──► phone
                                   │  other routes unchanged   │──► ntfy (cluster topic), healthchecks.io
                                   └───────────────────────────┘
```

| Component | Kind | Location | Responsibility |
|---|---|---|---|
| Exporter | Python process in a Deployment | `market-exporter/exporter.py` | Poll sources, expose gauges. No alerting logic |
| Image | Multi-arch OCI image (amd64 + arm64) | `ghcr.io/buth11/pi-homelab/market-exporter:<sha>` | Built by CI, pinned by commit SHA |
| Service | `ClusterIP` | `k8s/market-exporter/service.yaml` | Stable in-cluster address, named port `metrics` |
| ServiceMonitor | Prometheus Operator CRD | `k8s/market-exporter/servicemonitor.yaml` | Tells Prometheus to scrape the Service |
| PrometheusRule | Prometheus Operator CRD | `k8s/market-exporter/prometheusrule.yaml` | Recording rule + alert rules |
| Alert route | Alertmanager config (Helm values) | `helm-values/kube-prometheus-stack.yaml` | Sends `category=market` to the `ntfy-market` receiver |

Objects are connected **only through labels and selectors**:

```
ServiceMonitor  --selector app=market-exporter-->  Service
Service         --selector app=market-exporter-->  Pod (template labels)
Prometheus      --serviceMonitorSelector / ruleSelector: release=kube-prometheus-stack-->  ServiceMonitor, PrometheusRule
```

A mismatch anywhere in this chain fails silently (no error, just no data),
which is why §11 verifies every link separately.

## 3. Data sources

| Source | Assets | Polled every | Freshness measured on 2026-10-07 | Notes |
|---|---|---|---|---|
| Yahoo Finance (unofficial chart API) | USD/PLN (`USDPLN=X`), gold futures (`GC=F`, USD/oz) | 60 s | USD/PLN: real time while the market is open. Gold: consistently about 10 min behind (delayed exchange data) | Undocumented API: needs a browser `User-Agent`, may change or block without notice. FX is closed at weekends |
| CoinGecko (`/simple/price`) | BTC in USD and PLN | 120 s | About 1-2 min (cached on the free tier) | Aggregate across exchanges. Free tier is rate limited |
| NBP API (`api.nbp.pl`) | USD/PLN (table A mid rate), gold (PLN per gram) | 3600 s | Once per business day. Date only, no time | Official reference data. Gold for "today" can appear in the morning, FX fixing around noon |

Cross-check performed during the build: NBP gold converted to USD/oz
(PLN/g x 31.1035 / USD/PLN) agreed with Yahoo `GC=F` within about 0.6 %,
the difference being the one-day lag of the NBP fixing.

Level-crossing alerts use **Yahoo for USD/PLN and gold** (intraday) and
**CoinGecko for BTC**. NBP series are exposed for reference and charts only.

## 4. Metrics contract

The metrics are the exporter's public interface. Alert rules and
dashboards depend on these names and labels, so changing them is a
breaking change.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `asset_price` | gauge | `asset`, `quote`, `unit`, `source` | Last known price of `asset` in `quote` |
| `asset_price_timestamp_seconds` | gauge | same as above | Unix time at which **the source** produced the price, not when it was scraped |
| `exporter_fetch_errors_total` | counter | `source` | Failed fetches per source. Pre-initialised to 0 so `rate()` works from the start |
| `exporter_last_success_timestamp_seconds` | gauge | `source` | Unix time of the last successful fetch |
| `asset_last_crossed_level` | gauge | `asset`, `quote`, `unit`, `source` | Last price level crossed, either direction. Changes only when a **different** level is crossed. Absent until the first crossing after start |
| `asset_level_crossings_total` | counter | same + `direction` (`up`/`down`) | Crossings of a new level. Pre-initialised to 0 for every tracked series, because `increase()` cannot see the first increment of a series that appears at 1 |

Label values in use:

| `asset` | `quote` | `unit` | `source` |
|---|---|---|---|
| `USD` | `PLN` | `unit` | `yahoo`, `nbp` |
| `XAU` (ISO 4217 code for gold) | `USD` | `ounce` | `yahoo` |
| `XAU` | `PLN` | `gram` | `nbp` |
| `BTC` | `USD`, `PLN` | `coin` | `coingecko` |

Level tracking covers three series: BTC/USD (CoinGecko), USD/PLN (Yahoo)
and XAU/USD (Yahoo). Cardinality is fixed: 6 price series, 3 per-source
series, 3 level gauges and 6 crossing counters. Prices
and timestamps are values, never labels.

Prometheus adds target labels (`pod`, `instance`, `namespace`, `job`,
...). During a rollout the old and the new pod briefly expose the same
series with different `pod` labels; the recording rule
`market:asset_price:max` (§9) removes them.

## 5. Design decisions

Each decision is recorded with the alternative that was rejected.

| # | Decision | Rejected alternative | Reason |
|---|---|---|---|
| D1 | Exporter only exposes metrics; alert logic lives in PromQL rules, **except crossing memory (D17)** | Script that compares prices and calls ntfy itself | Reuses Prometheus storage, Alertmanager deduplication/grouping and Grafana. Rules are reviewable config with unit tests (§13) |
| D2 | One metric `asset_price` with labels | `btc_price`, `usd_price`, `gold_price` | One rule pattern covers every asset; consistent with Prometheus naming practice |
| D3 | Export the source timestamp as its own metric | Rely on the scrape timestamp | Prometheus only knows when *it* read the value. Without the source timestamp, a stuck source looks identical to a stable price |
| D4 | On fetch failure keep the last value | Drop the series | Staleness becomes visible in `asset_price_timestamp_seconds` instead of a gap that is hard to alert on |
| D5 | NBP timestamp = 00:00 Europe/Warsaw of the published date | 12:00 Warsaw (original implementation) | NBP publishes only a date and not always at noon. Noon produced timestamps in the future (see §15, L1) |
| D6 | Image tagged with the full commit SHA, manifest pinned to it | `:latest` | Reproducible deploys, one-line rollback, Git shows exactly what runs |
| D7 | Multi-arch build (amd64 + arm64) via QEMU | Separate images per arch, or arm64 runners | One tag works on Pi (arm64) and N100/VM (amd64). QEMU costs about 12x on `pip install`, mitigated by layer cache |
| D8 | `replicas: 1` | 2+ replicas | Two replicas would double external API calls for identical data. A short gap during rollout is acceptable |
| D9 | Memory limit, CPU request, **no CPU limit** | CPU limit | CPU limits throttle even on idle nodes; this has already slowed another service in this cluster |
| D10 | `ClusterIP` Service | `LoadBalancer` (MetalLB) | Metrics are consumed only inside the cluster |
| D11 | Price **grid** (`floor(price / step)`), computed in the exporter since D17 | One rule per individual level | A grid needs no maintenance as the price moves; two generic rules cover every level of every tracked asset |
| D12 | ~~Debounce: band compared with 15 min ago, plus `for: 5m`~~ **Superseded by D17 on 2026-10-16** | Fire on any band change | The debounce still let a price that oscillated slowly around one level notify every time it re-crossed it (observed for a week). Also cost about 5 min of delay |
| D13 | Custom `severity: notice` + `category: market` | `severity: info` | `info` is routed to `null` and is suppressed by the existing `InfoInhibitor` rule |
| D14 | Dedicated ntfy topic and receiver, `send_resolved: false` | Reuse the cluster topic | Market notifications must not drown infrastructure alerts. Level alerts auto-resolve, so a "resolved" message would be noise |
| D15 | New receiver appended **last** in `receivers:` | Insert it anywhere | Real webhook URLs are injected by list index (`--set-string ...receivers[N]...`); appending keeps existing indices valid |
| D16 | Rule unit tests live in `market-exporter/`, not in `k8s/market-exporter/` | Next to the PrometheusRule | `kubectl apply -f k8s/market-exporter/` would try to apply the test file and fail |
| D17 | **Crossing memory in the exporter** (`LevelTracker`): remember the last crossed level per series and report a crossing only when the level differs, regardless of direction. Rules only react to the counter | Keep it in PromQL (e.g. `last_over_time` over a long window) | The requirement needs state with unbounded memory; PromQL rules only see a time window, and a windowed approximation would be complex and still forget. Trade-offs: state lives in pod memory, so a restart (in practice only a deploy) starts a fresh baseline and can repeat at most one notification; grid steps move from the rules into `LEVEL_STEPS` in code, so changing a step is an image release |

## 6. Security

**Workload hardening** (meets the Kubernetes Pod Security Standards
*restricted* profile):

| Setting | Level | Value |
|---|---|---|
| `runAsNonRoot` / `runAsUser` | Pod | `true` / `10001` (matches `USER 10001` in the Dockerfile) |
| `seccompProfile` | Pod | `RuntimeDefault` |
| `allowPrivilegeEscalation` | Container | `false` |
| `readOnlyRootFilesystem` | Container | `true` (the image sets `PYTHONDONTWRITEBYTECODE=1`, so Python writes no `.pyc`) |
| `capabilities.drop` | Container | `ALL` (port 8000 > 1024 needs no `NET_BIND_SERVICE`) |

**Network exposure:** none outside the cluster. The pod makes outbound
HTTPS calls to the three sources only.

**Secrets:**
- The exporter itself needs no credentials.
- ntfy topic names act as passwords (anyone who knows one can read and
  publish). The market topic name is random, stored in the password
  manager, and never committed. In Git the receiver URL is `REPLACE_ME`,
  supplied only at `helm upgrade` time (repo-wide pattern, see
  [docs/SECURITY.md](../docs/SECURITY.md)).
- During upgrades the URLs are read into shell variables (`read -rs`,
  `helm get values`), never typed into a command line (shell history), and
  any rendered files containing them are deleted afterwards.
- A topic name that has been exposed anywhere (chat, ticket, log) is
  rotated, not just removed.

**Supply chain:** base image `python:3.12-slim` (resolved to a digest by
the builder), dependencies pinned in `requirements*.txt`, image built only
by CI from reviewed code, published to GHCR with the repository's
short-lived `GITHUB_TOKEN`. The publish permission (`packages: write`) is
granted to the build job only.

## 7. Repository layout

```
market-exporter/
├── exporter.py              # application: fetchers, parsers, metrics, main loop
├── test_exporter.py         # pytest: parsers on real captured API responses + regression test
├── alerts.test.yaml         # promtool unit tests for the alert rules
├── Dockerfile               # non-root, read-only-FS friendly image
├── requirements.txt         # runtime deps (pinned)
├── requirements-dev.txt     # pytest, pyyaml (pinned)
├── .dockerignore            # keeps tests out of the image
└── .gitignore

k8s/market-exporter/
├── namespace.yaml
├── deployment.yaml          # pinned image SHA, resources, probes, securityContext
├── service.yaml             # ClusterIP, named port "metrics"
├── servicemonitor.yaml      # label release=kube-prometheus-stack
└── prometheusrule.yaml      # recording rule + 2 generic level alerts

.github/workflows/
├── market-exporter.yml      # test (pytest + promtool) -> build/push image
└── validate.yml             # kubeconform + terraform checks on every PR
```

## 8. CI/CD pipeline

### Workflows

| Workflow | Triggers | Jobs |
|---|---|---|
| `validate.yml` | every PR, push to `main` | `kubeconform -strict` on `k8s/` and `argocd/` (CRDs via the community schema catalog); `terraform fmt/validate` + `tflint` |
| `market-exporter.yml` | PR / push to `main` touching `market-exporter/**`, `k8s/market-exporter/prometheusrule.yaml` or the workflow itself | `test`: pytest, `promtool check rules`, `promtool test rules`. `build` (needs `test`): multi-arch image; **pushed only on `main`** |

Key properties:
- **Least privilege:** workflow default `contents: read`; only `build`
  gets `packages: write`; registry login is skipped on PRs.
- **Build once, deploy many:** a manifest-only change (new image tag) does
  not rebuild the image, because `k8s/market-exporter/deployment.yaml` is
  outside the `paths` filter.
- **Layer cache** (`type=gha`): `requirements.txt` is copied before the
  code, so code-only changes reuse the `pip install` layer (78 s -> 30 s).
- **Required checks:** only `Validate` can be made a required status check.
  `market-exporter.yml` has a `paths` filter; a required check that never
  starts would block every unrelated PR.

### Release process (code change)

```
1. Branch fix/... or feat/...  -> PR  -> market-exporter.yml: tests + build (no push)
2. Merge (squash) to main      -> market-exporter.yml: tests + build + push :<merge-sha>
3. Confirm the image exists anonymously (§11, step 1)
4. Branch chore/market-exporter-bump-<short-sha>: update the image tag in deployment.yaml
5. PR -> Validate (kubeconform) -> merge
6. kubectl apply -f k8s/market-exporter/deployment.yaml ; rollout status ; verify (§11)
```

Step 3 matters: a manifest pointing at a tag that has not been pushed yet
ends in `ImagePullBackOff`.

## 9. Alerting design

### Recording rule

```promql
market:asset_price:max = max without (pod, instance) (asset_price)
```

One series per asset regardless of which pod reported it. All alerts use
it, which keeps them short and avoids duplicate notifications during
rollouts.

The recording rule is not used by the level alerts any more (they react
to the exporter's crossing counter); it is kept for the planned jump
alerts.

### Crossing memory (exporter, `LevelTracker`)

| Series | Step (`LEVEL_STEPS`) |
|---|---|
| BTC/USD, CoinGecko | 1,000 USD |
| USD/PLN, Yahoo | 0.05 PLN |
| XAU/USD, Yahoo | 100 USD/oz |

On every new price:

1. `band = floor(price / step + 1e-6)`. The `1e-6` guards against binary
   floating point: `4.10 / 0.05` is `81.99999999999999`.
2. First price after start: store the band as a baseline, report nothing.
3. Band changed: the crossed level is `band x step` going up, or
   `(band + 1) x step` going down (on a multi-band jump, the furthest
   level). Rounded to 6 decimals (`82 x 0.05 = 4.1000000000000005`).
4. Report only if this level differs from the last reported one; then set
   `asset_last_crossed_level` and increment
   `asset_level_crossings_total{direction}`.

Example, BTC (step 1,000):

```
84 900 -> 85 100   crosses 85 000 up     notify  (last = 85 000)
85 100 -> 84 950   crosses 85 000 down   silent  (same level)
84 950 -> 85 050   crosses 85 000 up     silent  (same level)
85 050 -> 83 900   crosses 84 000 down   notify  (last = 84 000)
83 900 -> 84 100   crosses 84 000 up     silent  (same level)
84 100 -> 85 100   crosses 85 000 up     notify  (differs from 84 000)
```

### Level-crossing alerts

Two generic rules cover every tracked asset:

```promql
# MarketLevelCrossedUp (Down is identical with direction="down")
max without (pod, instance) (asset_last_crossed_level)
  and on (asset, quote, source)
(sum without (pod, instance) (increase(asset_level_crossings_total{direction="up"}[5m])) > 0)
```

- **`and on (asset, quote, source)`** keeps the left-hand value, so
  `$value` is the crossed level. The counter carries an extra
  `direction` label, hence the explicit `on (...)`.
- **No `for:`**: noise is removed by the crossing memory, so the alert
  fires on the next evaluation. End-to-end delay is roughly one poll
  interval plus one scrape (1-3 min); gold adds its ~10 min data delay.
- **`increase(...[5m]) > 0`**: the alert is active for about 5 minutes,
  then resolves on its own (`send_resolved: false`, so no second message).
- **`level: '{{ $value }}'`** is a templated label: every crossed level is
  a distinct alert, so two different levels crossed within 5 minutes
  produce two notifications instead of updating one firing alert.
- **Message:** `{{ if eq $labels.quote "PLN" }}` formats PLN quotes with
  two decimals (`4.10`) and the rest as integers (`85000`).
- **Deploy order:** exporter first (producer of the new metrics), rules
  second (consumer). The other way round leaves a window with no working
  level alerts and no error.

Labels on every market alert: `severity: notice`, `category: market`.

### Routing

```yaml
# helm-values/kube-prometheus-stack.yaml, alertmanager.config
receivers:
  # ... null, ntfy, deadman (unchanged, indices 0-2)
  - name: "ntfy-market"            # index 3, URL injected at deploy time
    webhook_configs:
      - url: "REPLACE_ME?template=alertmanager"
        send_resolved: false
route:
  routes:
    - matchers: ['alertname = "Watchdog"']   # unchanged
      receiver: "deadman"
    - matchers: ['category = "market"']      # new
      receiver: "ntfy-market"
      group_by: ['alertname', 'asset']
      group_wait: 10s
      repeat_interval: 24h
    # severity critical / warning -> ntfy (unchanged)
```

Routing was regression-tested with `amtool config routes test` before
deployment:

| Labels | Receiver |
|---|---|
| `category=market severity=notice` | `ntfy-market` |
| `severity=critical` | `ntfy` (unchanged) |
| `severity=warning` | `ntfy` (unchanged) |
| `alertname=Watchdog` | `deadman` (unchanged) |
| `severity=info` | `null` (unchanged) |

## 10. Deployment procedures

### 10.1 First-time install of the workload

Namespace first: `kubectl apply -f <dir>` processes files alphabetically,
so `deployment.yaml` would be applied before `namespace.yaml`.

```bash
kubectl apply -f k8s/market-exporter/namespace.yaml
kubectl apply --dry-run=server -f k8s/market-exporter/     # full server-side validation, no changes
kubectl apply -f k8s/market-exporter/
kubectl -n market rollout status deployment/market-exporter --timeout=120s
```

### 10.2 Image upgrade

See the release process in §8. Rollback options:
- **Preferred:** revert the tag-bump commit in Git, merge, `kubectl apply`.
  Git stays the source of truth.
- **Emergency:** `kubectl -n market rollout undo deployment/market-exporter`.
  Fast, but the cluster now differs from Git; follow up with a revert.

### 10.3 Alertmanager routing change (`kube-prometheus-stack`)

This changes alerting for the **whole cluster**. A mistake can silently
stop infrastructure notifications.

1. **Validate offline:** extract `alertmanager.config` from the values
   file, substitute `REPLACE_ME` with `https://example.invalid/...`, run
   `amtool check-config` and the routing regression table from §9.
2. **Record the rollback point:** `helm list -n monitoring` (current
   revision) and the chart version.
3. **Collect the existing webhook URLs from the live release without
   printing them:**
   ```bash
   NTFY_URL=$(helm get values kube-prometheus-stack -n monitoring -o json | jq -r '.alertmanager.config.receivers[1].webhook_configs[0].url')
   DEADMAN_URL=$(helm get values kube-prometheus-stack -n monitoring -o json | jq -r '.alertmanager.config.receivers[2].webhook_configs[0].url')
   read -rs NTFY_MARKET_TOPIC     # paste the topic name only, from the password manager
   MARKET_URL="https://ntfy.sh/${NTFY_MARKET_TOPIC}?template=alertmanager"
   ```
   Check lengths and prefixes (`${#VAR}`, `${VAR:0:16}`); stop if any
   value is `REPLACE_ME` or `null`.
4. **Dry run to a file** (output contains secrets, never print it):
   ```bash
   helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack \
     -n monitoring --version <current chart version> \
     -f helm-values/kube-prometheus-stack.yaml \
     --set-string "alertmanager.config.receivers[1].webhook_configs[0].url=$NTFY_URL" \
     --set-string "alertmanager.config.receivers[2].webhook_configs[0].url=$DEADMAN_URL" \
     --set-string "alertmanager.config.receivers[3].webhook_configs[0].url=$MARKET_URL" \
     --dry-run=server > /tmp/kps-dryrun.yaml
   ```
   Always pin `--version`; without it Helm upgrades the whole chart.
5. **Diff live vs rendered config, URLs redacted.** The dry-run file has a
   text header and NOTES, so extract the `MANIFEST:` section first:
   ```bash
   sed -n '/^MANIFEST:/,/^NOTES:/{//!p}' /tmp/kps-dryrun.yaml > /tmp/kps-manifests.yaml
   kubectl -n monitoring get secret alertmanager-kube-prometheus-stack-alertmanager \
     -o jsonpath='{.data.alertmanager\.yaml}' | base64 -d > /tmp/am-live.yaml
   # decode the same Secret from /tmp/kps-manifests.yaml into /tmp/am-new.yaml
   redact() { sed -E 's#url: .*#url: <redacted>#' "$1"; }
   wc -l /tmp/am-live.yaml /tmp/am-new.yaml        # both non-empty!
   diff <(redact /tmp/am-live.yaml) <(redact /tmp/am-new.yaml)
   grep -c REPLACE_ME /tmp/am-new.yaml             # must be 0
   ```
   Expected diff: additions only (`>`). Any removal (`<`) means stop.
6. **Upgrade** with the identical command minus `--dry-run`, output to a
   file. Confirm the new revision is `deployed`.
7. **Verify end to end** (§11, step 6), then merge the PR immediately so
   `main` matches the cluster.
8. **Clean up:** delete the `/tmp` files and `unset` the variables.

Rollback: `helm rollback kube-prometheus-stack <previous revision> -n monitoring`.

## 11. Verification checklist

Run after every deploy. Each step checks one link of the chain in §2.

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Image published and pullable anonymously | `curl` the GHCR manifest with an anonymous pull token | HTTP `200`; manifest lists `amd64` and `arm64` |
| 2 | Pod healthy | `kubectl -n market get pods -o wide` | `1/1 Running`, restarts not increasing |
| 3 | App fetching | `kubectl -n market logs deploy/market-exporter --tail=10` | `price ...` lines for every source, no `fetch failed` |
| 4 | Service has endpoints | `kubectl -n market get endpointslices -l kubernetes.io/service-name=market-exporter` | the pod IP |
| 5 | Prometheus scraping | PromQL `up{namespace="market"}` | `1` |
| 6 | Data sane | PromQL `time() - asset_price_timestamp_seconds` | all values positive; Yahoo FX < 2 min, gold about 10 min, CoinGecko < 5 min, NBP hours |
| 7 | Rules loaded | `GET /api/v1/rules`, groups `market.*` | `market.recording`: `market:asset_price:max`; `market.levels`: `MarketLevelCrossedUp`, `MarketLevelCrossedDown`; `health=ok` |
| 8 | Routing live | `GET /api/v2/status` on Alertmanager, `config.original` contains `ntfy-market` | count > 0 |
| 9 | End to end | `amtool alert add ... category=market severity=notice --end=+5m` | notification on the **market** topic within about 10 s |
| 10 | Regression | `amtool alert add ... severity=warning --end=+5m` | notification on the **cluster** topic |

Rule loading (step 7) takes up to 1-2 minutes after `kubectl apply`:
operator -> ConfigMap -> kubelet volume sync -> config-reloader -> reload.

## 12. Operations runbook

### No market notifications although the price clearly crossed a level

1. Was it a **different** level than the last one crossed? Re-crossing the
   same level is intentionally silent (D17). Check what the exporter
   decided: `kubectl -n market logs deploy/market-exporter | grep "level crossed"`
   and `asset_last_crossed_level`. After a pod restart the memory starts
   from a fresh baseline.
2. Is the alert firing in Prometheus? `ALERTS{category="market"}`.
3. Is the data fresh? Check 6 in §11. A stale source cannot cross anything.
4. Is the rule healthy? Check 7 in §11.
5. Did Alertmanager receive and route it? `amtool alert query
   --alertmanager.url=... 'category="market"'`, then the routing test from §9.
6. Is the phone subscribed to the right topic (a typo is the most common
   cause)? Poll the topic directly:
   `curl -s "https://ntfy.sh/$TOPIC/json?poll=1&since=1h" | jq -r .message`.

### `up{namespace="market"} == 0` or no target at all

- No target: ServiceMonitor label `release: kube-prometheus-stack`
  missing, or Service labels/selector mismatch (check endpoints, §11 step 4).
- Target down: pod not Ready. Check probes and logs.

### Pod not Ready / CrashLoopBackOff

- `kubectl -n market describe pod` and look at events for probe failures,
  `OOMKilled` (raise the memory limit after checking actual use with
  `kubectl top pod -n market`) or `ImagePullBackOff` (tag not pushed yet).

### A source keeps failing (`exporter_fetch_errors_total` rising)

- `kubectl -n market logs deploy/market-exporter | grep "fetch failed"`.
- Yahoo: HTTP 429 or empty responses usually mean throttling or an API
  change. Old values stay exported; `asset_price_timestamp_seconds` stops
  advancing. Increase `YAHOO_INTERVAL_SECONDS` via the Deployment env if
  throttled.
- CoinGecko: free-tier rate limit; same mitigation with
  `COINGECKO_INTERVAL_SECONDS`.

### Change a grid step or add an asset

Steps live in `LEVEL_STEPS` in `market-exporter/exporter.py`, so this is
an image release (§8), not a rule change:

1. Change or add the entry in `LEVEL_STEPS` (a new asset also needs a
   fetcher, parser, the unit mapping used to pre-initialise counters, and
   tests).
2. Add a `LevelTracker` test with a price exactly on a level.
3. Release: PR -> merge -> image -> tag bump -> `kubectl apply` of the
   Deployment. The rules need no change; they work for any tracked series.

## 13. Testing strategy

| Layer | Tool | What it proves | Where it runs |
|---|---|---|---|
| Parsers and crossing memory | pytest (10 tests: parsers on real captured API responses, `LevelTracker` scenarios) | JSON shape handling, label sets, NBP timestamp never in the future (regression); baseline, a level reported once while the price dances around it, a different level in either direction, multi-band jump, floating-point edge case | CI `test` job, locally |
| Manifests | `kubeconform -strict` + CRD catalog | schema validity, no unknown fields (typos) | `validate.yml` on every PR |
| Manifests vs live cluster | `kubectl apply --dry-run=server` | API server acceptance incl. admission | manually before applying |
| Rule syntax | `promtool check rules` | every PromQL expression parses | CI `test` job |
| Rule behaviour | `promtool test rules` (synthetic counter and gauge series) | no alert before a crossing, alert right after with the correct `level` label and message text, auto-resolve after the 5-min window, direction handling, `4.10` formatting | CI `test` job |
| Routing | `amtool check-config`, `amtool config routes test` | new route works, existing routes unchanged | manually before `helm upgrade` |
| End to end | synthetic alerts via `amtool alert add` | delivery to the correct phone topic | manually after deploy |

Every new check was first shown to **fail** on a deliberate error (a
misspelled field for kubeconform, a broken manifest in a PR, a misspelled
PromQL function) before being trusted as green.

## 14. Known limitations and roadmap

| Item | Impact | Plan |
|---|---|---|
| Jump alerts not implemented yet | The size and speed of a move are not reported; only level crossings are. For gold, a 1 % move (about 40 USD) can happen entirely inside one 100 USD band and produce no notification | Add `BTC ±5 %/1h`, `USD/PLN and gold ±1 %/1d` using `market:asset_price:max / (... offset 1h) - 1`, with promtool tests |
| No source-health alerts | A dead source is visible only on dashboards | Alert on `exporter_last_success_timestamp_seconds` age, `rate(exporter_fetch_errors_total)`, and `up == 0`. Use fetch success, not data age, for Yahoo FX, because FX data legitimately goes stale at weekends |
| Yahoo is an unofficial API | May break without notice | Health alerts above; NBP as an official fallback for daily values |
| Gold intraday data is ~10 min delayed | Gold crossings reported about 10-13 min after they happen | Accepted |
| Crossing memory is in pod memory | After a restart (deploy) the first crossing can repeat the last notified level once | Accepted (D17); persistence would need a PVC or reading the last value back from Prometheus |
| NBP timestamp precision is one day | Staleness thresholds for NBP must be in days, covering long weekends | Accepted by design (D5) |
| Helm values not validated in CI | A routing typo is caught only by the manual `amtool` step | Add an `amtool` job to `validate.yml` |
| No Grafana dashboard yet | Charts need ad-hoc queries | Dashboard ConfigMap in `k8s/monitoring/`, same pattern as the existing ones |
| Actions on Node.js 20, `ubuntu-latest` floating | CI warnings; runner image changes without notice | Bump action majors (Renovate), pin `runs-on: ubuntu-24.04` |

## 15. Lessons learned during the build

| # | What happened | Root cause | Lesson / fix |
|---|---|---|---|
| L1 | NBP gold showed a **negative** data age in Prometheus (about -6500 s) | Code assumed every NBP value is published at 12:00; today's gold price appeared in the morning, so its timestamp was in the future. Unit tests passed because they encoded the same assumption | Found by observability (D3), not by tests. Fixed (D5) and a regression test added. Verify time assumptions against live data |
| L2 | During rollout PromQL returned **four** NBP series instead of two | Old and new pod are different scrape targets (`pod` label) | Aggregate `without (pod, instance)` in a recording rule before alerting |
| L3 | `containerPort: 8080` passed kubeconform | Schema validation checks shape, not meaning; the app listens on 8000 | Take ports from the code/Dockerfile; readiness probes catch it at runtime |
| L4 | Edits went to a re-created `deployment.yml` while checks ran on `deployment.yaml` | File renamed in the terminal while open in the editor; saving re-created the old name | Rename open files in the editor (F2); after every edit run `git diff` |
| L5 | `kubectl apply -f k8s/market-exporter/` failed on a `*.sync-conflict-*.yaml` | The working copy is also synced by Syncthing; a concurrent move produced a conflict copy | Keep non-manifest files out of manifest directories; repo/Syncthing interaction tracked separately |
| L6 | `kubectl apply --dry-run=server` failed with `namespaces "market" not found` | Server dry-run does not persist the Namespace; other objects then reference a missing namespace | Apply the Namespace first, then dry-run the rest |
| L7 | A config diff "passed" with `REPLACE_ME` count `0` | The rendered file was empty (the Helm dry-run output has a text header, YAML parsing failed); a check on empty input passes vacuously | Always check input size before trusting a check; extract the `MANIFEST:` section |
| L8 | A CI job was green but `tflint` never ran | The step that runs the tool was missing; only the installer step existed | Confirm in logs that the step actually executed ("green is not verified") |
| L9 | `python-version: 3.12` unquoted; `on:` read as `true` by PyYAML | YAML 1.1 type coercion | Quote versions and anything that could become a number or boolean |
| L10 | A rule-only PR would not have triggered rule tests | `paths` filter did not include the rule file | When adding a test, check the trigger covers every input of that test |
| L11 | A week of real notifications showed repeated messages for the same level when a price oscillated slowly around it | Time-window debounce (D12) has no memory beyond its window | Requirement needed state; moved to the exporter (D17). Validate alert design against real data, not only synthetic tests |
| L12 | While editing rules the recording rule was deleted by accident; all tests stayed green | The new tests did not use the recording rule | `promtool check rules` reported 2 rules instead of the expected 3. Green tests prove only what they test; know the expected counts |

## 16. Change history

| PR | Commit | Change |
|---|---|---|
| #1 | `43a11f4` | `validate.yml`: kubeconform (strict, CRD catalog), terraform fmt/validate, tflint |
| #2 | `728918b` | Exporter, unit tests, Dockerfile, multi-arch build workflow |
| #3 | `0bb011d` | Namespace, Deployment (restricted securityContext), Service, ServiceMonitor |
| #4 | `a270864` | Fix: NBP timestamp at 00:00 Warsaw, regression test (L1) |
| #5 | `fafc3ac` | Deploy image `a270864` |
| #6 | `ae29120` | Recording rule, 6 level-crossing alerts, promtool check + unit tests in CI |
| #7 | `f6d5af5` | Alertmanager `ntfy-market` receiver and `category=market` route |
| #8 | `7785a2b` | This document |
| #9 | `50ef207` | Crossing memory (`LevelTracker`), two generic alerts replace six debounced ones |
| #10 | `2956fc1` | Deploy image `50ef207` |

## 17. References

- Prometheus: [Writing exporters](https://prometheus.io/docs/instrumenting/writing_exporters/),
  [Metric and label naming](https://prometheus.io/docs/practices/naming/),
  [Recording rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/),
  [Alerting rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/),
  [Unit testing for rules](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/)
- Alertmanager: [Configuration](https://prometheus.io/docs/alerting/latest/configuration/)
- Prometheus Operator: `ServiceMonitor` and `PrometheusRule` API (`kubectl explain servicemonitor.spec`)
- Kubernetes: [Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/),
  [Resource management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/),
  [Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)
- Docker: multi-platform builds with GitHub Actions (docs.docker.com, "GitHub Actions" section)
- NBP API: https://api.nbp.pl
- Related repo docs: [docs/SECURITY.md](../docs/SECURITY.md),
  [docs/postmortems/2026-09-12-alertmanager-null-routing.md](../docs/postmortems/2026-09-12-alertmanager-null-routing.md)
