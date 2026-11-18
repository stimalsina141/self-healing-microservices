import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from time import perf_counter, sleep, time
from urllib.request import urlopen
from uuid import UUID, uuid4

import docker
import psycopg
from flask import Flask, Response, jsonify, request
from redis import Redis
from redis.exceptions import RedisError

app = Flask(__name__)
log = logging.getLogger("seminar")
lock = Lock()
directory = Path("/data")
cache = Redis(host="cache", socket_timeout=0.85, socket_connect_timeout=0.6)
scenarios = {
    "container-crash": ("web", "ServiceDown"),
    "network-slowdown": ("cache", "HighLatency"),
    "memory-exhaustion": ("worker", "HighMemory"),
}
services = {"web", "cache", "worker", "db", "broker"}
docker_client = docker.from_env(timeout=12)
project = os.environ["COMPOSE_PROJECT"]
last_attempt = {}
active = None


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds")


def save(trial):
    path = directory / (trial["trial_id"] + ".json")
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(trial, indent=2) + "\n")
    temp.replace(path)


def record(trial, event, **details):
    trial["events"].append({"time": utc(time()), "event": event, **details})
    save(trial)
    log.info("%s %s %s", trial["trial_id"], event, details)


def container_for(service):
    if service not in services:
        raise ValueError("Unknown lab service")
    containers = docker_client.containers.list(all=True, filters={"label": [
        f"com.docker.compose.project={project}", f"com.docker.compose.service={service}",
        "com.docker.compose.oneoff=False",
    ]})
    if len(containers) != 1:
        raise RuntimeError(f"Expected one Compose container for {service}, found {len(containers)}")
    return containers[0]


def fetch(url):
    with urlopen(url, timeout=2) as response:
        return json.load(response)


def probe(service, confirm=False):
    started = perf_counter()
    memory = None
    try:
        if service == "web":
            fetch("http://web:8000/health" if confirm else "http://web:8000/")
        elif service == "cache":
            cache.ping()
        elif service == "broker":
            Redis(host="broker", socket_timeout=0.8, socket_connect_timeout=0.6).ping()
        elif service == "worker":
            result = fetch("http://worker:8001/health")
            memory = result["memory_ratio"]
        elif service == "db":
            with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=2) as conn:
                conn.execute("SELECT 1")
    except (OSError, RedisError, psycopg.Error, ValueError):
        return False, perf_counter() - started, memory
    return True, perf_counter() - started, memory


def healthy(service):
    up, latency, memory = probe(service, confirm=True)
    if service == "cache":
        return up and latency <= 0.15
    if service == "worker":
        return up and memory is not None and memory <= 0.90
    return up


@app.get("/metrics")
def metrics():
    lines = []
    for service in sorted(services):
        up, latency, memory = probe(service)
        label = '{service="' + service + '"}'
        lines.append(f"lab_service_up{label} {int(up)}")
        if service == "cache":
            # A timeout during packet loss is also an impaired cache response.
            lines.append(f"lab_cache_probe_duration_seconds{label} {latency if up else 1}")
        if memory is not None:
            lines.append(f"lab_container_memory_ratio{label} {memory}")
    return Response("\n".join(lines) + "\n", mimetype="text/plain")


def checked_exec(container, command):
    result = container.exec_run(command, user="root")
    if result.exit_code:
        raise RuntimeError(result.output.decode(errors="replace").strip() or str(command))
    return result.output


def inject(trial, container):
    fault = trial["fault_type"]
    duration = str(trial["duration"])
    if fault == "container-crash":
        container.kill(signal="SIGKILL")
    elif fault == "network-slowdown":
        checked_exec(container, ["tc", "qdisc", "add", "dev", "eth0", "root",
                                 "netem", "delay", "200ms", "loss", "5%"])
        try:
            container.exec_run(
                ["sh", "-c", 'sleep "$1"; tc qdisc del dev eth0 root', "cleanup", duration],
                user="root", detach=True,
            )
        except docker.errors.DockerException:
            checked_exec(container, ["tc", "qdisc", "del", "dev", "eth0", "root"])
            raise
    else:
        output = checked_exec(container, ["sh", "-c",
                          "cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.max"])
        current, limit = map(int, output.split())
        workload = int(0.94 * limit) - current
        if workload < 16 * 1024 * 1024:
            raise RuntimeError("Worker has insufficient memory headroom for a controlled trial")
        container.exec_run(
            ["stress-ng", "--vm", "1", "--vm-bytes", str(workload), "--vm-keep",
             "--timeout", duration], user="root", detach=True,
        )


def finish(trial, status, recovered=None):
    global active
    trial["status"] = status
    if recovered is not None:
        trial["t_recover"] = utc(recovered)
        trial["mttr_sec"] = round(recovered - trial["failure_epoch"], 3)
    record(trial, status)
    active = None


def watch():
    consecutive = 0
    previous_id = None
    while True:
        sleep(1)
        with lock:
            trial = active
            if trial is None:
                continue
            if previous_id != trial["trial_id"]:
                previous_id = trial["trial_id"]
                consecutive = 0
        is_healthy = healthy(trial["target"])
        with lock:
            if active is not trial:
                continue
            checked_at = time()
            if not is_healthy:
                if not trial["observed_fault"]:
                    trial["observed_fault"] = True
                    record(trial, "unhealthy_probe")
                consecutive = 0
            elif trial["observed_fault"]:
                consecutive += 1
            if consecutive >= 3:
                if trial["action_epoch"] is None:
                    finish(trial, "recovered_without_action")
                else:
                    finish(trial, "recovered", checked_at)
            elif checked_at > trial["failure_epoch"] + max(180, trial["duration"] + 90):
                finish(trial, "timed_out")


@app.post("/trials")
def start_trial():
    global active
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Expected a JSON object"), 400
    fault, method, duration = (body.get(key) for key in ("fault", "method", "duration"))
    if fault not in tuple(scenarios) or method not in ("manual", "automated"):
        return jsonify(error="Invalid fault or method"), 400
    if type(duration) is not int or not 60 <= duration <= 300:
        return jsonify(error="Duration must be 60–300 seconds"), 400
    target, alert = scenarios[fault]
    if body.get("target", target) != target:
        return jsonify(error=f"{fault} requires target {target}"), 400
    with lock:
        if active:
            return jsonify(error="A trial is already active", trial=active), 409
        if time() - last_attempt.get(target, 0) < 30 or not healthy(target):
            return jsonify(error="Target must be healthy and outside the 30s cooldown"), 409
        try:
            alerts = fetch("http://alertmanager:9093/api/v2/alerts")
            if any(item["labels"].get("service") == target for item in alerts):
                return jsonify(error="Wait for the previous alert to clear"), 409
            target_container = container_for(target)
        except (OSError, RuntimeError, docker.errors.DockerException) as exc:
            return jsonify(error=str(exc)), 503
        now = time()
        trial = {
            "trial_id": uuid4().hex, "fault_type": fault, "target": target,
            "method": method, "expected_alert": alert, "duration": duration,
            "status": "active", "failure_epoch": now, "t_fail": utc(now),
            "t_recover": None, "mttr_sec": None, "action_epoch": None,
            "observed_fault": False, "events": [],
        }
        active = trial
        record(trial, "injection_started")
        try:
            inject(trial, target_container)
        except (RuntimeError, ValueError, docker.errors.DockerException) as exc:
            record(trial, "injection_error", message=str(exc))
            finish(trial, "injection_failed")
            return jsonify(trial), 500
        record(trial, "injection_applied")
        return jsonify(trial), 201


@app.get("/trials/active")
def current_trial():
    with lock:
        return jsonify(active)


@app.get("/trials/<trial_id>")
def get_trial(trial_id):
    try:
        trial_id = UUID(trial_id).hex
    except ValueError:
        return jsonify(error="Invalid trial ID"), 400
    with lock:
        path = directory / (trial_id + ".json")
        if not path.is_file():
            return jsonify(error="Unknown trial"), 404
        return Response(path.read_text(), mimetype="application/json")


def remediate(trial):
    target = trial["target"]
    now = time()
    if trial["action_epoch"] is not None or now - last_attempt.get(target, 0) < 30:
        return "duplicate_or_cooldown"
    if trial["fault_type"] != "container-crash" and now >= trial["failure_epoch"] + trial["duration"]:
        return "fault_window_elapsed"
    if healthy(target):
        return "target_healthy"
    trial["observed_fault"] = True
    last_attempt[target] = now
    record(trial, "restart_requested", method=trial["method"])
    try:
        target_container = container_for(target)
        if trial["fault_type"] == "network-slowdown":
            # Clear the queue before restarting to cover engines that reuse the namespace.
            checked_exec(target_container, ["tc", "qdisc", "del", "dev", "eth0", "root"])
        target_container.restart(timeout=2)
    except (RuntimeError, docker.errors.DockerException) as exc:
        record(trial, "restart_error", message=str(exc))
        finish(trial, "remediation_failed")
        return "restart_failed"
    trial["action_epoch"] = now
    record(trial, "restart_finished")
    return "restarted"


@app.post("/trials/<trial_id>/recover")
def recover_trial(trial_id):
    try:
        trial_id = UUID(trial_id).hex
    except ValueError:
        return jsonify(error="Invalid trial ID"), 400
    with lock:
        trial = active
        if not trial or trial["trial_id"] != trial_id or trial["method"] != "manual":
            return jsonify(error="Expected the active manual trial ID"), 409
        result = remediate(trial)
        return jsonify(result=result), 200 if result == "restarted" else 409


@app.post("/webhook/alert")
def receive_alert():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("alerts"), list):
        return jsonify(error="Expected an Alertmanager payload"), 400
    with lock:
        trial = active
        if trial is None:
            return jsonify(result="no_active_trial")
        for alert in body["alerts"]:
            if not isinstance(alert, dict) or alert.get("status") != "firing":
                continue
            labels = alert.get("labels")
            if not isinstance(labels, dict):
                continue
            if (labels.get("service"), labels.get("alertname")) != (
                    trial["target"], trial["expected_alert"]):
                continue
            try:
                started = datetime.fromisoformat(alert["startsAt"]).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            if started < trial["failure_epoch"]:
                continue
            if not any(event["event"] == "alert_received" for event in trial["events"]):
                record(trial, "alert_received", alert=trial["expected_alert"])
            if trial["method"] == "automated":
                return jsonify(result=remediate(trial))
            return jsonify(result="awaiting_operator")
    return jsonify(result="no_matching_alert")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    directory.mkdir(exist_ok=True)
    for path in directory.glob("*.json"):
        trial = json.loads(path.read_text())
        if trial["status"] == "active":
            finish(trial, "controller_interrupted")
    Thread(target=watch, daemon=True).start()
    app.run(host="0.0.0.0", port=8088)
