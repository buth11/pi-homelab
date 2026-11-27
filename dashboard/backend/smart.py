"""Disks tab: on-demand SMART wear report.

Reuses smart-report/smart_report.py and smart_store.py (copied into the
image at build time, see the Dockerfile), so the CLI and the dashboard
compute exactly the same numbers. SSH goes through paramiko with the
dashboard's own key.

History lives in SQLite on a local-path PVC (smart-data, /data) on
pi4-master, which is why this Deployment is pinned there. After every scan
the database is copied to MinIO (best effort: MinIO runs on g3-worker3,
which is switched off part of the time; a failed copy is retried on the
next scan and shown in the UI).
"""

import datetime as dt
import hashlib
import hmac
import os
import tempfile
import threading
import time
import urllib.parse
from typing import Optional

import httpx
import paramiko
from fastapi import APIRouter, BackgroundTasks

import smart_report as sr
import smart_store as store

router = APIRouter(prefix="/api/smart")

SSH_KEY_PATH = os.getenv("SSH_KEY_PATH", "/root/.ssh/id_rsa")
HOSTS_FILE = os.getenv("SMART_HOSTS", "/app/smart/hosts.conf")
SPECS_FILE = os.getenv("SMART_SPECS", "/app/smart/endurance.conf")
DB_PATH = os.getenv("SMART_DB", "/data/smart.db")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "")          # e.g. http://minio.minio.svc:9000
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "smart-report")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")


def _db():
    return store.connect(DB_PATH)


def paramiko_runner(target: str, script: str):
    user, host = target.split("@", 1)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # same as the rest of the backend
    try:
        ssh.connect(host, username=user, key_filename=SSH_KEY_PATH, timeout=8,
                    look_for_keys=False, allow_agent=False)
        stdin, stdout, stderr = ssh.exec_command("bash -s", timeout=sr.SSH_TIMEOUT)
        stdin.write(script)
        stdin.channel.shutdown_write()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace").strip().splitlines()
        return out, err[-1] if err else ""
    finally:
        ssh.close()


# ---------------------------------------------------------------------------
# MinIO backup (S3 PUT, SigV4, no extra dependency)
# ---------------------------------------------------------------------------

def _s3_put(key: str, data: bytes):
    now = dt.datetime.now(dt.timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    host = urllib.parse.urlparse(MINIO_ENDPOINT).netloc
    uri = f"/{MINIO_BUCKET}/{urllib.parse.quote(key)}"
    payload_hash = hashlib.sha256(data).hexdigest()
    signed = "host;x-amz-content-sha256;x-amz-date"
    canonical = "\n".join(["PUT", uri, "",
                           f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n",
                           signed, payload_hash])
    scope = f"{day}/us-east-1/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    k = ("AWS4" + MINIO_SECRET_KEY).encode()
    for part in (day, "us-east-1", "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    r = httpx.put(MINIO_ENDPOINT.rstrip("/") + uri, content=data, timeout=10, headers={
        "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash,
        "Authorization": f"AWS4-HMAC-SHA256 Credential={MINIO_ACCESS_KEY}/{scope}, "
                         f"SignedHeaders={signed}, Signature={sig}"})
    r.raise_for_status()


def _backup(conn):
    if not (MINIO_ENDPOINT and MINIO_ACCESS_KEY and MINIO_SECRET_KEY):
        store.set_meta(conn, "backup", {"ok": False, "at": None, "error": "MinIO not configured"})
        return
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "smart.db")
            store.backup_to(conn, path)
            with open(path, "rb") as f:
                data = f.read()
        _s3_put("smart.db", data)                                  # latest
        _s3_put(f"daily/smart-{now:%Y-%m-%d}.db", data)            # one per day
        at = now.isoformat(timespec="seconds")
        store.set_meta(conn, "backup", {"ok": True, "at": at, "last_ok": at,
                                        "error": None, "bytes": len(data)})
    except Exception as exc:  # MinIO down (g3 off) is expected now and then
        prev = store.get_meta(conn, "backup", {}) or {}
        store.set_meta(conn, "backup", {"ok": False, "at": now.isoformat(timespec="seconds"),
                                        "last_ok": prev.get("last_ok"),
                                        "error": f"{type(exc).__name__}: {exc}"[:300]})


# ---------------------------------------------------------------------------
# Scan (background) and API
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_status = {"state": "idle", "started": None, "error": None}


def _scan():
    conn = None
    try:
        conn = _db()
        hosts = sr.read_hosts(HOSTS_FILE)
        specs = sr.read_specs(SPECS_FILE)
        now, disks, host_errors = sr.run_report(
            hosts, store.history(conn), specs, runner=paramiko_runner)
        store.add_scan(conn, now, disks, host_errors, source="dashboard",
                       duration_s=round(time.time() - _status["started"], 1), hosts=len(hosts))
        _status.update(state="done", error=None)
        _backup(conn)
    except Exception as exc:  # surfaced in the UI instead of a silent dead task
        _status.update(state="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        if conn:
            conn.close()


@router.post("/scan")
def start_scan(background_tasks: BackgroundTasks):
    with _lock:
        if _status["state"] == "running":
            return {"ok": False, "message": "A scan is already running"}
        _status.update(state="running", started=time.time(), error=None)
    background_tasks.add_task(_scan)
    return {"ok": True, "message": "Scan started"}


@router.get("")
def get_report():
    """Scan status, the last stored result and the backup status."""
    conn = _db()
    try:
        return {**_status, "last": store.last_scan(conn),
                "backup": store.get_meta(conn, "backup")}
    finally:
        conn.close()


@router.get("/history")
def get_history(serial: Optional[str] = None):
    conn = _db()
    try:
        return store.history(conn, serial)
    finally:
        conn.close()
