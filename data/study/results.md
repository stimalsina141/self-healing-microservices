# Self-Healing Microservices — experiment results

The randomized schedule produced 60 verified recoveries in 60 trial attempts. The comparison measures automatic restart against a scripted operator. It measures service recovery on this lab and the cost of the scripted response policy. Human on-call response time remains unmeasured.

## Procedure

The experiment used ten randomized blocks with one trial per fault and recovery policy in each block. The faults were a forced Flask container crash, 200 ms of delay and 5% packet loss on Redis, and memory pressure on the Celery worker. Temporary faults were configured for 120 seconds. A seeded delay of up to 12 seconds before each trial varied injection timing relative to monitoring.

The scripted operator polled Alertmanager every 5 seconds and requested the controller’s manual restart 10 seconds after observing the matching firing alert. The automated policy requested the same restart through the Alertmanager webhook. Both policies verified recovery with three consecutive healthy probes. The timer ran from the injection request to the final confirming probe.

## Baseline

The study began with a 120-second baseline on the same approximately one-request-per-second cached note workload used during the trials.

- The workload recorded 120 successful reads and 0 failed reads.
- Up probes accounted for 25 of 25 service availability samples.
- The median sampled web p95 latency was 4.75 ms.
- The median cache probe took 0.45 ms.
- Median worker memory usage was 33.6% of its limit.
- The peak sampled HTTP 5xx rate was 0.000 per second.

## Recovery time by condition

| Fault | Recovery policy | Recovered / attempts | Mean (s) | Median (s) | Sample SD (s) |
| --- | --- | ---: | ---: | ---: | ---: |
| container crash | Scripted operator | 10/10 | 68.37 | 69.38 | 5.48 |
| container crash | Automated | 10/10 | 54.19 | 52.67 | 4.77 |
| network slowdown | Scripted operator | 10/10 | 85.08 | 85.33 | 3.52 |
| network slowdown | Automated | 10/10 | 70.70 | 71.23 | 3.90 |
| memory exhaustion | Scripted operator | 10/10 | 70.04 | 67.97 | 4.43 |
| memory exhaustion | Automated | 10/10 | 57.42 | 58.58 | 5.99 |

## Difference in mean recovery time

Positive values favor automation. Each difference compares the two policies within a randomized block. The intervals are descriptive 95% percentile bootstrap intervals from 10,000 resamples of the matched blocks.

| Fault | Matched blocks | Mean reduction (s) | 95% interval (s) |
| --- | ---: | ---: | ---: |
| container crash | 10 | 14.17 | 9.76 to 18.40 |
| network slowdown | 10 | 14.38 | 10.67 to 18.21 |
| memory exhaustion | 10 | 12.62 | 8.42 to 16.68 |

Across 10 blocks with all six recoveries, the scripted operator averaged **74.49 seconds** and automation averaged **60.77 seconds**. The signed reduction was **13.72 seconds (18.4%)**. The descriptive 95% interval was 11.28 to 16.26 seconds.

## Interpretation

- For the scripted operator, mean time to webhook delivery was 57.90 seconds, followed by 16.60 seconds to confirmed recovery across 30 recoveries with a logged webhook.
- For the automated policy, mean time to webhook delivery was 56.81 seconds, followed by 3.96 seconds to confirmed recovery across 30 recoveries with a logged webhook.

The operator’s polling cadence and fixed response delay are part of the measured comparison. Results apply to the local Compose configuration, thresholds, load, and injected faults. Ten blocks provide a limited view of tail and long-run behavior.

The workload recorded 3692 successful and 1171 failed reads across the run, including the injected fault windows.

## Artifacts

- [Trial observations](trials.csv)
- [Grouped statistics](trials.summary.json)
- [Recovery-time box plot](trials.png)
- [Baseline samples](baseline.json)
- [Environment](environment.json)
- [Trial schedule](../trial-plan.csv)
