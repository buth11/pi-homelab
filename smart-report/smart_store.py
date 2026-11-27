"""SQLite storage for SMART scans.

Used by the dashboard backend (the main, long-term store: a local-path PVC on
pi4-master) and by the CLI in --local mode (~/.local/share/smart-report/).
SQLite must live on a local filesystem, never on SMB (no locking for WAL;
see CLAUDE.md).

Schema: one row per scan in `scans` (with the full analysed result as JSON, so
the dashboard can show the last scan without recomputing), one row per disk
per scan in `readings` (the raw counters the trend analysis needs).
"""

import datetime as dt
import json
import sqlite3

# Counters stored per disk per scan. Order matters only for the INSERT.
READING_FIELDS = (
    "host", "dev", "serial", "model", "kind", "written", "read", "used_pct",
    "poh", "temp", "start_stop", "load_cycles", "realloc", "pending",
    "offline_unc", "reported_unc", "crc", "media_errors", "err_log",
    "unsafe_shutdowns", "level",
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    source      TEXT NOT NULL,          -- dashboard | cli | import
    duration_s  REAL,
    hosts       INTEGER,
    host_errors TEXT,                   -- JSON list of [host, message]
    result      TEXT                    -- JSON list of analysed disks
);
CREATE TABLE IF NOT EXISTS readings (
    id      INTEGER PRIMARY KEY,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    ts      TEXT NOT NULL,
    {", ".join(f + (" INTEGER" if f not in ("host", "dev", "serial", "model", "kind", "level") else " TEXT")
               for f in READING_FIELDS)}
);
CREATE INDEX IF NOT EXISTS readings_serial_ts ON readings(serial, ts);
CREATE UNIQUE INDEX IF NOT EXISTS readings_serial_ts_unique ON readings(serial, ts);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);  -- e.g. last backup status
"""


def utc(ts):
    """Timestamps are stored in UTC so that ORDER BY ts (string order) is
    chronological; mixing +01:00 (laptop) and +00:00 (pod) would not be."""
    if isinstance(ts, str):
        ts = dt.datetime.fromisoformat(ts)
    return ts.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def connect(path):
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def add_scan(conn, now, disks, host_errors, source, duration_s=None, hosts=None):
    """Store one scan. Disks without a serial (sleeping HDDs, SD cards) are
    kept in the result JSON but get no reading row."""
    ts = utc(now)
    with conn:
        cur = conn.execute(
            "INSERT INTO scans (ts, source, duration_s, hosts, host_errors, result) VALUES (?,?,?,?,?,?)",
            (ts, source, duration_s, hosts, json.dumps(host_errors), json.dumps(disks, ensure_ascii=False)))
        scan_id = cur.lastrowid
        for d in disks:
            if not d.get("serial"):
                continue
            conn.execute(
                f"INSERT OR IGNORE INTO readings (scan_id, ts, {', '.join(READING_FIELDS)}) "
                f"VALUES (?, ?, {', '.join('?' * len(READING_FIELDS))})",
                (scan_id, ts, *[d.get(f) for f in READING_FIELDS]))
    return scan_id


def history(conn, serial=None):
    """Readings as dicts, oldest first: what smart_report.analyse() expects."""
    q = "SELECT * FROM readings"
    args = ()
    if serial:
        q += " WHERE serial = ?"
        args = (serial,)
    rows = conn.execute(q + " ORDER BY ts", args).fetchall()
    return [{k: r[k] for k in r.keys() if k not in ("id", "scan_id")} for r in rows]


def last_scan(conn):
    r = conn.execute("SELECT * FROM scans WHERE result IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
    if not r:
        return None
    return {"time": r["ts"], "source": r["source"], "duration_s": r["duration_s"],
            "hosts": r["hosts"], "host_errors": json.loads(r["host_errors"] or "[]"),
            "disks": json.loads(r["result"])}


def import_jsonl(conn, text, source="import"):
    """Import the old JSONL history (ConfigMap / CLI file). Each distinct
    timestamp becomes one scan without a stored result; duplicates
    (same serial and timestamp) are skipped. Returns rows imported."""
    by_ts = {}
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("serial") and row.get("ts"):
            by_ts.setdefault(utc(row["ts"]), []).append(row)
    added = 0
    with conn:
        for ts, rows in sorted(by_ts.items()):
            cur = conn.execute("INSERT INTO scans (ts, source) VALUES (?, ?)", (ts, source))
            for row in rows:
                c = conn.execute(
                    f"INSERT OR IGNORE INTO readings (scan_id, ts, {', '.join(READING_FIELDS)}) "
                    f"VALUES (?, ?, {', '.join('?' * len(READING_FIELDS))})",
                    (cur.lastrowid, ts, *[row.get(f) for f in READING_FIELDS]))
                added += c.rowcount
    return added


def get_meta(conn, key, default=None):
    r = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return json.loads(r["v"]) if r else default


def set_meta(conn, key, value):
    with conn:
        conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                     (key, json.dumps(value)))


def backup_to(conn, path):
    """Consistent copy of the live database (safe while it is in use)."""
    conn.execute("VACUUM INTO ?", (path,))
