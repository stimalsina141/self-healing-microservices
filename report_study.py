"""Write the seminar results report from the completed unattended study."""

import csv
import json
from random import Random
from statistics import mean, median

from experiment import FAULTS, METHODS
from run_study import BASELINE_SECONDS, DURATION, POLL_SECONDS, REACTION_SECONDS, STUDY


def samples(series):
    return [float(value) for metric in series for _, value in metric["values"]]


def interval(values):
    rng = Random(2026)
    resampled = sorted(mean(rng.choices(values, k=len(values))) for _ in range(10000))
    return resampled[250], resampled[9749]


def display(value):
    return f"{float(value):.2f}" if value != "" else "—"


def make_report():
    with (STUDY / "trials.csv").open(newline="") as source:
        trials = list(csv.DictReader(source))
    if len(trials) != 60 or any(trial["status"] == "active" for trial in trials):
        raise ValueError("The report requires 60 completed trial attempts")
    baseline = json.loads((STUDY / "baseline.json").read_text())
    baseline_metrics = baseline["prometheus_last_minute"]
    summary = json.loads((STUDY / "trials.summary.json").read_text())
    workload = json.loads((STUDY / "workload.json").read_text())
    blocks = sorted({trial["block"] for trial in trials}, key=int)
    recovered = [trial for trial in trials if trial["status"] == "recovered"]
    values = {(row["block"], row["fault_type"], row["method"]): float(row["mttr_sec"])
              for row in recovered}
    pair_differences = {}
    for fault in FAULTS:
        differences = {}
        for block in blocks:
            manual = values.get((block, fault, "manual"))
            automated = values.get((block, fault, "automated"))
            if manual is not None and automated is not None:
                differences[block] = manual - automated
        pair_differences[fault] = differences

    lines = [
        "# Self-Healing Microservices — experiment results", "",
        f"The randomized schedule produced {len(recovered)} verified recoveries in "
        f"{len(trials)} trial attempts. The comparison measures automatic restart against "
        "a scripted operator. It measures service recovery on this lab and the cost of "
        "the scripted response policy. Human on-call response time remains unmeasured.",
        "", "## Procedure", "",
        "The experiment used ten randomized blocks with one trial per fault and recovery "
        "policy in each block. The faults were a forced Flask container crash, 200 ms of "
        "delay and 5% packet loss on Redis, and memory pressure on the Celery worker. "
        f"Temporary faults were configured for {DURATION} seconds. A seeded delay of up to "
        "12 seconds before each trial varied injection timing relative to monitoring.",
        "",
        f"The scripted operator polled Alertmanager every {POLL_SECONDS} seconds and "
        f"requested the controller’s manual restart {REACTION_SECONDS} seconds after "
        "observing the matching firing alert. The automated policy requested the same "
        "restart through the Alertmanager webhook. Both policies verified recovery with "
        "three consecutive healthy probes. The timer ran from the injection request to "
        "the final confirming probe.",
        "", "## Baseline", "",
        f"The study began with a {BASELINE_SECONDS}-second baseline on the same "
        "approximately one-request-per-second cached note workload used during the trials.",
        "",
        f"- The workload recorded {baseline['workload']['successful']} successful reads "
        f"and {baseline['workload']['failed']} failed reads.",
        f"- Up probes accounted for {sum(samples(baseline_metrics['service_availability'])):.0f} "
        f"of {len(samples(baseline_metrics['service_availability']))} service availability samples.",
        f"- The median sampled web p95 latency was "
        f"{median(samples(baseline_metrics['web_latency_p95_seconds'])) * 1000:.2f} ms.",
        f"- The median cache probe took {median(samples(baseline_metrics['cache_probe_seconds'])) * 1000:.2f} ms.",
        f"- Median worker memory usage was {median(samples(baseline_metrics['worker_memory_ratio'])) * 100:.1f}% of its limit.",
        f"- The peak sampled HTTP 5xx rate was {max(samples(baseline_metrics['web_5xx_per_second'])):.3f} per second.",
        "", "## Recovery time by condition", "",
        "| Fault | Recovery policy | Recovered / attempts | Mean (s) | Median (s) | Sample SD (s) |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for group in summary["groups"]:
        fault = group["fault_type"].replace("-", " ")
        method = "Scripted operator" if group["method"] == "manual" else "Automated"
        lines.append(
            f'| {fault} | {method} | {group["recovered"]}/{group["recorded"]} | '
            f'{display(group["mean_sec"])} | {display(group["median_sec"])} | '
            f'{display(group["sample_sd_sec"])} |'
        )

    lines += ["", "## Difference in mean recovery time", "",
              "Positive values favor automation. Each difference compares the two policies "
              "within a randomized block. The intervals are descriptive 95% percentile "
              "bootstrap intervals from 10,000 resamples of the matched blocks.", "",
              "| Fault | Matched blocks | Mean reduction (s) | 95% interval (s) |",
              "| --- | ---: | ---: | ---: |"]
    for fault, differences in pair_differences.items():
        times = list(differences.values())
        effect = f"{mean(times):.2f}" if times else "—"
        bounds = " to ".join(f"{end:.2f}" for end in interval(times)) if len(times) > 1 else "—"
        lines.append(f'| {fault.replace("-", " ")} | {len(times)} | {effect} | {bounds} |')

    complete_blocks = set.intersection(*(set(values) for values in pair_differences.values()))
    if complete_blocks:
        paired = [mean(pair_differences[fault][block] for fault in FAULTS)
                  for block in sorted(complete_blocks)]
        paired_mean = mean(paired)
        manual_mean = mean(values[block, fault, "manual"] for block in sorted(complete_blocks)
                           for fault in FAULTS)
        auto_mean = mean(values[block, fault, "automated"] for block in sorted(complete_blocks)
                         for fault in FAULTS)
        lower, upper = interval(paired) if len(paired) > 1 else (None, None)
        interval_text = (f" The descriptive 95% interval was {lower:.2f} to {upper:.2f} seconds."
                         if lower is not None else "")
        lines += [
            "", f"Across {len(complete_blocks)} blocks with all six recoveries, the scripted "
            f"operator averaged **{manual_mean:.2f} seconds** and automation averaged "
            f"**{auto_mean:.2f} seconds**. The signed reduction was **{paired_mean:.2f} seconds "
            f"({100 * paired_mean / manual_mean:.1f}%)**." + interval_text,
        ]
    lines += ["", "## Interpretation", ""]
    for method in METHODS:
        rows = [trial for trial in recovered if trial["method"] == method]
        rows_with_alert = [trial for trial in rows if trial["alert_delivery_sec"]]
        if rows_with_alert:
            delivery = [float(trial["alert_delivery_sec"]) for trial in rows_with_alert]
            after_delivery = [float(trial["mttr_sec"]) - float(trial["alert_delivery_sec"])
                              for trial in rows_with_alert]
            name = "scripted operator" if method == "manual" else "automated policy"
            lines.append(
                f"- For the {name}, mean time to webhook delivery was {mean(delivery):.2f} "
                f"seconds, followed by {mean(after_delivery):.2f} seconds to confirmed "
                f"recovery across {len(delivery)} recoveries with a logged webhook."
            )
    incomplete = [trial for trial in trials if trial["status"] != "recovered"]
    if incomplete:
        lines += ["", "Non-recovery outcomes remain in the CSV and the counts above. "
                  "Recovery-time statistics use trials with a confirmed restart and recovery.", ""]
        for trial in incomplete:
            lines.append(f'- Block {trial["block"]}, order {trial["order"]}: '
                         f'{trial["fault_type"]}, {trial["method"]}, {trial["status"]}.')
    lines += [
        "", "The operator’s polling cadence and fixed response delay are part of the measured "
        "comparison. Results apply to the local Compose configuration, thresholds, load, and "
        "injected faults. Ten blocks provide a limited view of tail and long-run behavior.",
        "", f"The workload recorded {workload['successful']} successful and {workload['failed']} "
        "failed reads across the run, including the injected fault windows.",
        "", "## Artifacts", "",
        "- [Trial observations](trials.csv)",
        "- [Grouped statistics](trials.summary.json)",
        "- [Recovery-time box plot](trials.png)",
        "- [Baseline samples](baseline.json)",
        "- [Environment](environment.json)",
        "- [Trial schedule](../trial-plan.csv)",
        "",
    ]
    (STUDY / "results.md").write_text("\n".join(lines))


if __name__ == "__main__":
    make_report()
