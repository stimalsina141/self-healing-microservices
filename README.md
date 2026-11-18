# Self-Healing Microservices

This project measures how automated detection and remediation affect mean time to recovery (MTTR) in a small Docker Compose system. The experiment compares automated recovery with a scripted operator.

## Start the lab

Use Docker Desktop or a Linux Docker Engine with Compose v2, cgroup v2, and kernel support for `tc netem`. The experiment CLI uses Python 3.10+ and the standard library. Start Docker before running these commands.

```sh
docker compose up -d --build --wait
docker compose ps
curl -fsS http://localhost:8000/health
```

| Local endpoint | Purpose |
| --- | --- |
| [Flask demo](http://localhost:8000) | Write notes to PostgreSQL, read notes through Redis, and submit word-count jobs to Celery |
| [Grafana dashboard](http://localhost:3000/d/self-healing-lab) | Observe availability, cache latency, worker memory, firing alerts, and app latency and errors |
| [Prometheus alerts](http://localhost:9090/alerts) | Inspect pending and firing rules |
| [Alertmanager](http://localhost:9093) | Operator view for manual trials and webhook delivery for automated trials |

The controller interacts with Docker through its socket and looks up only the lab’s Compose services by label. cAdvisor uses privileged access to collect raw container metrics. Run this stack on your own local development machine. Its published ports bind to loopback.

## Establish a baseline

```sh
python3 experiment.py workload --seconds 120
```

This writes a note, submits a Celery job, and reads the note approximately once a second. Check the job result, successful and failed read counts, and Grafana’s latency, error, and availability charts. Record the time window, host and Docker configuration, and observed baseline for the research write-up. Keep the same configuration and workload conditions across both trial modes. If you tune thresholds after this baseline, use the updated config for every measured trial.

## Run a trial

| Fault | Target | Injection | Alert |
| --- | --- | --- | --- |
| `container-crash` | `web` | Docker sends SIGKILL | `ServiceDown` after 10 seconds |
| `network-slowdown` | `cache` | Linux netem adds 200 ms delay and 5% loss | `HighLatency` above 150 ms for 30 seconds |
| `memory-exhaustion` | `worker` | stress-ng allocates memory within a 256 MiB cgroup | `HighMemory` above 90% for 15 seconds |

Prometheus scrapes and evaluates every 15 seconds, so alert delivery also depends on when the fault lands in that cycle. cAdvisor supplies raw container metrics. The worker’s cgroup reading drives the memory alert, and direct Redis probes drive the latency alert. A 150 ms threshold detects the planned 200 ms impairment before a timeout.

For a manual trial, watch Alertmanager, inspect the affected service, then request the restart yourself. Copy the trial ID from the start command.

```sh
python3 experiment.py start --fault container-crash --method manual
python3 experiment.py recover TRIAL_ID
python3 experiment.py status TRIAL_ID --wait
```

For an automated trial, Alertmanager sends the firing alert to the controller, which validates the alert against the active trial and restarts the affected container. Use the same fault and duration in both modes.

```sh
python3 experiment.py start --target cache --fault network-slowdown --method automated --duration 120
python3 experiment.py status TRIAL_ID --wait
```

Run one trial at a time. Wait for the previous alert to clear and the 30-second per-container cooldown to pass before starting another trial against the same target. Network and memory faults expire automatically after the configured duration. Use the 120-second default for the formal trials so there is time for the Prometheus and Alertmanager pipeline to run. A shorter window can expire before the alert reaches the controller. The controller records an expiry or other unassisted recovery separately. If a crash trial times out with the service still down, restore it with `docker compose restart web` before the next trial.

## Measure and analyze

```sh
python3 experiment.py plan --repeats 10 --seed 42 > data/trial-plan.csv
python3 experiment.py analyze
```

The schedule contains ten randomized blocks. Each block includes all six fault and recovery-mode combinations. Follow the schedule for 60 formal trials. Use the same operator procedure for each manual trial and record any deviations. Automated trials need no operator action after injection.

The controller saves each trial as JSON in `data/`, with UTC timestamps for injection and recovery, alert and restart events, and the final outcome. Both modes use the same timer. It starts just before the injection call and stops at the third consecutive healthy probe after an observed fault and a successful restart. Recovery checks the target directly, including a Redis latency threshold for cache and a Celery health and memory check for worker. Web recovery requires its dependency-aware `/health` endpoint to return success.

The analysis exports `data/results.csv` and `data/results.summary.json`. It groups by fault and method, reports recovered and other outcome counts, and calculates mean, median, sample standard deviation, and mean reduction. Only verified recoveries following a restart contribute to MTTR. Local verification runs are in `data/smoke/` and the default analysis reads formal trial logs directly under `data/`. Include the other outcomes when reporting results. For a PNG box plot, install `matplotlib` in a virtual environment and run `python3 experiment.py analyze --plot`.

```sh
docker compose down
```

## Unattended study

`run_study.py` follows the 60-row schedule with a scripted operator for rows marked
`manual`. The operator polls the real Alertmanager API every five seconds and requests
the manual restart ten seconds after it observes the matching alert. The comparison
measures the deployed recovery loop under those two policies. A human-response study
requires a human operator.

With the lab running, install Matplotlib in a Python virtual environment and run:

```sh
python run_study.py
python report_study.py
```

The runner needs exclusive use of the lab while it collects a two-minute baseline
and executes the schedule. It keeps progress in `data/study/progress.json` and records
trial attempts even when recovery fails. Running the same command again resumes that
study. The study report, trial CSV, grouped statistics, and box plot are written to
`data/study/`.
