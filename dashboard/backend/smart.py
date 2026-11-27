"""Disks tab: on-demand SMART wear report.

Reuses smart-report/smart_report.py (copied into the image at build time,
see the Dockerfile) so the CLI and the dashboard compute exactly the same
numbers. SSH goes through paramiko with the dashboard's own key.

History and the last full result live in the `smart-history` ConfigMap in
the dashboard namespace, not on a PVC. The backend must keep running when
TrueNAS/SMB is down, because this dashboard is what wakes g3-worker3, and it
must not be pinned to one node. The backend's ServiceAccount can already
write ConfigMaps (custom-actions uses the same mechanism).
"""

import json
import os
import threading
import time
from typing import Optional

import paramiko
from fastapi import APIRouter, BackgroundTasks
from kubernetes import client
from kubernetes.client.rest import ApiException

import smart_report as sr

router = APIRouter(prefix="/api/smart")

NAMESPACE = "dashboard"
CONFIGMAP = "smart-history"
SSH_KEY_PATH = os.getenv("SSH_KEY_PATH", "/root/.ssh/id_rsa")
HOSTS_FILE = os.getenv("SMART_HOSTS", "/app/smart/hosts.conf")
ENDURANCE_FILE = os.getenv("SMART_ENDURANCE", "/app/smart/endurance.conf")
# A ConfigMap is capped at 1 MiB. One run is ~2 KB, so this keeps a few
# hundred runs; the oldest lines are dropped first.
MAX_HISTORY_BYTES = 800_000


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
# ConfigMap storage
# ---------------------------------------------------------------------------

def _load():
    try:
        cm = client.CoreV1Api().read_namespaced_config_map(CONFIGMAP, NAMESPACE)
    except ApiException as exc:
        if exc.status == 404:
            return "", None
        raise
    data = cm.data or {}
    last = json.loads(data["last.json"]) if data.get("last.json") else None
    return data.get("history.jsonl", ""), last


def _trim(history: str) -> str:
    lines = history.splitlines(keepends=True)
    size = sum(len(l.encode()) for l in lines)
    while lines and size > MAX_HISTORY_BYTES:
        size -= len(lines.pop(0).encode())
    return "".join(lines)


def _save(history: str, last: dict):
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=CONFIGMAP, namespace=NAMESPACE,
                                     labels={"app": "dashboard-backend"}),
        data={"history.jsonl": _trim(history),
              "last.json": json.dumps(last, ensure_ascii=False)},
    )
    v1 = client.CoreV1Api()
    try:
        v1.replace_namespaced_config_map(CONFIGMAP, NAMESPACE, body)
    except ApiException as exc:
        if exc.status != 404:
            raise
        v1.create_namespaced_config_map(NAMESPACE, body)


# ---------------------------------------------------------------------------
# Scan (background) and API
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_status = {"state": "idle", "started": None, "error": None}


def _scan():
    try:
        hosts = sr.read_hosts(HOSTS_FILE)
        tbw_table = sr.read_endurance(ENDURANCE_FILE)
        history, _ = _load()
        now, disks, host_errors = sr.run_report(
            hosts, sr.parse_history(history), tbw_table, runner=paramiko_runner)
        last = {"time": now.isoformat(timespec="seconds"), "hosts": len(hosts),
                "disks": disks, "host_errors": host_errors,
                "duration_s": round(time.time() - _status["started"], 1)}
        _save(history + "".join(sr.history_lines(disks, now)), last)
        _status.update(state="done", error=None)
    except Exception as exc:  # surfaced in the UI instead of a silent dead task
        _status.update(state="error", error=f"{type(exc).__name__}: {exc}")


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
    """Scan status plus the last stored result (survives backend restarts)."""
    try:
        _, last = _load()
    except ApiException as exc:
        return {**_status, "last": None, "error": f"ConfigMap read failed: {exc.reason}"}
    return {**_status, "last": last}


@router.get("/history")
def get_history(serial: Optional[str] = None):
    history, _ = _load()
    rows = sr.parse_history(history)
    if serial:
        rows = [r for r in rows if r.get("serial") == serial]
    return rows
