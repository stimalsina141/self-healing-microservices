# Reproduce Self-Healing Microservices

Start with the short peer test. It runs the application, deliberately crashes the Flask container, and verifies that the monitoring system restarts it. The optional full study compares automated recovery with a scripted operator across 60 trials.

## 1. Install and check the tools

| Tool | What to install or check |
| --- | --- |
| Git | [Install Git](https://git-scm.com/downloads) to clone the repository. |
| Docker | Install and open [Docker Desktop](https://docs.docker.com/compose/install/) for your operating system, or use Docker Engine with the [Compose plugin on Linux](https://docs.docker.com/compose/install/linux/). Use the `docker compose` command and a version that supports `up --wait`. |
| Python | [Install Python 3.10 or newer](https://www.python.org/downloads/) for the command-line tools. |
| Matplotlib | Install this only for the full study and its box plot. Matplotlib 3.11.2 was used with Python 3.15 for the original export. The full-study instructions below install a compatible version for your Python. |

The project was verified on an Apple Silicon Mac with Docker Engine **29.6.2**, Compose **5.3.1**, cgroup **v2**, and about **4 GB** assigned to Docker. These are the tested settings. The full study requires Linux containers, cgroup v2, and kernel support for `tc netem`. Docker Desktop runs the Linux containers on macOS. Windows and native Linux were outside the verified setup. [Original environment](data/study/environment.json)

Use your terminal to check the tools:

```sh
git --version
docker version
docker compose version
python3 --version
```

`docker version` should show both a Client and a Server section. If there is a connection error, open Docker Desktop and wait for it to start. The commands below use a macOS or Linux shell. On Windows, Docker must be in Linux container mode, and the commands require a shell with Git, Docker, and Python available.

Docker downloads the app and monitoring dependencies during the first build. Flask, Redis, PostgreSQL, Celery, Prometheus, Alertmanager, Grafana, and cAdvisor all run in containers.

## 2. Download and start the project

```sh
git clone https://github.com/stimalsina141/self-healing-microservices.git
cd self-healing-microservices
docker compose up -d --build --wait
docker compose ps
curl -fsS http://127.0.0.1:8000/health
```

Run the remaining commands from inside the cloned folder. The first build may take a few minutes. The last command should print `{"ok":true}`. Open [Grafana](http://127.0.0.1:3000/d/self-healing-lab), select **Last 5 minutes**, and look for the **Service availability** panel. Give Prometheus a few scrape cycles at startup so the chart has a healthy baseline.

**Screenshot 1.** Capture the running containers and the successful health response.

## 3. Run the short peer test

In the terminal, deliberately crash the web container:

```sh
python3 experiment.py start --fault container-crash --method automated
```

Copy the `trial_id` from the JSON output. Replace `TRIAL_ID` with that value:

```sh
python3 experiment.py status TRIAL_ID --wait
```

The command waits for recovery. While it runs, check the `web` line in Grafana or open [Prometheus Alerts](http://127.0.0.1:9090/alerts). The line should drop to `0` and return to `1` after recovery. The alert may clear before you catch it on screen, so the completed terminal log is the easiest evidence to capture. Allow roughly a minute for the trial on the tested setup, with extra time for monitoring and startup on another machine.

A successful run shows:

- `alert_received` with the `ServiceDown` alert.
- `restart_requested` and `restart_finished` after the alert.
- `"status": "recovered"` and a positive `mttr_sec` value in seconds.

`mttr_sec` is the elapsed time from injection to the third consecutive healthy probe. The controller saves the full record in `data/TRIAL_ID.json`. Run the health command again to confirm the app is available:

```sh
curl -fsS http://127.0.0.1:8000/health
```

**Screenshot 2.** Capture the completed trial record with the alert, restart, and recovery time. If you can see the drop and return on Grafana, capture that too.

## 4. Know what a good result looks like

For the short test, success means the alert triggered a restart and the service recovered without a restart command from the evaluator. A different recovery time is expected. Prometheus scrapes every 15 seconds, so even two runs on the same computer can catch a fault at different points in that cycle. Alert delivery, container startup, machine load, and packet-loss variation in the network trials can also change the timing.

The original full study had **60 verified recoveries in 60 attempts**. Automation averaged **60.77 seconds** and the scripted operator averaged **74.49 seconds**, a reduction of **13.72 seconds (18.4%)**. The scripted operator polls for alerts every five seconds and waits ten seconds after seeing a matching alert before requesting the restart. These numbers describe the original experiment. Timing and the winning policy can vary in individual trials and across full reruns. Report the results you observe, including failed or timed-out trials. [Original results report](data/study/results.md)

## 5. Optional full study

Use a fresh checkout for a fresh study and keep the lab running. This step takes about **82 minutes** on the tested setup. Run only one study against the lab at a time and leave the terminal open. The runner collects a two-minute baseline, keeps a steady workload running, and follows the 60-row schedule. The `manual` rows in that schedule use the scripted operator automatically.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install matplotlib
python experiment.py plan --repeats 10 --seed 42 > data/trial-plan.csv
python run_study.py
python report_study.py
```

If the runner stops, resolve the error and rerun `python run_study.py` to resume from its saved progress. The final output contains 10 attempts for each of the six fault and recovery-policy combinations. Inspect the recovered counts first, then compare the mean and median for each fault. The local run updates the result files in your checkout:

| File | What it shows |
| --- | --- |
| `data/study/results.md` | Baseline, results by fault, and the measured difference between policies |
| `data/study/trials.csv` | All attempts, outcomes, timestamps, and recovery times |
| `data/study/trials.summary.json` | Grouped statistics and average recovery differences |
| `data/study/trials.png` | Box plots showing the spread of recovery times |

## 6. If something goes wrong

| What you see | What to check |
| --- | --- |
| Docker cannot connect to its server | Start Docker Desktop or the Docker Engine and rerun `docker version`. |
| `port is already allocated` | Check whether another app is using port 3000, 8000, 8088, 9090, or 9093. Stop the conflicting app or existing lab if you control it. |
| Build or startup fails | Record the first error shown in the terminal. Run `docker compose ps -a` and `docker compose logs --tail=50` for details. |
| A previous alert or cooldown blocks a new trial | Check `python3 experiment.py status`. Wait for the active run to finish, the old alert to clear, and the 30-second cooldown to pass. |
| A network or memory fault fails to start | Save the error. The full study needs `tc netem` support and a cgroup v2 memory limit. Check your setup with `docker info`. |
| A crash trial finishes without recovery | Save the trial output for your evaluation. If the web service is still down, run `docker compose restart web` and check its health again. An operator restart is troubleshooting evidence for that attempt. |

If something prevents you from running the project, include the step that failed, your OS and tool versions, and the exact error or a screenshot in your peer evaluation.

## 7. Stop the lab

```sh
docker compose down
```

The trial records and screenshots remain available after shutdown.
