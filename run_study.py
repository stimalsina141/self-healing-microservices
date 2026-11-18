"""Run the scheduled trials with a scripted operator and a constant read workload."""

import csv
import json
import random
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlencode
from uuid import UUID

from experiment import CONTROLLER, ROOT, WEB, api, boxplot, summarize

DATA = ROOT / "data"
STUDY = DATA / "study"
ALERTMANAGER = "http://127.0.0.1:9093"
PROMETHEUS = "http://127.0.0.1:9090"
POLL_SECONDS = 5
REACTION_SECONDS = 10
DURATION = 120
BASELINE_SECONDS = 120


def write_json(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.replace(path)


def load_loop(note_id, stop, counts):
    while not stop.is_set():
        started = time.monotonic()
        try:
            api(WEB, f"/notes/{note_id}")
        except RuntimeError:
            counts["failed"] += 1
        else:
            counts["successful"] += 1
        stop.wait(max(0, 1 - (time.monotonic() - started)))


def baseline(counts):
    start = time.time()
    print("Collecting the 120-second baseline under the study workload.", flush=True)
    time.sleep(BASELINE_SECONDS)
    end = time.time()
    expressions = {
        "web_latency_p95_seconds": 'histogram_quantile(0.95, sum by (le) (rate(lab_http_request_duration_seconds_bucket{route="/notes/<int:note_id>"}[1m])))',
        "web_5xx_per_second": 'sum(rate(lab_http_requests_total{status=~"5.."}[1m])) or vector(0)',
        "service_availability": "lab_service_up",
        "cache_probe_seconds": "lab_cache_probe_duration_seconds",
        "worker_memory_ratio": "lab_container_memory_ratio",
    }
    observations = {}
    for name, expression in expressions.items():
        query = urlencode({"query": expression, "start": start + 60, "end": end, "step": 15})
        observations[name] = api(PROMETHEUS, "/api/v1/query_range?" + query)["data"]["result"]
    if not all(observations.values()) or counts["failed"]:
        raise RuntimeError("Baseline monitoring or the baseline workload was unhealthy")
    write_json(STUDY / "baseline.json", {
        "started_utc": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "elapsed_seconds": round(end - start, 2), "workload": dict(counts),
        "prometheus_last_minute": observations,
    })


def trial_files():
    files = {}
    for path in DATA.glob("*.json"):
        try:
            trial_id = UUID(path.stem).hex
        except ValueError:
            continue
        files[trial_id] = path
    return files


def start_trial(row, progress):
    used = {item["trial_id"] for item in progress["trials"]}
    unknown = set(trial_files()) - used - set(progress["preexisting_ids"])
    if unknown:
        if len(unknown) != 1:
            raise RuntimeError("Multiple unmatched trial logs. Study requires exclusive lab access.")
        trial = json.loads(trial_files()[unknown.pop()].read_text())
        if (trial["fault_type"], trial["method"]) != (row["fault_type"], row["method"]):
            raise RuntimeError("An unmatched log belongs to a different trial condition")
        return trial

    if api(CONTROLLER, "/trials/active") is not None:
        raise RuntimeError("An unrelated trial is active")
    body = {"fault": row["fault_type"], "method": row["method"], "duration": DURATION}
    for attempt in range(40):
        try:
            return api(CONTROLLER, "/trials", body)
        except RuntimeError as exc:
            if "HTTP 409" not in str(exc) or attempt == 39:
                raise
            time.sleep(3)


def firing_alert(trial):
    query = urlencode({"filter": [
        f'service="{trial["target"]}"', f'alertname="{trial["expected_alert"]}"',
    ]}, doseq=True)
    alerts = api(ALERTMANAGER, f"/api/v2/alerts?{query}")
    for alert in alerts:
        if alert["status"]["state"] == "active" and (
            datetime.fromisoformat(alert["startsAt"]).timestamp() >= trial["failure_epoch"]
        ):
            return True
    return False


def follow_trial(trial, entry):
    route = f'/trials/{trial["trial_id"]}'
    next_poll = time.monotonic()
    while True:
        trial = api(CONTROLLER, route)
        if trial["status"] != "active":
            return trial
        if trial["method"] == "manual" and trial["action_epoch"] is None:
            now = time.time()
            if entry.get("observed_alert_epoch") is not None:
                if now >= entry["observed_alert_epoch"] + REACTION_SECONDS:
                    try:
                        api(CONTROLLER, route + "/recover", {})
                    except RuntimeError as exc:
                        if "HTTP 409" not in str(exc):
                            raise
                        entry["operator_response"] = str(exc)
                    else:
                        entry["operator_response"] = "restart_requested"
                    if entry["operator_response"] != "restart_requested":
                        print(f"Operator restart result: {entry['operator_response']}", flush=True)
                        time.sleep(3)
            elif time.monotonic() >= next_poll:
                next_poll = time.monotonic() + POLL_SECONDS
                if firing_alert(trial):
                    entry["observed_alert_epoch"] = time.time()
        time.sleep(0.5)


def restore_if_needed(trial):
    if trial["status"] == "recovered":
        return
    subprocess.run(["docker", "compose", "restart", trial["target"]], cwd=ROOT, check=True)
    time.sleep(30)


def run():
    STUDY.mkdir(exist_ok=True)
    with (DATA / "trial-plan.csv").open(newline="") as file:
        rows = list(csv.DictReader(file))
    expected = Counter((row["fault_type"], row["method"]) for row in rows)
    if len(rows) != 60 or len(expected) != 6 or set(expected.values()) != {10}:
        raise ValueError("Expected ten scheduled trials for each of the six conditions")
    progress_path = STUDY / "progress.json"
    if progress_path.exists():
        progress = json.loads(progress_path.read_text())
    else:
        progress = {"preexisting_ids": sorted(trial_files()), "trials": []}
        write_json(progress_path, progress)

    note = api(WEB, "/notes", {"text": "A note for the study's constant request workload"})
    counts = Counter(successful=0, failed=0)
    stop = Event()
    workload = Thread(target=load_loop, args=(note["id"], stop, counts), daemon=True)
    workload.start()
    try:
        if not (STUDY / "baseline.json").exists():
            baseline(counts)
        for index, row in enumerate(rows):
            if index < len(progress["trials"]):
                entry = progress["trials"][index]
                if "status" in entry:
                    continue
                trial = api(CONTROLLER, f'/trials/{entry["trial_id"]}')
            else:
                time.sleep(random.Random(7345 + index).uniform(0, 12))
                trial = start_trial(row, progress)
                entry = {**row, "trial_id": trial["trial_id"]}
                progress["trials"].append(entry)
                write_json(progress_path, progress)
            trial = follow_trial(trial, entry)
            restore_if_needed(trial)
            entry.update(status=trial["status"], mttr_sec=trial["mttr_sec"])
            write_json(progress_path, progress)
            print(f'{index + 1:02d}/60  {row["fault_type"]:18}  {row["method"]:9}  '
                  f'{trial["status"]}: {trial["mttr_sec"]} s', flush=True)
    finally:
        stop.set()
        workload.join(timeout=36)
        write_json(STUDY / "workload.json", dict(counts))
    export(progress)


def export(progress):
    source = trial_files()
    trials = [
        {**json.loads(source[item["trial_id"]].read_text()), **item}
        for item in progress["trials"]
    ]
    columns = (
        "block", "order", "trial_id", "fault_type", "method", "target", "status",
        "duration", "t_fail", "t_recover", "mttr_sec", "observed_alert_epoch",
        "operator_response", "alert_delivery_sec", "restart_request_sec",
    )
    with (STUDY / "trials.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for trial in trials:
            offsets = {
                event["event"]: round(datetime.fromisoformat(event["time"]).timestamp()
                                      - trial["failure_epoch"], 3)
                for event in trial["events"]
            }
            writer.writerow({**trial, "alert_delivery_sec": offsets.get("alert_received"),
                             "restart_request_sec": offsets.get("restart_requested")})
    summarize(trials, STUDY / "trials")
    boxplot(trials, STUDY / "trials.png")
    print("Study data exported.", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--export"]:
        export(json.loads((STUDY / "progress.json").read_text()))
    else:
        run()
