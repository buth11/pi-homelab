#!/usr/bin/env python3
"""SMART wear report for every disk in the homelab.

Two ways to run it:

    ./smart_report.py                 # ask the dashboard to scan, print the result
    ./smart_report.py --no-scan       # show the dashboard's last scan
    ./smart_report.py --history [SN]  # per-disk history from the dashboard
    ./smart_report.py --local         # SSH from this machine, local SQLite history

The dashboard (Disks tab) holds the long-term history in SQLite on
pi4-master, so the default mode only talks to its API. --local is the
fallback for when the dashboard is down.

How wear is judged:
  SSD/NVMe  bytes written vs the manufacturer's TBW (endurance.conf), and the
            drive's own "% used" counter; the higher of the two counts.
            Remaining life = TBW left / current write rate.
  HDD       load/unload cycles vs rating, yearly workload (read + written)
            vs rating, start/stop rate. Media errors (reallocated, pending,
            uncorrectable) raise an alert when they grow between scans.
Rates are least-squares slopes over the last 30 and 90 days of history
(calendar time, not power-on hours); the higher one is used.

Spun-down HDDs are skipped, not woken (`--scan` + `-n standby`), so
truenas/hdd-idle keeps working. Standard library only.
Full description: README.md in this directory.
"""

import argparse
import base64
import concurrent.futures
import datetime as dt
import json
import netrc
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HOSTS = os.path.join(HERE, "hosts.conf")
DEFAULT_SPECS = os.path.join(HERE, "endurance.conf")
# Outside the repo on purpose: the history holds disk serial numbers and the
# repo is a Syncthing folder pushed to GitHub.
DEFAULT_DB = os.path.expanduser("~/.local/share/smart-report/smart.db")
OLD_JSONL = os.path.expanduser("~/.local/share/smart-report/history.jsonl")
DEFAULT_DASHBOARD = os.environ.get("SMART_DASHBOARD", "http://192.168.50.58")

SSH_TIMEOUT = 90  # seconds per host

# Thresholds, in one place so they are easy to tune.
WARN_WEAR_PCT = 70
CRIT_WEAR_PCT = 90
WARN_YEARS_LEFT = 3
CRIT_YEARS_LEFT = 1
WARN_TEMP_SSD = 70
WARN_TEMP_HDD = 50
MIN_HISTORY_DAYS = 0.8     # shortest span a rate is computed over
MIN_30D_SPAN_DAYS = 7      # the 30-day rate needs at least this much data
MIN_PCT_TREND_DAYS = 60    # "% used" moves in whole percent: needs a long span
MIN_LIFETIME_POH = 720     # hours; below this a lifetime average is noise
COUNTER_WINDOW_DAYS = 7    # error-counter growth stays flagged this long

# Counters that indicate media damage: non-zero is a warning, growth is critical.
MEDIA_COUNTERS = (
    ("realloc", "reallocated sectors"),
    ("pending", "pending sectors"),
    ("offline_unc", "offline uncorrectable sectors"),
    ("reported_unc", "reported uncorrectable errors"),
    ("media_errors", "NVMe media errors"),
)
# Counters that only matter when they grow (a static value is old history).
GROWTH_COUNTERS = (
    ("crc", "UDMA CRC errors (cable/connector)"),
    ("err_log", "device error log entries"),
)

# Runs on the remote host via `bash -s`. Prints marker lines that
# parse_remote() splits on; smartctl JSON goes between them unchanged.
REMOTE_SCRIPT = r"""
set -u
if [ "$(id -u)" -eq 0 ]; then S=""; else S="sudo -n"; fi
SC=$(command -v smartctl 2>/dev/null || true)
[ -z "$SC" ] && [ -x /usr/sbin/smartctl ] && SC=/usr/sbin/smartctl
echo "@@HOST $(hostname)"
for d in /sys/block/mmcblk[0-9]*; do
  [ -e "$d/device/name" ] || continue
  echo "@@MMC $(basename "$d")|$(cat "$d/device/name")|$(cat "$d/device/date" 2>/dev/null)|$(cat "$d/device/manfid" 2>/dev/null)|$(( $(cat "$d/size") * 512 ))|$(cat "$d/device/type" 2>/dev/null)|$(tr -d '\0' </proc/device-tree/model 2>/dev/null)"
done
if [ -z "$SC" ]; then echo "@@NOSMARTCTL"; exit 0; fi
if [ -n "$S" ] && ! sudo -n "$SC" --version >/dev/null 2>&1; then echo "@@NOSUDO"; exit 0; fi
# --scan (not --scan-open): it does not send commands to the disks,
# so a spun-down HDD stays asleep.
for dev in $($S "$SC" --scan | awk '$1 ~ /^\/dev\// {print $1}'); do
  echo "@@DEV $dev"
  $S "$SC" -x -j -n standby "$dev" 2>/dev/null
  rc=$?
  echo
  echo "@@RC $rc"
done
echo "@@DONE"
"""

COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def paint(text, level):
    if not COLOR:
        return text
    code = {"CRIT": "31;1", "WARN": "33;1", "OK": "32", "DIM": "2"}.get(level)
    return f"\033[{code}m{text}\033[0m" if code else text


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def read_hosts(path):
    hosts = []
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                raise ValueError(f"{path}: bad line (need: NAME SSH_TARGET): {line}")
            hosts.append((parts[0], parts[1]))
    return hosts


def read_specs(path):
    """endurance.conf -> [(model substring, {tbw, load_cycles, workload})].
    Value part: a bare number (= TBW) or key=value pairs."""
    table = []
    if not os.path.exists(path):
        return table
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.split("#", 1)[0].strip()
            if not line or "|" not in line:
                continue
            model, values = line.split("|", 1)
            spec = {}
            for part in values.replace("|", " ").split():
                key, _, val = part.partition("=") if "=" in part else ("tbw", "", part)
                if key not in ("tbw", "load_cycles", "workload"):
                    raise ValueError(f"{path}:{n}: unknown key {key!r}")
                spec[key] = float(val)
            table.append((model.strip().lower(), spec))
    return table


def spec_for(model, table):
    model = (model or "").lower()
    best = None
    for sub, spec in table:
        if sub in model and (best is None or len(sub) > len(best[0])):
            best = (sub, spec)
    return best[1] if best else {}


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

def ssh_cli_runner(target, script):
    """Default transport: the system ssh client. Returns (stdout, error)."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target, "bash -s"]
    try:
        p = subprocess.run(cmd, input=script, capture_output=True,
                           text=True, timeout=SSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "", f"SSH timeout after {SSH_TIMEOUT}s"
    err = (p.stderr.strip().splitlines() or ["no output"])[-1]
    return p.stdout, err


def collect_host(name, target, runner=ssh_cli_runner):
    """runner(target, script) -> (stdout, error_text). The dashboard backend
    passes a paramiko-based runner; the CLI uses the system ssh."""
    try:
        out, err = runner(target, REMOTE_SCRIPT)
    except Exception as exc:  # a runner bug or network error must not kill the whole report
        return {"host": name, "error": f"SSH failed: {exc}"}
    if "@@HOST" not in out:
        return {"host": name, "error": f"SSH failed: {err or 'no output'}"}
    return parse_remote(name, out)


def run_report(hosts, history, specs, runner=ssh_cli_runner, now=None):
    """Collect from all hosts in parallel and analyse against `history`
    (readings, oldest first). Returns (now, disks, host_errors)."""
    now = now or dt.datetime.now(dt.timezone.utc).astimezone()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(hosts), 1)) as ex:
        results = list(ex.map(lambda h: collect_host(h[0], h[1], runner), hosts))
    disks, host_errors = [], []
    for r in results:
        if "error" in r:
            host_errors.append((r["host"], r["error"]))
            continue
        for n in r["notes"]:
            host_errors.append((r["host"], n))
        disks.extend(r["disks"])
    by_serial = {}
    for h in history:
        by_serial.setdefault(h.get("serial"), []).append(h)
    for d in disks:
        analyse(d, by_serial.get(d.get("serial"), []), now, specs)
        classify(d)
    return now, disks, host_errors


def parse_remote(name, out):
    res = {"host": name, "disks": [], "notes": []}
    cur_dev, buf = None, []
    for line in out.splitlines():
        if line.startswith("@@NOSMARTCTL"):
            res["notes"].append("smartctl not installed")
        elif line.startswith("@@NOSUDO"):
            res["notes"].append("passwordless sudo for smartctl is missing (see README)")
        elif line.startswith("@@MMC "):
            dev, cid_name, date, manfid, size, ctype, board = (line[6:].split("|") + [""] * 7)[:7]
            if ctype == "MMC":
                ff = "eMMC"
            elif "Raspberry Pi" in board:
                ff = "microSD"  # every Raspberry Pi card slot since the B+ is microSD
            else:
                ff = "SD"
            res["disks"].append({
                "host": name, "dev": "/dev/" + dev, "kind": "SD",
                "model": sd_model(cid_name, manfid),
                "manufactured": sd_date(date),
                "manufactured_src": "card CID register",
                "capacity": int(size) if size.isdigit() else None,
                "form_factor": ff,
                "form_factor_src": f"card type {ctype or '?'}" + (f", {board}" if board else ""),
                "level": "INFO",
                "issues": ["SD/eMMC card: no SMART, wear cannot be read"],
            })
        elif line.startswith("@@DEV "):
            cur_dev, buf = line[6:].strip(), []
        elif line.startswith("@@RC ") and cur_dev:
            rc = int(line[5:].strip() or 0)
            res["disks"].append(parse_disk(name, cur_dev, "\n".join(buf), rc))
            cur_dev, buf = None, []
        elif cur_dev is not None:
            buf.append(line)
    # A host with only SD cards (the Pis) has nothing smartctl could read,
    # so a missing smartctl there is expected, not a problem.
    if res["disks"] and all(d["kind"] == "SD" for d in res["disks"]):
        res["notes"] = [n for n in res["notes"] if n != "smartctl not installed"]
    return res


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

# ATA attributes whose *normalized* value is "life remaining %"
# (smartctl database names; unnamed vendor attributes are ignored).
ATA_LIFE_REMAINING_ATTRS = {
    "SSD_Life_Left", "Media_Wearout_Indicator", "Percent_Lifetime_Remain",
    "Wear_Leveling_Count", "Remaining_Lifetime_Perc", "Percent_Life_Remaining",
}


def parse_disk(host, dev, text, rc):
    d = {"host": host, "dev": dev, "issues": []}
    try:
        j = json.loads(text)
    except ValueError:
        d.update(kind="?", level="WARN", issues=["WARN: smartctl returned no JSON"])
        return d

    msgs = " ".join(m.get("string", "") for m in j.get("smartctl", {}).get("messages", []))
    if any(w in msgs.upper() for w in ("STANDBY", "SLEEP")):
        d.update(kind="HDD", level="SKIP", model="(spun down)",
                 issues=["spun down, skipped so it is not woken (-n standby)"])
        return d

    proto = j.get("device", {}).get("protocol")
    rot = j.get("rotation_rate")
    if proto == "NVMe":
        kind = "NVMe"
    elif isinstance(rot, int) and rot > 0:
        kind = "HDD"
    elif rot == 0:
        kind = "SSD"
    else:
        kind = proto or "?"

    d.update(
        kind=kind,
        model=j.get("model_name") or j.get("scsi_model_name") or j.get("model_family") or "?",
        serial=j.get("serial_number"),
        firmware=j.get("firmware_version"),
        capacity=j.get("user_capacity", {}).get("bytes"),
        poh=j.get("power_on_time", {}).get("hours"),
        temp=j.get("temperature", {}).get("current"),
        smart_passed=j.get("smart_status", {}).get("passed"),
    )
    d["form_factor"], d["form_factor_src"] = form_factor(j, kind, d["model"])

    if not j.get("smart_support", {}).get("available", True) or (
            d["poh"] is None and d["smart_passed"] is None):
        d["level"] = "INFO"
        d["issues"].append("no SMART data (virtual disk or USB bridge?)")
        return d

    if kind == "NVMe":
        parse_nvme(d, j.get("nvme_smart_health_information_log", {}))
    else:
        parse_ata(d, j)

    if d["smart_passed"] is False:
        d["issues"].append("CRIT: SMART overall-health FAILED")
    if rc & 0b1000:
        d["issues"].append("CRIT: smartctl: disk failing (exit bit 3)")
    if rc & 0b10000:
        d["issues"].append("WARN: an attribute was below threshold in the past (exit bit 4)")
    if rc & 0b10000000:
        d["issues"].append("WARN: self-test log reports errors (exit bit 7)")
    # Exit bit 6 (error log has entries) is handled through the err_log
    # counter instead: only growth matters, old entries stay forever.
    return d


# SD card manufacturer IDs (CID register, assigned by the SD Association).
# Only well-established ones; anything else is shown as the raw ID.
SD_MANUFACTURERS = {
    0x01: "Panasonic", 0x02: "Toshiba/Kioxia", 0x03: "SanDisk", 0x1B: "Samsung",
    0x1D: "ADATA", 0x27: "Phison", 0x28: "Lexar", 0x41: "Kingston",
    0x74: "Transcend", 0x76: "Patriot", 0x82: "Sony",
}


def sd_model(name, manfid):
    try:
        mid = int(manfid, 16)
    except ValueError:
        return name or "?"
    if mid in SD_MANUFACTURERS:
        return f"{SD_MANUFACTURERS[mid]} {name}".strip()
    # 0x00 is not a valid ID; common on no-name and counterfeit cards
    return f"{name} (unknown maker, manfid {mid:#04x})"


def sd_date(date):
    """CID date "MM/YYYY" -> "YYYY-MM" (sorts as text)."""
    m, _, y = (date or "").partition("/")
    return f"{y}-{int(m):02d}" if y.isdigit() and m.isdigit() else None


FORM_FACTOR_NAMES = {
    "3.5 inches": '3.5"', "2.5 inches": '2.5"', "1.8 inches": '1.8"',
    "less than 1.8 inches": '<1.8"', "5.25 inches": '5.25"',
    "M.2": "M.2", "mSATA": "mSATA", "MicroSSD": "MicroSSD", "CFast": "CFast",
}


def form_factor(j, kind, model):
    """(form factor, where it came from). The drive's own IDENTIFY data
    (ATA word 168) is used, except when the model name clearly says M.2:
    the no-name "M.2 SSD 512GB" in g3-worker3 reports 2.5 inches. NVMe has
    no such field; every consumer NVMe line in this lab is M.2 only."""
    reported = FORM_FACTOR_NAMES.get((j.get("form_factor") or {}).get("name"))
    up = (model or "").upper()
    if "M.2" in up or " 2280" in up:
        src = "model name" + (f" (drive reports {reported})" if reported and reported != "M.2" else "")
        return "M.2", src
    if reported:
        return reported, "drive"
    if kind == "NVMe":
        return "M.2", "assumed for NVMe (no form factor field)"
    return None, None


def parse_nvme(d, log):
    unit = 512_000  # NVMe data unit = 1000 x 512 B
    if log.get("data_units_written") is not None:
        d["written"] = log["data_units_written"] * unit
    if log.get("data_units_read") is not None:
        d["read"] = log["data_units_read"] * unit
    d["used_pct"] = log.get("percentage_used")
    d["unsafe_shutdowns"] = log.get("unsafe_shutdowns")
    d["media_errors"] = log.get("media_errors")
    d["err_log"] = log.get("num_err_log_entries")
    spare, thr = log.get("available_spare"), log.get("available_spare_threshold")
    if log.get("critical_warning"):
        d["issues"].append(f"CRIT: NVMe critical_warning = {log['critical_warning']:#x}")
    if spare is not None and thr is not None and spare <= thr:
        d["issues"].append(f"CRIT: spare blocks at the threshold ({spare}% <= {thr}%)")
    elif spare is not None and spare < 100:
        d["issues"].append(f"spare blocks dropped to {spare}% (threshold {thr}%)")


def parse_ata(d, j):
    attrs = {a["id"]: a for a in j.get("ata_smart_attributes", {}).get("table", [])}
    raw = lambda i: attrs[i]["raw"]["value"] if i in attrs else None
    devstat = {}
    for page in j.get("ata_device_statistics", {}).get("pages", []):
        for row in page.get("table", []):
            devstat[row.get("name")] = row.get("value")
    lbs = j.get("logical_block_size") or 512

    # Power-on hours: smartctl prefers the Device Statistics value, which
    # some firmware gets badly wrong (SanDisk X400: 135 h in devstat vs
    # 8710 h in attribute 9; WD Blue WDS250G2B0A: 0 vs 21900). Take the
    # larger of the two.
    if 9 in attrs and attrs[9].get("name") == "Power_On_Hours":
        s = str(attrs[9]["raw"].get("string", "")).split("h")[0].split()[0]
        if s.isdigit() and 0 < int(s) < 200_000:
            d["poh"] = max(d.get("poh") or 0, int(s))

    # Bytes written: ATA Device Statistics are standardised (sectors),
    # attribute 241 is not (sectors, 32 MiB or GiB depending on vendor).
    if devstat.get("Logical Sectors Written") is not None:
        d["written"] = devstat["Logical Sectors Written"] * lbs
    elif raw(246) is not None and attrs[246].get("name") in (
            "Total_Host_Sector_Write", "Total_LBAs_Written"):  # Crucial/Micron (BX500: no devstat)
        d["written"] = raw(246) * lbs
    elif raw(241) is not None:
        d["written"] = raw(241) * lbs
        d["issues"].append("bytes written from attribute 241, unit unconfirmed for this model")
    if devstat.get("Logical Sectors Read") is not None:
        d["read"] = devstat["Logical Sectors Read"] * lbs

    if d["kind"] != "HDD":
        if devstat.get("Percentage Used Endurance Indicator") is not None:
            d["used_pct"] = devstat["Percentage Used Endurance Indicator"]
        else:
            for a in attrs.values():
                if a.get("name") in ATA_LIFE_REMAINING_ATTRS and 0 <= a.get("value", -1) <= 100:
                    d["used_pct"] = 100 - a["value"]
                    break

    d.update(realloc=raw(5), pending=raw(197), offline_unc=raw(198),
             reported_unc=raw(187), crc=raw(199))
    log = j.get("ata_smart_error_log", {})
    d["err_log"] = (log.get("extended") or log.get("summary") or {}).get("count")
    if d["kind"] == "HDD":
        d["start_stop"] = raw(4)
        d["load_cycles"] = raw(193)


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def _ts(row):
    return dt.datetime.fromisoformat(row["ts"])


def trend(d, past, key, now, window_days, min_span):
    """Least-squares slope of `key` per day over the last `window_days`,
    current value included. Points before a counter reset (value going
    down) are dropped. Returns (slope_per_day, span_days) or None."""
    pts = []
    for h in past:
        age = (now - _ts(h)).total_seconds() / 86400
        if 0 < age <= window_days and h.get(key) is not None:
            pts.append((-age, h[key]))
    if d.get(key) is None:
        return None
    pts.append((0.0, d[key]))
    pts.sort()
    start = 0
    for i in range(1, len(pts)):
        if pts[i][1] < pts[i - 1][1]:
            start = i
    pts = pts[start:]
    if len(pts) < 2:
        return None
    span = pts[-1][0] - pts[0][0]
    if span < min_span:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx == 0:
        return None
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx
    return max(slope, 0.0), span


def _rate(d, past, key, now):
    """Higher of the 30- and 90-day rates (pessimistic: a recent burst of
    writes counts). Returns (per_day, span_days, label) or None."""
    candidates = []
    r90 = trend(d, past, key, now, 90, MIN_HISTORY_DAYS)
    if r90:
        candidates.append((r90[0], r90[1], f"{r90[1]:.0f}-day history"))
    r30 = trend(d, past, key, now, 30, MIN_30D_SPAN_DAYS)
    if r30:
        candidates.append((r30[0], r30[1], f"last {r30[1]:.0f} days"))
    return max(candidates) if candidates else None


def analyse(d, past, now, specs):
    """Fill wear, rates and projections for one disk. `past` = this disk's
    earlier readings, oldest first."""
    if not d.get("serial") or d["kind"] == "SD":
        return
    past = sorted((h for h in past if h.get("ts") and _ts(h) < now), key=_ts)
    spec = spec_for(d.get("model"), specs)

    # Counter changes (used by classify()), compared with the oldest scan of
    # the last COUNTER_WINDOW_DAYS, so a new error stays flagged for a week
    # instead of disappearing at the next scan.
    if past:
        recent = [h for h in past if (now - _ts(h)).days < COUNTER_WINDOW_DAYS]
        prev = recent[0] if recent else past[-1]
        d["prev_ts"] = prev["ts"]
        d["changes"] = {k: [prev[k], d[k]]
                        for k, _ in MEDIA_COUNTERS + GROWTH_COUNTERS
                        if d.get(k) is not None and prev.get(k) is not None and d[k] > prev[k]}

    w = _rate(d, past, "written", now)
    if w:
        d["gb_per_day"], d["history_days"], d["rate_src"] = w[0] / 1e9, w[1], w[2]
    elif d.get("written") is not None and (d.get("poh") or 0) >= MIN_LIFETIME_POH:
        # No history yet: lifetime average per power-on hour. Only a rough
        # hint: power-on hours undercount on hosts switched off part of the
        # day, and on drives that stop the counter in low-power states.
        d["gb_per_day"] = d["written"] / 1e9 / (d["poh"] / 24)
        d["rate_src"] = "lifetime average"
    r = _rate(d, past, "read", now)
    if r:
        d["read_gb_per_day"] = r[0] / 1e9

    if d["kind"] == "HDD":
        _analyse_hdd(d, past, now, spec)
    else:
        _analyse_ssd(d, past, now, spec)


def _analyse_ssd(d, past, now, spec):
    written, used = d.get("written"), d.get("used_pct")
    tbw = spec.get("tbw")
    wear = []
    if tbw and written is not None:
        d["tbw"] = tbw
        d["tbw_pct"] = written / 1e12 / tbw * 100
        d["tbw_left_tb"] = max(tbw - written / 1e12, 0.0)
        wear.append((d["tbw_pct"], "TBW"))
    if used is not None:
        wear.append((float(used), "drive's own counter"))
    if wear:
        d["wear_pct"], d["wear_src"] = max(wear)

    pct = trend(d, past, "used_pct", now, 365, MIN_PCT_TREND_DAYS)
    if pct and pct[0] > 0:
        d["pct_per_year"] = pct[0] * 365

    lifetime_rate = d.get("rate_src") == "lifetime average"
    confident, rough = [], []
    if tbw and written is not None and d.get("gb_per_day"):
        years = max((tbw * 1e12 - written) / (d["gb_per_day"] * 1e9) / 365, 0)
        (rough if lifetime_rate else confident).append((years, "TBW at current write rate"))
    if d.get("pct_per_year"):
        confident.append(((100 - used) / d["pct_per_year"], "% used trend"))
    elif used and used >= 1 and (d.get("poh") or 0) >= MIN_LIFETIME_POH:
        rough.append(((100 - used) / (used / (d["poh"] / 24 / 365)), "% used, lifetime average"))
    _set_projection(d, confident, rough)


def _analyse_hdd(d, past, now, spec):
    lc, lc_rating = d.get("load_cycles"), spec.get("load_cycles")
    if lc is not None and lc_rating:
        d["load_cycles_rating"] = lc_rating
        d["wear_pct"], d["wear_src"] = lc / lc_rating * 100, "load cycles"
    ss = _rate(d, past, "start_stop", now)
    if ss:
        d["starts_per_day"] = ss[0]
    lcr = _rate(d, past, "load_cycles", now)
    if lcr:
        d["load_cycles_per_day"] = lcr[0]
    if d.get("gb_per_day") is not None and d.get("rate_src") != "lifetime average":
        d["workload_tb_year"] = (d["gb_per_day"] + d.get("read_gb_per_day", 0)) * 365 / 1000
        if spec.get("workload"):
            d["workload_rating"] = spec["workload"]
            d["workload_pct"] = d["workload_tb_year"] / spec["workload"] * 100
    confident = []
    if lc is not None and lc_rating and d.get("load_cycles_per_day"):
        confident.append((max(lc_rating - lc, 0) / d["load_cycles_per_day"] / 365, "load cycle budget"))
    _set_projection(d, confident, [])


def _set_projection(d, confident, rough):
    """Only projections built on measured history may raise an alert;
    lifetime averages are shown as "~" estimates and used only when nothing
    better exists."""
    if confident:
        d["years_left"], d["years_src"] = min(confident)
        d["years_confident"] = True
    elif rough:
        d["years_left"], d["years_src"] = min(rough)
        d["years_confident"] = False


def classify(d):
    if d.get("level") in ("SKIP", "INFO"):
        return
    issues = d["issues"]
    changes = d.get("changes", {})
    since = f" since {d['prev_ts'][:10]}" if d.get("prev_ts") else ""

    for key, label in MEDIA_COUNTERS:
        v = d.get(key)
        if key in changes:
            issues.append(f"CRIT: {label} grew {changes[key][0]} -> {v}{since}")
        elif v:
            issues.append(f"WARN: {label} = {v}")
    for key, label in GROWTH_COUNTERS:
        v = d.get(key)
        if key in changes:
            issues.append(f"WARN: {label} grew {changes[key][0]} -> {v}{since}")
        elif v:
            note = "not grown recently" if d.get("prev_ts") else "no earlier scan to compare"
            issues.append(f"{label} = {v} ({note})")

    w = d.get("wear_pct")
    if w is not None:
        src = d.get("wear_src", "")
        if w >= CRIT_WEAR_PCT:
            issues.append(f"CRIT: wear {w:.0f}% ({src})")
        elif w >= WARN_WEAR_PCT:
            issues.append(f"WARN: wear {w:.0f}% ({src})")
    if d.get("workload_pct", 0) > 100:
        issues.append(f"WARN: workload {d['workload_tb_year']:.0f} TB/year, "
                      f"above the rated {d['workload_rating']:.0f}")

    y = d.get("years_left")
    if y is not None and d.get("years_confident"):
        if y < CRIT_YEARS_LEFT:
            issues.append(f"CRIT: projected life under a year ({y:.1f}, {d['years_src']})")
        elif y < WARN_YEARS_LEFT:
            issues.append(f"WARN: projected life {y:.1f} years ({d['years_src']})")
    elif y is not None and y < WARN_YEARS_LEFT:
        issues.append(f"rough estimate only: ~{y:.1f} years ({d['years_src']}); "
                      "alerts start once there is history")

    t = d.get("temp")
    limit = WARN_TEMP_HDD if d["kind"] == "HDD" else WARN_TEMP_SSD
    if t is not None and t >= limit:
        issues.append(f"WARN: temperature {t}°C (limit {limit}°C)")

    if any(i.startswith("CRIT") for i in issues):
        d["level"] = "CRIT"
    elif any(i.startswith("WARN") for i in issues):
        d["level"] = "WARN"
    else:
        d["level"] = "OK"


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def fmt_bytes(n):
    if n is None:
        return "-"
    return f"{n / 1e12:.2f} TB" if n >= 1e12 else f"{n / 1e9:.0f} GB"


def fmt_size(n):
    if not n:
        return "-"
    return f"{n / 1e12:.1f}T" if n >= 1e12 else f"{n / 1e9:.0f}G"


def fmt_pct(p):
    if p is None:
        return "-"
    return f"{p:.0f}%" if p >= 10 else f"{p:.1f}%"


def fmt_years(d):
    y = d.get("years_left")
    if y is None:
        return "-"
    txt = ">50 y" if y > 50 else f"{y:.1f} y"
    return txt if d.get("years_confident") else "~" + txt


def print_table(disks):
    cols = [("HOST", 11), ("DEVICE", 12), ("MODEL", 26), ("TYPE", 4), ("FORM", 7), ("MADE", 7), ("SIZE", 5),
            ("HOURS", 6), ("WRITTEN", 9), ("WEAR", 5), ("TBW LEFT", 9), ("TEMP", 4),
            ("GB/DAY", 7), ("LIFE LEFT", 9), ("STATUS", 6)]
    print("  ".join(n.ljust(w) for n, w in cols))
    print("  ".join("-" * w for _, w in cols))
    for d in disks:
        rate = "-"
        if d.get("gb_per_day") is not None:
            rate = f"{d['gb_per_day']:.1f}" + ("*" if d.get("rate_src") == "lifetime average" else "")
        row = [
            d["host"], d["dev"], (d.get("model") or "?")[:26], d.get("kind", "?"),
            d.get("form_factor") or "-",
            d.get("manufactured") or "-",
            fmt_size(d.get("capacity")),
            str(d["poh"]) if d.get("poh") is not None else "-",
            fmt_bytes(d.get("written")),
            fmt_pct(d.get("wear_pct")),
            f"{d['tbw_left_tb']:.1f} TB" if d.get("tbw_left_tb") is not None else "-",
            f"{d['temp']}°" if d.get("temp") is not None else "-",
            rate, fmt_years(d), d.get("level", "?"),
        ]
        cells = [str(v).ljust(w) for v, (_, w) in zip(row, cols)]
        cells[-1] = paint(cells[-1], d.get("level"))
        line = "  ".join(cells)
        print(paint(line, "DIM") if d.get("level") in ("SKIP", "INFO") else line)


def details(d):
    """Secondary facts for one disk, as short strings."""
    out = []
    if d.get("tbw"):
        out.append(f"{fmt_pct(d.get('tbw_pct'))} of rated {d['tbw']:.0f} TBW")
    if d.get("used_pct") is not None and d["kind"] != "HDD":
        out.append(f"drive's own counter {d['used_pct']}%")
    if d.get("pct_per_year"):
        out.append(f"+{d['pct_per_year']:.1f}%/year")
    if d.get("load_cycles") is not None:
        rating = f" of {d['load_cycles_rating']:.0f}" if d.get("load_cycles_rating") else ""
        out.append(f"load cycles {d['load_cycles']}{rating}")
    if d.get("start_stop") is not None:
        per = f" ({d['starts_per_day']:.1f}/day)" if d.get("starts_per_day") is not None else ""
        out.append(f"start/stop {d['start_stop']}{per}")
    if d.get("workload_tb_year") is not None:
        rating = f" of {d['workload_rating']:.0f}" if d.get("workload_rating") else ""
        out.append(f"workload {d['workload_tb_year']:.1f} TB/year{rating}")
    if d.get("read") is not None:
        out.append(f"read {fmt_bytes(d['read'])}")
    if d.get("unsafe_shutdowns"):
        out.append(f"unsafe shutdowns {d['unsafe_shutdowns']}")
    if d.get("rate_src") and d["rate_src"] != "lifetime average":
        out.append(f"rate from {d['rate_src']}")
    if d.get("years_src"):
        out.append(f"projection: {d['years_src']}")
    return out


def print_notes(disks, host_errors):
    lines = []
    for d in disks:
        extra = details(d)
        if d["issues"] or extra:
            lines.append(paint(f"{d['host']} {d['dev']} {d.get('model', '')}".strip(), d.get("level")))
            lines += [f"    - {i}" for i in d["issues"]]
            if extra:
                lines.append("      " + "; ".join(extra))
    for h, e in host_errors:
        lines.append(paint(f"{h}: {e}", "WARN"))
    if lines:
        print("\nNotes:")
        print("\n".join(lines))
    if any(d.get("rate_src") == "lifetime average" for d in disks):
        print("\n* GB/DAY with an asterisk, LIFE LEFT with ~ = lifetime average (no history yet); "
              "never raises an alert. Real trends start from a scan at least a day after the first.")


def print_history(rows, serial_filter=None):
    by_serial = {}
    for h in rows:
        by_serial.setdefault(h["serial"], []).append(h)
    for serial, rs in sorted(by_serial.items(), key=lambda kv: (kv[1][-1].get("host") or "", kv[0])):
        if serial_filter and serial_filter not in serial:
            continue
        last = rs[-1]
        print(f"\n{last.get('host', '?')}  {last.get('model')}  ({last.get('kind', '?')}, s/n …{serial[-4:]})")
        for r in rs:
            u = f"{r['used_pct']}%" if r.get("used_pct") is not None else "-"
            print(f"  {r['ts'][:16]}  written {fmt_bytes(r.get('written')):>9}  "
                  f"wear {u:>4}  hours {r.get('poh')}")


# --------------------------------------------------------------------------
# Dashboard API client
# --------------------------------------------------------------------------

def _auth_header(url):
    user = os.environ.get("SMART_DASHBOARD_USER")
    pw = os.environ.get("SMART_DASHBOARD_PASSWORD")
    if not user:
        host = urllib.parse.urlparse(url).hostname
        try:
            found = netrc.netrc().authenticators(host)
        except (FileNotFoundError, netrc.NetrcParseError):
            found = None
        if found:
            user, _, pw = found
    if not user:
        return {}
    token = base64.b64encode(f"{user}:{pw or ''}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def api(url, path, method="GET"):
    req = urllib.request.Request(url.rstrip("/") + path, method=method, headers=_auth_header(url))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit(f"{url}: 401 Unauthorized. Put the dashboard login in ~/.netrc "
                     f"(machine {urllib.parse.urlparse(url).hostname} login USER password PASS, chmod 600) "
                     "or SMART_DASHBOARD_USER/SMART_DASHBOARD_PASSWORD. See README.")
        sys.exit(f"{url}{path}: HTTP {e.code}")
    except urllib.error.URLError as e:
        sys.exit(f"{url}: unreachable ({e.reason}). Use --local to scan directly.")


def dashboard_report(url, scan):
    if scan:
        r = api(url, "/api/smart/scan", "POST")
        if not r.get("ok") and "already" not in r.get("message", ""):
            sys.exit(f"scan not started: {r}")
        print("Scanning via dashboard…", file=sys.stderr)
        while True:
            state = api(url, "/api/smart")
            if state["state"] != "running":
                break
            time.sleep(2)
        if state["state"] == "error":
            sys.exit(f"scan failed: {state.get('error')}")
    else:
        state = api(url, "/api/smart")
    last = state.get("last")
    if not last:
        sys.exit("The dashboard has no scan yet.")
    return last


# --------------------------------------------------------------------------

def local_store(path):
    """Open the local SQLite history, importing the old JSONL file once."""
    sys.path.insert(0, HERE)
    import smart_store
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = smart_store.connect(path)
    if os.path.exists(OLD_JSONL) and path == DEFAULT_DB:
        with open(OLD_JSONL) as f:
            n = smart_store.import_jsonl(conn, f.read(), source="cli-jsonl")
        os.rename(OLD_JSONL, OLD_JSONL + ".imported")
        print(paint(f"Imported {n} readings from {OLD_JSONL}", "DIM"), file=sys.stderr)
    return smart_store, conn


def main():
    ap = argparse.ArgumentParser(description="SMART wear report for all homelab disks.")
    ap.add_argument("--local", action="store_true",
                    help="scan over SSH from this machine instead of via the dashboard")
    ap.add_argument("--dashboard", default=DEFAULT_DASHBOARD, help="dashboard URL")
    ap.add_argument("--no-scan", action="store_true", help="dashboard mode: show the last scan only")
    ap.add_argument("--hosts", default=DEFAULT_HOSTS, help="--local: hosts file")
    ap.add_argument("--specs", default=DEFAULT_SPECS, help="--local: endurance/spec file")
    ap.add_argument("--db", default=DEFAULT_DB, help="--local: SQLite history file")
    ap.add_argument("--only", action="append", metavar="HOST", help="only this host (repeatable)")
    ap.add_argument("--no-save", action="store_true", help="--local: do not record the scan")
    ap.add_argument("--json", action="store_true", help="print analysed data as JSON")
    ap.add_argument("--history", nargs="?", const="", metavar="SERIAL",
                    help="show recorded history (optionally one serial) and exit")
    args = ap.parse_args()

    if args.history is not None:
        if args.local:
            store, conn = local_store(args.db)
            rows = store.history(conn)
        else:
            q = f"?serial={urllib.parse.quote(args.history)}" if args.history else ""
            rows = api(args.dashboard, "/api/smart/history" + q)
        if not rows:
            sys.exit("No history yet.")
        print_history(rows, args.history or None)
        return

    if args.local:
        store, conn = local_store(args.db)
        hosts = read_hosts(args.hosts)
        if args.only:
            hosts = [h for h in hosts if h[0] in args.only]
            if not hosts:
                sys.exit("No matching host in " + args.hosts)
        started = time.time()
        now, disks, host_errors = run_report(hosts, store.history(conn), read_specs(args.specs))
        if not args.no_save:
            store.add_scan(conn, now, disks, host_errors, source="cli",
                           duration_s=round(time.time() - started, 1), hosts=len(hosts))
        when, nhosts = now, len(hosts)
    else:
        last = dashboard_report(args.dashboard, scan=not args.no_scan)
        disks, host_errors = last["disks"], [tuple(e) for e in last["host_errors"]]
        if args.only:
            disks = [d for d in disks if d["host"] in args.only]
            host_errors = [e for e in host_errors if e[0] in args.only]
        when, nhosts = dt.datetime.fromisoformat(last["time"]).astimezone(), last.get("hosts")

    if args.json:
        print(json.dumps({"time": when.isoformat(timespec="seconds"), "disks": disks,
                          "host_errors": host_errors}, indent=2, ensure_ascii=False))
    else:
        src = "local SSH" if args.local else args.dashboard
        print(f"SMART report  {when:%Y-%m-%d %H:%M}  ({len(disks)} disks, {nhosts} hosts, via {src})\n")
        print_table(disks)
        print_notes(disks, host_errors)

    levels = {d.get("level") for d in disks}
    sys.exit(2 if "CRIT" in levels else 1 if ("WARN" in levels or host_errors) else 0)


if __name__ == "__main__":
    main()
