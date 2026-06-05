import asyncio
import json
import os
import socket
import struct
import time
from contextlib import asynccontextmanager
from typing import Optional

import paramiko
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    yield

app = FastAPI(title="Homelab Dashboard API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Kubernetes clients (initialised on first use via dependency-free globals)
# ---------------------------------------------------------------------------

def _v1() -> client.CoreV1Api:
    return client.CoreV1Api()

def _apps() -> client.AppsV1Api:
    return client.AppsV1Api()

def _custom() -> client.CustomObjectsApi:
    return client.CustomObjectsApi()

# ---------------------------------------------------------------------------
# Config from env (set by ConfigMap in deployment)
# ---------------------------------------------------------------------------

G3_MAC       = os.getenv("G3_MAC", "G3_MAC_ADDRESS_PLACEHOLDER")
G3_HOST      = os.getenv("G3_HOST", "192.168.50.13")
G3_USER      = os.getenv("G3_USER", "buth11")
G3_NODE_NAME = os.getenv("G3_NODE_NAME", "g3-worker3")
SSH_KEY_PATH = "/root/.ssh/id_rsa"
DASHBOARD_NS = "dashboard"
CUSTOM_ACTIONS_CM = "custom-actions"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_cpu_to_millicores(cpu: str) -> int:
    if not cpu:
        return 0
    if cpu.endswith("n"):
        return int(cpu[:-1]) // 1_000_000
    if cpu.endswith("m"):
        return int(cpu[:-1])
    return int(float(cpu) * 1000)

def _parse_mem_to_mib(mem: str) -> int:
    if not mem:
        return 0
    if mem.endswith("Ki"):
        return int(mem[:-2]) // 1024
    if mem.endswith("Mi"):
        return int(mem[:-2])
    if mem.endswith("Gi"):
        return int(mem[:-2]) * 1024
    if mem.endswith("Ti"):
        return int(mem[:-2]) * 1024 * 1024
    return int(mem) // (1024 * 1024)

def _age_str(start_time) -> str:
    if not start_time:
        return "-"
    delta = int(time.time() - start_time.timestamp())
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h{(delta % 3600) // 60}m"
    return f"{delta // 86400}d"

def _send_wol(mac: str) -> None:
    mac_clean = mac.replace(":", "").replace("-", "")
    if len(mac_clean) != 12:
        raise ValueError(f"Invalid MAC address: {mac}")
    mac_bytes = bytes.fromhex(mac_clean)
    magic = b"\xff" * 6 + mac_bytes * 16
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.sendto(magic, ("<broadcast>", 9))
    sock.close()

def _ssh_exec(host: str, user: str, command: str, key_path: str = SSH_KEY_PATH) -> dict:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, username=user, key_filename=key_path, timeout=10)
        _, stdout, stderr = ssh.exec_command(command, timeout=30)
        rc = stdout.channel.recv_exit_status()
        return {
            "stdout": stdout.read().decode(errors="replace"),
            "stderr": stderr.read().decode(errors="replace"),
            "returncode": rc,
        }
    finally:
        ssh.close()

# ---------------------------------------------------------------------------
# Node-level drain (cordon + evict)
# ---------------------------------------------------------------------------

def _drain_node(node_name: str) -> dict:
    v1 = _v1()
    # Cordon
    v1.patch_node(node_name, {"spec": {"unschedulable": True}})
    pod_list = v1.list_pod_for_all_namespaces(
        field_selector=f"spec.nodeName={node_name}"
    )
    evicted, skipped, errors = [], [], []
    for pod in pod_list.items:
        ns = pod.metadata.namespace
        name = pod.metadata.name
        owners = pod.metadata.owner_references or []
        owner_kinds = [o.kind for o in owners]
        if "DaemonSet" in owner_kinds or "Node" in owner_kinds:
            skipped.append(f"{ns}/{name}")
            continue
        try:
            eviction = client.V1Eviction(
                metadata=client.V1ObjectMeta(name=name, namespace=ns)
            )
            v1.create_namespaced_pod_eviction(name=name, namespace=ns, body=eviction)
            evicted.append(f"{ns}/{name}")
        except Exception as exc:
            errors.append(f"{ns}/{name}: {exc}")
    return {"evicted": evicted, "skipped": skipped, "errors": errors}

# ---------------------------------------------------------------------------
# Background task: start media
# ---------------------------------------------------------------------------

_start_media_status: dict = {"state": "idle", "log": []}

async def _start_media_bg():
    global _start_media_status
    log = []
    _start_media_status = {"state": "running", "log": log}
    try:
        log.append("Sending WoL packet…")
        _send_wol(G3_MAC)
        log.append(f"WoL sent to {G3_MAC}. Waiting for node to be Ready…")

        deadline = time.time() + 300  # 5-minute timeout
        ready = False
        while time.time() < deadline:
            await asyncio.sleep(10)
            try:
                node = _v1().read_node(G3_NODE_NAME)
                for cond in node.status.conditions:
                    if cond.type == "Ready" and cond.status == "True":
                        ready = True
                        break
                if ready:
                    log.append(f"Node {G3_NODE_NAME} is Ready.")
                    break
                else:
                    log.append(f"Node {G3_NODE_NAME} not yet Ready, retrying…")
            except Exception:
                log.append("Node not visible yet, retrying…")

        if not ready:
            log.append("Timeout waiting for node — aborting scale-up.")
            _start_media_status["state"] = "error"
            return

        # Uncordon
        try:
            _v1().patch_node(G3_NODE_NAME, {"spec": {"unschedulable": False}})
            log.append(f"Node {G3_NODE_NAME} uncordoned.")
        except Exception as exc:
            log.append(f"Uncordon warning: {exc}")

        for ns, dep in [("qbittorrent", "qbittorrent"), ("jellyfin", "jellyfin")]:
            try:
                _apps().patch_namespaced_deployment_scale(
                    dep, ns, {"spec": {"replicas": 1}}
                )
                log.append(f"Scaled up {ns}/{dep}.")
            except Exception as exc:
                log.append(f"Error scaling {ns}/{dep}: {exc}")

        _start_media_status["state"] = "done"
    except Exception as exc:
        log.append(f"Fatal: {exc}")
        _start_media_status["state"] = "error"

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True}

# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

@app.get("/api/nodes")
def get_nodes():
    v1 = _v1()
    nodes = v1.list_node()
    metrics_map: dict = {}
    try:
        nm = _custom().list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")
        for item in nm.get("items", []):
            metrics_map[item["metadata"]["name"]] = item.get("usage", {})
    except Exception:
        pass

    result = []
    for node in nodes.items:
        ready = False
        for cond in node.status.conditions:
            if cond.type == "Ready":
                ready = cond.status == "True"

        ip = next(
            (a.address for a in node.status.addresses if a.type == "InternalIP"), ""
        )
        labels = node.metadata.labels or {}
        if any(k in labels for k in ("node-role.kubernetes.io/master", "node-role.kubernetes.io/control-plane")):
            role = "master"
        elif "node-role.kubernetes.io/worker" in labels:
            role = "worker"
        else:
            role = "worker"

        usage = metrics_map.get(node.metadata.name, {})
        alloc = node.status.allocatable or {}

        cpu_used = _parse_cpu_to_millicores(usage.get("cpu", ""))
        cpu_total = _parse_cpu_to_millicores(alloc.get("cpu", ""))
        mem_used = _parse_mem_to_mib(usage.get("memory", ""))
        mem_total = _parse_mem_to_mib(alloc.get("memory", ""))

        result.append({
            "name": node.metadata.name,
            "status": "Ready" if ready else "NotReady",
            "ip": ip,
            "role": role,
            "version": node.status.node_info.kubelet_version,
            "cpu_used_m": cpu_used,
            "cpu_total_m": cpu_total,
            "cpu_pct": round(cpu_used / cpu_total * 100, 1) if cpu_total else None,
            "mem_used_mi": mem_used,
            "mem_total_mi": mem_total,
            "mem_pct": round(mem_used / mem_total * 100, 1) if mem_total else None,
            "unschedulable": bool(node.spec.unschedulable),
        })
    return result

# ---------------------------------------------------------------------------
# Pods
# ---------------------------------------------------------------------------

@app.get("/api/pods")
def get_pods(namespace: Optional[str] = None):
    v1 = _v1()
    if namespace:
        pods = v1.list_namespaced_pod(namespace)
    else:
        pods = v1.list_pod_for_all_namespaces()

    result = []
    for pod in pods.items:
        containers = pod.status.container_statuses or []
        restarts = sum(c.restart_count for c in containers)

        phase = pod.status.phase or "Unknown"
        reason = pod.status.reason or ""
        # Detect crash-loop and OOMKilled
        for cs in containers:
            if cs.state and cs.state.waiting:
                w = cs.state.waiting
                if w.reason in ("CrashLoopBackOff", "OOMKilled", "Error"):
                    phase = w.reason
                    break
            if cs.state and cs.state.terminated:
                t = cs.state.terminated
                if t.reason in ("OOMKilled", "Error"):
                    phase = t.reason

        result.append({
            "namespace": pod.metadata.namespace,
            "name": pod.metadata.name,
            "status": reason or phase,
            "ip": pod.status.pod_ip or "",
            "node": pod.spec.node_name or "",
            "restarts": restarts,
            "age": _age_str(pod.status.start_time),
            "ready": f"{sum(1 for c in containers if c.ready)}/{len(containers)}" if containers else "0/0",
            "containers": [c.name for c in (pod.spec.containers or [])],
        })
    return result

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

@app.get("/api/services")
def get_services():
    v1 = _v1()
    svcs = v1.list_service_for_all_namespaces()

    # Ports that are NOT web UIs — skip URL generation for these
    NON_WEB_PORTS = {53, 67, 68, 9100, 9090}

    # Force a specific port and/or protocol for named services
    # key = substring matched against service name (lowercase)
    WEB_PORT_OVERRIDE = {
        "firefox": (3001, "https"),
    }

    result = []
    for svc in svcs.items:
        external_ips = svc.status.load_balancer.ingress or [] if svc.status.load_balancer else []
        ext_ip = external_ips[0].ip if external_ips else ""
        ports = [
            {"port": p.port, "target": p.target_port, "protocol": p.protocol}
            for p in (svc.spec.ports or [])
        ]

        # Pick the best web-UI port
        url = ""
        if ext_ip and ports:
            svc_name_lower = svc.metadata.name.lower()

            # Check explicit override first
            web_port = None
            scheme = "http"
            for pattern, (forced_port, forced_scheme) in WEB_PORT_OVERRIDE.items():
                if pattern in svc_name_lower:
                    if any(p["port"] == forced_port for p in ports):
                        web_port = forced_port
                        scheme = forced_scheme
                    break

            # Fall back to first TCP port that is not a known non-web port
            if web_port is None:
                for p_info in ports:
                    if p_info["protocol"] != "UDP" and p_info["port"] not in NON_WEB_PORTS:
                        web_port = p_info["port"]
                        break

            if web_port is not None:
                url = f"{scheme}://{ext_ip}:{web_port}" if web_port != 80 else f"{scheme}://{ext_ip}"

        result.append({
            "namespace": svc.metadata.namespace,
            "name": svc.metadata.name,
            "type": svc.spec.type,
            "cluster_ip": svc.spec.cluster_ip,
            "external_ip": ext_ip,
            "ports": ports,
            "url": url,
        })
    return result

# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

@app.post("/api/action/scale/{namespace}/{deployment}/{replicas}")
def scale_deployment(namespace: str, deployment: str, replicas: int):
    if replicas < 0 or replicas > 10:
        raise HTTPException(400, "replicas must be 0–10")
    try:
        _apps().patch_namespaced_deployment_scale(
            deployment, namespace, {"spec": {"replicas": replicas}}
        )
        return {"ok": True, "namespace": namespace, "deployment": deployment, "replicas": replicas}
    except ApiException as exc:
        raise HTTPException(exc.status, detail=exc.reason)

# ---------------------------------------------------------------------------
# Drain / Uncordon
# ---------------------------------------------------------------------------

@app.post("/api/action/drain/{node}")
def drain_node(node: str):
    try:
        result = _drain_node(node)
        return {"ok": True, **result}
    except Exception as exc:
        raise HTTPException(500, str(exc))

@app.post("/api/action/uncordon/{node}")
def uncordon_node(node: str):
    try:
        _v1().patch_node(node, {"spec": {"unschedulable": False}})
        return {"ok": True, "node": node}
    except Exception as exc:
        raise HTTPException(500, str(exc))

# ---------------------------------------------------------------------------
# Wake-on-LAN
# ---------------------------------------------------------------------------

@app.post("/api/action/wol/{mac}")
def wake_on_lan(mac: str):
    try:
        _send_wol(mac)
        return {"ok": True, "mac": mac}
    except Exception as exc:
        raise HTTPException(500, str(exc))

# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------

class SSHBody(BaseModel):
    command: str
    user: str = G3_USER

@app.post("/api/action/ssh/{host}")
def ssh_command(host: str, body: SSHBody):
    try:
        result = _ssh_exec(host, body.user, body.command)
        return {"ok": True, **result}
    except Exception as exc:
        raise HTTPException(500, str(exc))

# ---------------------------------------------------------------------------
# Stop Media (scale down + drain + shutdown)
# ---------------------------------------------------------------------------

@app.post("/api/action/stop-media")
async def stop_media():
    results = {}

    for ns, dep in [("qbittorrent", "qbittorrent"), ("jellyfin", "jellyfin")]:
        try:
            _apps().patch_namespaced_deployment_scale(dep, ns, {"spec": {"replicas": 0}})
            results[f"scale_{dep}"] = "scaled to 0"
        except Exception as exc:
            results[f"scale_{dep}"] = f"error: {exc}"

    # Brief wait for pods to begin terminating
    await asyncio.sleep(5)

    try:
        drain_result = _drain_node(G3_NODE_NAME)
        results["drain"] = drain_result
    except Exception as exc:
        results["drain"] = f"error: {exc}"

    try:
        ssh_result = _ssh_exec(G3_HOST, G3_USER, "sudo shutdown -h now")
        results["ssh_shutdown"] = ssh_result
    except Exception as exc:
        results["ssh_shutdown"] = f"error: {exc}"

    return {"ok": True, "results": results}

# ---------------------------------------------------------------------------
# Start Media (WoL + wait + scale up)
# ---------------------------------------------------------------------------

@app.post("/api/action/start-media")
async def start_media(background_tasks: BackgroundTasks):
    global _start_media_status
    if _start_media_status.get("state") == "running":
        return {"ok": False, "message": "Start-media already running"}
    _start_media_status = {"state": "starting", "log": []}
    background_tasks.add_task(_start_media_bg)
    return {"ok": True, "message": "WoL sequence started"}

@app.get("/api/action/start-media/status")
def start_media_status():
    return _start_media_status

# ---------------------------------------------------------------------------
# Restart pod (delete → k8s recreates it)
# ---------------------------------------------------------------------------

@app.post("/api/action/restart/{namespace}/{pod}")
def restart_pod(namespace: str, pod: str):
    try:
        _v1().delete_namespaced_pod(pod, namespace)
        return {"ok": True, "message": f"Pod {namespace}/{pod} deleted, will be recreated"}
    except ApiException as exc:
        raise HTTPException(exc.status, exc.reason)

# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@app.get("/api/logs/{namespace}/{pod}")
def get_logs(namespace: str, pod: str, lines: int = 50, container: Optional[str] = None):
    try:
        logs = _v1().read_namespaced_pod_log(
            name=pod,
            namespace=namespace,
            tail_lines=lines,
            container=container,
        )
        return {"logs": logs, "pod": pod, "namespace": namespace}
    except ApiException as exc:
        raise HTTPException(exc.status, exc.reason)

# ---------------------------------------------------------------------------
# Custom actions (stored in ConfigMap)
# ---------------------------------------------------------------------------

class CustomAction(BaseModel):
    id: str
    name: str
    icon: str
    command: str
    type: str   # "kubectl" | "ssh" | "bash"
    host: Optional[str] = None
    confirm: bool = True

@app.get("/api/custom-actions")
def get_custom_actions():
    try:
        cm = _v1().read_namespaced_config_map(CUSTOM_ACTIONS_CM, DASHBOARD_NS)
        return json.loads(cm.data.get("actions", "[]"))
    except ApiException:
        return []

@app.post("/api/custom-actions")
def save_custom_action(action: CustomAction):
    try:
        try:
            cm = _v1().read_namespaced_config_map(CUSTOM_ACTIONS_CM, DASHBOARD_NS)
            actions = json.loads(cm.data.get("actions", "[]"))
        except ApiException:
            actions = []

        actions = [a for a in actions if a.get("id") != action.id]
        actions.append(action.model_dump())

        body = {"data": {"actions": json.dumps(actions)}}
        try:
            _v1().patch_namespaced_config_map(CUSTOM_ACTIONS_CM, DASHBOARD_NS, body)
        except ApiException:
            full = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(name=CUSTOM_ACTIONS_CM, namespace=DASHBOARD_NS),
                data={"actions": json.dumps(actions)},
            )
            _v1().create_namespaced_config_map(DASHBOARD_NS, full)

        return {"ok": True}
    except Exception as exc:
        raise HTTPException(500, str(exc))

@app.delete("/api/custom-actions/{action_id}")
def delete_custom_action(action_id: str):
    try:
        cm = _v1().read_namespaced_config_map(CUSTOM_ACTIONS_CM, DASHBOARD_NS)
        actions = json.loads(cm.data.get("actions", "[]"))
        actions = [a for a in actions if a.get("id") != action_id]
        _v1().patch_namespaced_config_map(
            CUSTOM_ACTIONS_CM, DASHBOARD_NS, {"data": {"actions": json.dumps(actions)}}
        )
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(500, str(exc))

@app.post("/api/custom-actions/{action_id}/run")
def run_custom_action(action_id: str):
    actions = get_custom_actions()
    action = next((a for a in actions if a.get("id") == action_id), None)
    if not action:
        raise HTTPException(404, "Action not found")

    action_type = action.get("type", "bash")
    command = action.get("command", "")
    host = action.get("host", G3_HOST)

    try:
        if action_type == "ssh":
            result = _ssh_exec(host, G3_USER, command)
        elif action_type in ("kubectl", "bash"):
            import subprocess
            proc = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=30
            )
            result = {"stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode}
        else:
            raise HTTPException(400, f"Unknown action type: {action_type}")
        return {"ok": True, **result}
    except Exception as exc:
        raise HTTPException(500, str(exc))
