#!/usr/bin/env python3
"""On-demand SMART wear report for every disk in the homelab.

Connects to each host in hosts.conf over SSH, reads `smartctl -x -j` for
every disk, prints a wear/health table and appends a snapshot to a local
history file. Wear *rate* (GB written per day, % used per year) and the
projected remaining life come from comparing against earlier snapshots,
so the report gets more useful every time it is run.

Spun-down HDDs are skipped, not woken: smartctl runs with `-n standby`,
which keeps truenas/hdd-idle working.

Only the Python standard library is used. Usage:

    ./smart_report.py                # all hosts, save snapshot
    ./smart_report.py --only g3-worker3
    ./smart_report.py --no-save      # look without recording history
    ./smart_report.py --json         # normalized data as JSON
    ./smart_report.py --history      # per-disk history, no SSH

Full description: README.md in this directory.
"""

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HOSTS = os.path.join(HERE, "hosts.conf")
DEFAULT_ENDURANCE = os.path.join(HERE, "endurance.conf")
# Outside the repo on purpose: the history holds disk serial numbers and the
# repo is a Syncthing folder pushed to GitHub.
DEFAULT_HISTORY = os.path.expanduser("~/.local/share/smart-report/history.jsonl")

SSH_TIMEOUT = 90  # seconds per host; a busy HDD self-test can be slow

# Thresholds. Kept here, in one place, so they are easy to tune.
WARN_USED_PCT = 70
CRIT_USED_PCT = 90
WARN_YEARS_LEFT = 3
CRIT_YEARS_LEFT = 1
WARN_TEMP_SSD = 70
WARN_TEMP_HDD = 50
WARN_TBW_PCT = 80
MIN_HISTORY_DAYS = 0.8  # shorter gaps give a noisy rate
MIN_PCT_TREND_DAYS = 60  # see add_rates()
MIN_LIFETIME_POH = 720  # hours; below this a lifetime average is noise

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
  echo "@@MMC $(basename "$d")|$(cat "$d/device/name")|$(cat "$d/device/date" 2>/dev/null)|$(cat "$d/device/manfid" 2>/dev/null)|$(( $(cat "$d/size") * 512 ))"
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
                sys.exit(f"{path}: bad line (need: NAME SSH_TARGET): {line}")
            hosts.append((parts[0], parts[1]))
    return hosts


def read_endurance(path):
    """model substring -> rated TBW (terabytes written)."""
    table = []
    if not os.path.exists(path):
        return table
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or "|" not in line:
                continue
            model, tbw = line.rsplit("|", 1)
            try:
                table.append((model.strip().lower(), float(tbw)))
            except ValueError:
                sys.exit(f"{path}: TBW is not a number: {line}")
    return table


def rated_tbw(model, table):
    model = (model or "").lower()
    best = None
    for sub, tbw in table:
        if sub in model and (best is None or len(sub) > len(best[0])):
            best = (sub, tbw)
    return best[1] if best else None


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


def run_report(hosts, history, tbw_table, runner=ssh_cli_runner, now=None):
    """Collect from all hosts in parallel and analyse.
    Returns (now, disks, host_errors)."""
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
    for d in disks:
        add_rates(d, history, now, tbw_table)
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
            dev, cid_name, date, manfid, size = (line[6:].split("|") + [""] * 5)[:5]
            res["disks"].append({
                "host": name, "dev": "/dev/" + dev, "kind": "SD",
                "model": f"{cid_name} (manfid {manfid}, prod. {date})",
                "capacity": int(size) if size.isdigit() else None,
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
        d.update(kind="?", level="WARN", issues=["smartctl returned no JSON"])
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
    if rc & 0b1000000:
        d["issues"].append("WARN: errors in the device error log (exit bit 6)")
    if rc & 0b10000000:
        d["issues"].append("WARN: self-test log reports errors (exit bit 7)")
    return d


def parse_nvme(d, log):
    units = log.get("data_units_written")
    d["written"] = units * 512_000 if units is not None else None  # NVMe unit = 1000 x 512 B
    d["used_pct"] = log.get("percentage_used")
    d["unsafe_shutdowns"] = log.get("unsafe_shutdowns")
    spare, thr = log.get("available_spare"), log.get("available_spare_threshold")
    if log.get("critical_warning"):
        d["issues"].append(f"CRIT: NVMe critical_warning = {log['critical_warning']:#x}")
    if spare is not None and thr is not None and spare <= thr:
        d["issues"].append(f"CRIT: spare blocks at the threshold ({spare}% <= {thr}%)")
    elif spare is not None and spare < 100:
        d["issues"].append(f"spare blocks dropped to {spare}% (threshold {thr}%)")
    if log.get("media_errors"):
        d["issues"].append(f"WARN: media_errors = {log['media_errors']}")


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
    # 8710 h in attribute 9; WD Blue WDS250G2B0A: 0 vs 21900). An
    # undercount is the dangerous direction, because every per-hour rate is
    # then inflated, so take the larger of the two.
    if 9 in attrs and attrs[9].get("name") == "Power_On_Hours":
        s = str(attrs[9]["raw"].get("string", "")).split("h")[0].split()[0]
        if s.isdigit() and 0 < int(s) < 200_000:
            d["poh"] = max(d.get("poh") or 0, int(s))

    # Bytes written: ATA Device Statistics are standardised (sectors),
    # attribute 241 is not (sectors, 32 MiB or GiB depending on vendor).
    if devstat.get("Logical Sectors Written") is not None:
        d["written"] = devstat["Logical Sectors Written"] * lbs
        d["written_src"] = "devstat"
    elif raw(246) is not None and attrs[246].get("name") in (
            "Total_Host_Sector_Write", "Total_LBAs_Written"):  # Crucial/Micron (BX500: no devstat)
        d["written"] = raw(246) * lbs
        d["written_src"] = "attr246"
    elif raw(241) is not None:
        d["written"] = raw(241) * lbs
        d["written_src"] = "attr241?"
        d["issues"].append("bytes written from attribute 241, unit unconfirmed for this model")

    if d["kind"] != "HDD":
        if devstat.get("Percentage Used Endurance Indicator") is not None:
            d["used_pct"] = devstat["Percentage Used Endurance Indicator"]
        else:
            for a in attrs.values():
                if a.get("name") in ATA_LIFE_REMAINING_ATTRS and 0 <= a.get("value", -1) <= 100:
                    d["used_pct"] = 100 - a["value"]
                    break

    for aid, label, level in (
        (5, "reallocated sectors", "WARN"),
        (197, "pending sectors", "WARN"),
        (198, "offline uncorrectable sectors", "WARN"),
        (187, "reported uncorrectable errors", "WARN"),
        (199, "UDMA CRC errors (cable/connector)", "WARN"),
    ):
        v = raw(aid)
        if v:
            d["issues"].append(f"{level}: {label} = {v}")
    if d["kind"] == "HDD":
        d["start_stop"] = raw(4)
        d["load_cycles"] = raw(193)


# --------------------------------------------------------------------------
# History and wear rate
# --------------------------------------------------------------------------

def parse_history(text):
    rows = []
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def load_history(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return parse_history(f.read())


def history_lines(disks, now):
    """One JSONL line per disk with a serial (sleeping/SD disks have none)."""
    out = []
    for d in disks:
        if not d.get("serial"):
            continue
        out.append(json.dumps({
            "ts": now.isoformat(timespec="seconds"), "host": d["host"],
            "serial": d["serial"], "model": d.get("model"), "kind": d["kind"],
            "written": d.get("written"), "used_pct": d.get("used_pct"),
            "poh": d.get("poh"), "temp": d.get("temp"),
        }) + "\n")
    return out


def save_history(path, disks, now):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.writelines(history_lines(disks, now))


def add_rates(d, history, now, tbw_table):
    """Fill gb_per_day, years_left and the source of each estimate."""
    serial = d.get("serial")
    if not serial or d["kind"] in ("HDD", "SD"):
        return
    past = [h for h in history if h.get("serial") == serial and h.get("written") is not None]
    base = None
    for h in sorted(past, key=lambda h: h["ts"]):
        days = (now - dt.datetime.fromisoformat(h["ts"])).total_seconds() / 86400
        if days >= MIN_HISTORY_DAYS:
            base = (h, days)
            break  # oldest snapshot = longest baseline

    written, used = d.get("written"), d.get("used_pct")
    rate_src = None
    if base and written is not None:
        h, days = base
        d["gb_per_day"] = max(0.0, (written - h["written"]) / 1e9 / days)
        rate_src = f"from {days:.0f} days of history"
        d["history_days"] = days
        # "% used" moves in whole percent: one step over a short window looks
        # like a huge rate, so only trust the trend over a longer baseline.
        if (used is not None and h.get("used_pct") is not None
                and used > h["used_pct"] and days >= MIN_PCT_TREND_DAYS):
            d["pct_per_year"] = (used - h["used_pct"]) / days * 365
    elif written is not None and (d.get("poh") or 0) >= MIN_LIFETIME_POH:
        # No history yet: lifetime average per power-on hour. Only a rough
        # hint: power-on hours undercount on hosts switched off part of the
        # day, and on drives that stop the counter in low-power states
        # (seen on NVMe: Micron 2200 with 296 h after months in service).
        d["gb_per_day"] = written / 1e9 / (d["poh"] / 24)
        rate_src = "lifetime average"
    d["rate_src"] = rate_src

    estimates = []
    if d.get("pct_per_year"):
        estimates.append(((100 - used) / d["pct_per_year"], "%used trend"))
    elif used and used >= 1 and (d.get("poh") or 0) >= MIN_LIFETIME_POH:
        per_year = used / (d["poh"] / 24 / 365)
        estimates.append(((100 - used) / per_year, "%used lifetime"))
    tbw = rated_tbw(d.get("model"), tbw_table)
    if tbw:
        d["tbw"] = tbw
        if written is not None:
            d["tbw_pct"] = written / 1e12 / tbw * 100
            if d.get("gb_per_day"):
                years = (tbw * 1e12 - written) / (d["gb_per_day"] * 1e9) / 365
                src = "TBW, lifetime rate" if rate_src == "lifetime average" else "TBW"
                estimates.append((max(years, 0), src))
    if estimates:
        d["years_left"], d["years_src"] = min(estimates)
        # Only projections built on measured history may raise an alert;
        # lifetime averages are shown as "~" estimates.
        d["years_confident"] = "lifetime" not in d["years_src"]


def classify(d):
    if d.get("level") in ("SKIP", "INFO"):
        return
    issues = d["issues"]
    if d.get("used_pct") is not None:
        if d["used_pct"] >= CRIT_USED_PCT:
            issues.append(f"CRIT: wear {d['used_pct']}%")
        elif d["used_pct"] >= WARN_USED_PCT:
            issues.append(f"WARN: wear {d['used_pct']}%")
    if d.get("tbw_pct", 0) >= WARN_TBW_PCT:
        issues.append(f"WARN: {d['tbw_pct']:.0f}% of rated TBW ({d['tbw']:.0f} TB) written")
    y = d.get("years_left")
    if y is not None and not d.get("years_confident"):
        if y < WARN_YEARS_LEFT:
            issues.append(f"rough estimate only: ~{y:.1f} years ({d['years_src']}); "
                          "alerts start once there is history")
    elif y is not None:
        if y < CRIT_YEARS_LEFT:
            issues.append(f"CRIT: projected life under a year ({y:.1f}, {d['years_src']})")
        elif y < WARN_YEARS_LEFT:
            issues.append(f"WARN: projected life {y:.1f} years ({d['years_src']})")
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

def fmt_bytes(n, unit_tb=False):
    if n is None:
        return "-"
    if unit_tb or n >= 1e12:
        return f"{n / 1e12:.2f} TB"
    return f"{n / 1e9:.0f} GB"


def fmt_size(n):
    if not n:
        return "-"
    return f"{n / 1e12:.1f}T" if n >= 1e12 else f"{n / 1e9:.0f}G"


def fmt_years(d):
    y = d.get("years_left")
    if y is None:
        return "-"
    txt = ">50 y" if y > 50 else f"{y:.1f} y"
    return txt if d.get("years_confident") else "~" + txt


def print_table(disks):
    cols = [("HOST", 12), ("DEVICE", 12), ("MODEL", 26), ("TYPE", 4), ("SIZE", 5),
            ("HOURS", 6), ("WRITTEN", 9), ("WEAR", 5), ("TEMP", 4),
            ("GB/DAY", 7), ("LIFE LEFT", 9), ("STATUS", 6)]
    print("  ".join(n.ljust(w) for n, w in cols))
    print("  ".join("-" * w for _, w in cols))
    for d in disks:
        used = d.get("used_pct")
        row = [
            d["host"], d["dev"], (d.get("model") or "?")[:26], d.get("kind", "?"),
            fmt_size(d.get("capacity")),
            str(d["poh"]) if d.get("poh") is not None else "-",
            fmt_bytes(d.get("written")),
            f"{used}%" if used is not None else "-",
            f"{d['temp']}°" if d.get("temp") is not None else "-",
            f"{d['gb_per_day']:.1f}" + ("*" if d.get("rate_src") == "lifetime average" else "")
            if d.get("gb_per_day") is not None else "-",
            fmt_years(d),
            d.get("level", "?"),
        ]
        cells = [str(v).ljust(w) for v, (_, w) in zip(row, cols)]
        cells[-1] = paint(cells[-1], d.get("level"))
        line = "  ".join(cells)
        print(paint(line, "DIM") if d.get("level") in ("SKIP", "INFO") else line)


def print_notes(disks, host_errors):
    lines = []
    for d in disks:
        extra = []
        if d.get("tbw"):
            extra.append(f"{d.get('tbw_pct', 0):.1f}% of rated {d['tbw']:.0f} TBW")
        if d.get("unsafe_shutdowns"):
            extra.append(f"unsafe shutdowns: {d['unsafe_shutdowns']}")
        if d["kind"] == "HDD" and d.get("load_cycles") is not None:
            extra.append(f"start/stop {d.get('start_stop')}, load cycles {d['load_cycles']}")
        if d.get("years_src"):
            extra.append(f"projection from: {d['years_src']}")
        if d.get("rate_src") and d.get("rate_src") != "lifetime average":
            extra.append(f"rate {d['rate_src']}")
        if d["issues"] or extra:
            head = f"{d['host']} {d['dev']} {d.get('model', '')}".strip()
            lines.append(paint(head, d.get("level")))
            for i in d["issues"]:
                lines.append(f"    - {i}")
            if extra:
                lines.append("      " + "; ".join(extra))
    for h, e in host_errors:
        lines.append(paint(f"{h}: {e}", "WARN"))
    if lines:
        print("\nNotes:")
        print("\n".join(lines))
    if any(d.get("rate_src") == "lifetime average" for d in disks):
        print("\n* GB/DAY with an asterisk = lifetime average per power-on day "
              "(no history yet). The real trend appears from the second run, at least a day later.")


def print_history(history, serial_filter=None):
    by_serial = {}
    for h in history:
        by_serial.setdefault(h["serial"], []).append(h)
    for serial, rows in sorted(by_serial.items(), key=lambda kv: (kv[1][-1].get("host", ""), kv[0])):
        if serial_filter and serial_filter not in serial:
            continue
        last = rows[-1]
        print(f"\n{last.get('host', '?')}  {last.get('model')}  ({last.get('kind', '?')}, s/n …{serial[-4:]})")
        for r in rows:
            w = fmt_bytes(r.get("written"))
            u = f"{r['used_pct']}%" if r.get("used_pct") is not None else "-"
            print(f"  {r['ts'][:16]}  written {w:>9}  wear {u:>4}  hours {r.get('poh')}")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="SMART wear report for all homelab disks.")
    ap.add_argument("--hosts", default=DEFAULT_HOSTS)
    ap.add_argument("--endurance", default=DEFAULT_ENDURANCE)
    ap.add_argument("--history-file", default=DEFAULT_HISTORY)
    ap.add_argument("--only", action="append", metavar="HOST", help="only this host (repeatable)")
    ap.add_argument("--no-save", action="store_true", help="do not record a snapshot")
    ap.add_argument("--json", action="store_true", help="print normalized data as JSON")
    ap.add_argument("--history", nargs="?", const="", metavar="SERIAL",
                    help="show recorded history (optionally for one serial) and exit")
    args = ap.parse_args()

    history = load_history(args.history_file)
    if args.history is not None:
        if not history:
            sys.exit(f"No history in {args.history_file} yet.")
        print_history(history, args.history or None)
        return

    hosts = read_hosts(args.hosts)
    if args.only:
        hosts = [h for h in hosts if h[0] in args.only]
        if not hosts:
            sys.exit("No matching host in " + args.hosts)
    tbw_table = read_endurance(args.endurance)
    now, disks, host_errors = run_report(hosts, history, tbw_table)

    if args.json:
        print(json.dumps({"time": now.isoformat(timespec="seconds"), "disks": disks,
                          "host_errors": host_errors}, indent=2, ensure_ascii=False))
    else:
        print(f"SMART report  {now:%Y-%m-%d %H:%M}  ({len(disks)} disks, {len(hosts)} hosts)\n")
        print_table(disks)
        print_notes(disks, host_errors)

    if not args.no_save:
        save_history(args.history_file, disks, now)
        if not args.json:
            print(paint(f"\nSnapshot saved: {args.history_file}", "DIM"))

    levels = {d.get("level") for d in disks}
    sys.exit(2 if "CRIT" in levels else 1 if ("WARN" in levels or host_errors) else 0)


if __name__ == "__main__":
    main()
