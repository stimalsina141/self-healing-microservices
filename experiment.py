"""Operate the local lab and analyze measured trials."""

import argparse
import csv
import json
import random
import statistics
import sys
import time
from itertools import product
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

ROOT = Path(__file__).resolve().parent
CONTROLLER = "http://127.0.0.1:8088"
WEB = "http://127.0.0.1:8000"
FAULTS = ("container-crash", "network-slowdown", "memory-exhaustion")
METHODS = ("manual", "automated")
COLUMNS = (
    "trial_id", "fault_type", "method", "target", "status", "duration",
    "t_fail", "t_recover", "mttr_sec",
)


def api(base, route, body=None):
    payload = json.dumps(body).encode() if body is not None else None
    req = Request(base + route, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=35) as response:
            return json.load(response)
    except HTTPError as exc:
        response = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {response}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot reach {base}: {exc.reason}") from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot reach {base}: {exc}") from exc


def print_json(result):
    print(json.dumps(result, indent=2), flush=True)


def start(args):
    body = {"fault": args.fault, "method": args.method, "duration": args.duration}
    if args.target:
        body["target"] = args.target
    print_json(api(CONTROLLER, "/trials", body))


def recover(args):
    print_json(api(CONTROLLER, f"/trials/{args.trial_id}/recover", {}))


def status(args):
    route = f"/trials/{args.trial_id or 'active'}"
    if args.wait and not args.trial_id:
        raise ValueError("--wait requires a trial ID")
    while True:
        trial = api(CONTROLLER, route)
        if not args.wait or trial["status"] != "active":
            print_json(trial)
            return
        time.sleep(2)


def plan(args):
    writer = csv.writer(sys.stdout)
    writer.writerow(("block", "order", "fault_type", "method"))
    generator = random.Random(args.seed)
    for block in range(1, args.repeats + 1):
        conditions = list(product(FAULTS, METHODS))
        generator.shuffle(conditions)
        for order, (fault, method) in enumerate(conditions, 1):
            writer.writerow((block, order, fault, method))


def summarize(trials, prefix):
    rows = []
    for fault, method in product(FAULTS, METHODS):
        matching = [trial for trial in trials
                    if (trial["fault_type"], trial["method"]) == (fault, method)]
        values = [trial["mttr_sec"] for trial in matching if trial["status"] == "recovered"]
        rows.append({
            "fault_type": fault, "method": method, "recorded": len(matching),
            "recovered": len(values), "other": len(matching) - len(values),
            "mean_sec": round(statistics.mean(values), 3) if values else "",
            "median_sec": round(statistics.median(values), 3) if values else "",
            "sample_sd_sec": round(statistics.stdev(values), 3) if len(values) > 1 else "",
        })

    comparisons = []
    for manual, automated in zip(rows[::2], rows[1::2]):
        if manual["recovered"] and automated["recovered"]:
            difference = manual["mean_sec"] - automated["mean_sec"]
            comparisons.append({
                "fault_type": manual["fault_type"],
                "mean_reduction_sec": round(difference, 3),
                "mean_reduction_pct": round(100 * difference / manual["mean_sec"], 2)
                if manual["mean_sec"] else None,
            })
    with prefix.with_suffix(".summary.json").open("w") as output:
        json.dump({"groups": rows, "comparisons": comparisons}, output, indent=2)
        output.write("\n")
    return rows


def boxplot(trials, path):
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(1, len(FAULTS), figsize=(11, 4), sharey=True)
    for fault, axis in zip(FAULTS, axes):
        for position, method in enumerate(METHODS, 1):
            values = [trial["mttr_sec"] for trial in trials if trial["status"] == "recovered"
                      and (trial["fault_type"], trial["method"]) == (fault, method)]
            if values:
                axis.boxplot(values, positions=[position], widths=0.5)
        axis.set(xlim=(0.5, 2.5), xticks=[1, 2], xticklabels=METHODS, title=fault.replace("-", " "))
        axis.grid(axis="y", alpha=0.25)
    axes[0].set_ylabel("Time to recovery (seconds)")
    figure.tight_layout()
    figure.savefig(path, dpi=200)
    plt.close(figure)


def analyze(args):
    trials = []
    for path in args.data.glob("*.json"):
        try:
            trial_id = UUID(path.stem).hex
        except ValueError:
            continue
        trial = json.loads(path.read_text())
        if trial["trial_id"] != trial_id:
            raise ValueError(f"Trial ID does not match the filename: {path}")
        trials.append(trial)
    trials.sort(key=lambda trial: trial["t_fail"])
    if not trials:
        raise ValueError(f"No trial logs in {args.data}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.with_suffix(".csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trials)
    print_json(summarize(trials, args.output))
    if args.plot:
        boxplot(trials, args.output.with_suffix(".png"))


def workload(args):
    note = api(WEB, "/notes", {"text": "A note for the baseline workload"})
    job = api(WEB, "/jobs", {"text": note["text"]})
    deadline = time.monotonic() + args.seconds
    completed_requests = 0
    request_errors = 0
    while time.monotonic() < deadline:
        try:
            api(WEB, f"/notes/{note['id']}")
            completed_requests += 1
        except RuntimeError:
            request_errors += 1
        time.sleep(1)
    print_json({
        "successful_reads": completed_requests, "failed_reads": request_errors,
        "job": api(WEB, f"/jobs/{job['id']}"),
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(required=True, dest="command")
    trial = subparsers.add_parser("start", help="Inject a fault and start recording a trial")
    trial.add_argument("--fault", choices=FAULTS, required=True)
    trial.add_argument("--method", choices=METHODS, required=True)
    trial.add_argument("--target", choices=("web", "cache", "worker"))
    trial.add_argument("--duration", type=int, default=120, help="Temporary fault duration in seconds")
    trial.set_defaults(handler=start)
    manual = subparsers.add_parser("recover", help="Request the repair in a manual trial")
    manual.add_argument("trial_id")
    manual.set_defaults(handler=recover)
    query = subparsers.add_parser("status", help="Read the active trial or a trial by ID")
    query.add_argument("trial_id", nargs="?")
    query.add_argument("--wait", action="store_true")
    query.set_defaults(handler=status)
    schedule = subparsers.add_parser("plan", help="Print a randomized trial schedule as CSV")
    schedule.add_argument("--repeats", type=int, default=10)
    schedule.add_argument("--seed", type=int, default=42)
    schedule.set_defaults(handler=plan)
    analysis = subparsers.add_parser("analyze", help="Export CSV and compute grouped MTTR statistics")
    analysis.add_argument("--data", type=Path, default=ROOT / "data")
    analysis.add_argument("--output", type=Path, default=ROOT / "data" / "results")
    analysis.add_argument("--plot", action="store_true", help="Write a PNG box plot using matplotlib")
    analysis.set_defaults(handler=analyze)
    load = subparsers.add_parser("workload", help="Create a note and job, then repeatedly read the note")
    load.add_argument("--seconds", type=int, default=120)
    load.set_defaults(handler=workload)

    args = parser.parse_args()
    try:
        args.handler(args)
    except (RuntimeError, ValueError, ImportError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
