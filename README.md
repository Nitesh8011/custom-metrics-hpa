# custom-metrics-hpa

Scale a Kubernetes Deployment **horizontally** (more/fewer pods) and **vertically** (bigger/smaller pods) using the kinds of signals real organisations scale on, with as many [KEDA](https://keda.sh) external-metric scalers as practical.

> **Status:** the Prometheus-driven scaling (5 metrics), kube-prometheus-stack and a live Grafana dashboard are **written and ready to run**: see the Quick start below. (While writing them I ran the RPS, concurrency, CPU, latency and queue scalers on a throwaway minikube profile to check the PromQL and thresholds; the combined scaler's `fallback` and a Grafana visual check are the parts I did not exercise.) Sections 1-11 after it are the roadmap for the remaining scalers and VPA. Items marked **[DECISION]** need your answer (collected at the bottom).

---

## Quick start: Prometheus-driven scaling

```text
 load (fortio) ──> podinfo pods ──/metrics──> Prometheus ──PromQL──> KEDA ──> HPA ──> more/fewer pods
                       ▲                          │                                     │
                       └────────── Grafana <──────┴───── kube-state-metrics, cAdvisor, KEDA metrics
```

KEDA's `prometheus` trigger runs a PromQL query every few seconds; the number it returns, divided by the `threshold`, decides the replica count. Everything runs in three namespaces: `monitoring` (kube-prometheus-stack), `keda`, `app` (the app).

### Run it

```bash
make up                  # minikube profile + Prometheus/Grafana + KEDA + app + dashboard (first run: ~5-8 min)
make grafana             # http://localhost:3000   login admin / admin   ->  Dashboards -> "KEDA + Prometheus Autoscaling"

make scaler S=01-rps     # pick a scaler (see table below)
make load L=rps          # start the matching load
make watch               # in another terminal: ScaledObject / HPA / pods, live
make stop-load           # stop the load and watch it scale back down
make destroy             # delete the whole minikube profile when done
```

- Prefer to see every step? [cluster/README.md](cluster/README.md) has the plain `helm` / `kubectl` commands for installing both charts, verifying them and uninstalling.
- All targets are pinned to their own minikube profile (`custom-metrics-hpa`, override with `PROFILE=...`), so they never touch the cluster your current `kubectl` context points at.
- Right after `make up`, give Prometheus ~2 minutes to pick up the new ServiceMonitors before the first test (check Status -> Targets in Prometheus: `make prometheus`).
- Chart versions are pinned in the Makefile: kube-prometheus-stack **92.10.1**, KEDA **2.21.0**. Images: podinfo 6.15.0, fortio 1.76.0, redis 8.8-alpine, redis_exporter 1.94.0.

### The five metrics

The "web" tier (podinfo) is scaled by four traffic/saturation metrics; the "worker" tier (Redis consumers) by queue backlog. Each file in [scalers/](scalers/) has a header explaining the PromQL and the replica math.

| # | Metric | Why orgs use it | Scaler file | Target | Replicas = |
|---|---|---|---|---|---|
| 1 | **Requests/sec** | Simplest "capacity per pod" model | [01-rps.yaml](scalers/01-rps.yaml) | 50 req/s per pod | `ceil(total_rps / 50)` |
| 2 | **p95 latency** | Scale to an SLO | [02-latency-p95.yaml](scalers/02-latency-p95.yaml) | 100 ms (`metricType: Value`) | `ceil(current * p95 / 0.1)` |
| 3 | **Concurrency** (in-flight requests) | Better than RPS when request cost varies | [03-concurrency.yaml](scalers/03-concurrency.yaml) | 5 in flight per pod | `ceil(in_flight / 5)` |
| 4 | **CPU cores** (cAdvisor via Prometheus) | CPU scaling without metrics-server, combinable with other signals | [04-cpu.yaml](scalers/04-cpu.yaml) | 0.035 cores per pod (70% of the 50m request) | `ceil(total_cores / 0.035)` |
| 5 | **Queue backlog** (jobs waiting) | The classic KEDA use case; scales to zero | [05-queue-backlog.yaml](scalers/05-queue-backlog.yaml) | 10 jobs per worker, `minReplicaCount: 0` | `ceil(backlog / 10)` |
| – | **All four web metrics at once** | What production usually looks like | [06-combined.yaml](scalers/06-combined.yaml) | as above | `max` of the four |

### Which load to run for which scaler

`make scaler S=<file>` then `make load L=<load>`. Each load is a Job in [loadgen/](loadgen/); `make stop-load` removes it. Numbers in the last column are what I saw when trying them.

| Scaler | Load | What the load does | What I saw |
|---|---|---|---|
| `01-rps` | `rps` | steady 200 req/s | 1 -> 4 pods, settled at exactly 50 req/s per pod; back to 1 about 2-3 min after `stop-load` |
| `03-concurrency` | `slow` | 20 connections x `/delay/2`: only ~10 req/s but ~20 in flight | 4-5 pods (the RPS scaler would stay at 1 here) |
| `04-cpu` | `saturate` | ~3000 req/s | 3 pods, ~34m CPU per pod against the 35m target |
| `02-latency-p95` | `latency` | 10 connections x `/delay/1` | p95 reads ~2.4 s, replicas 1 -> 5 -> 10 in ~30 s and stay there (see lesson 8) |
| `05-queue-backlog` | `queue` | pushes 100 jobs onto the Redis list | workers 0 -> ~7-8, queue drained in ~1 min, back to 0 after the cooldown |
| `06-combined` | any of the above | the HPA follows whichever trigger asks for the most pods | applied and the HPA showed all four metrics; not load-tested |

Things to try beyond the table: run `saturate` against `01-rps` (it runs to 10 pods) and against `02-latency-p95` (it does not react, see lesson 8); run `slow` against `01-rps` to see it stay at 1 pod.

### Dashboards: one per load type

Open Grafana (`make grafana`, or the port-forward in [cluster/README.md](cluster/README.md); login admin / admin) -> Dashboards. Every dashboard refreshes every 5 s, shows the last 15 minutes, and has a **KEDA dashboards** dropdown at the top to jump between them.

| Dashboard | Use it with | Scaler + load | What you'll see |
|---|---|---|---|
| **KEDA 1 - RPS** | the RPS test | `01-rps` + `rps` | req/s on **each pod** (red line = 50 target), total vs capacity, requests served per pod |
| **KEDA 2 - Latency p95** | the latency test | `02-latency-p95` + `latency` | p50/p95/p99, **p95 per pod**, a latency heatmap, per-pod latency table |
| **KEDA 3 - Concurrency** | the slow-request test | `03-concurrency` + `slow` | requests **in flight on each pod** (target 5), requests/s vs in-flight, average duration |
| **KEDA 4 - CPU** | the CPU test | `04-cpu` + `saturate` | CPU **per pod** vs request/limit, % of request, **CPU throttling per pod**, memory per pod |
| **KEDA 5 - Queue backlog** | the worker test | `05-queue-backlog` + `queue` | backlog, backlog per worker, workers 0 -> N -> 0, Redis clients and commands/s, worker pod table |
| **KEDA + Prometheus Autoscaling** | everything at once | any | one panel per metric plus replica counts |

Each dashboard starts with a note naming its scaler, its load and what to expect, then six headline numbers, then time series, and ends with a **per-pod table** (one row per pod: req/s, CPU, memory, restarts, ...). The "desired vs current" panels show the HPA's decision next to reality, and **KEDA: metric value** is the number KEDA hands the HPA (flat 0 usually means the PromQL query matches nothing).

The dashboards are generated from [monitoring/dashboards/generate.py](monitoring/dashboards/generate.py) (standard-library Python). The namespace and object names are hidden dashboard variables (Dashboard settings -> Variables), set at the top of that script, so a rename is a one-line change: edit `VARS`, run `python3 monitoring/dashboards/generate.py`, then `kubectl apply -k monitoring/dashboards`.

### Lessons from building and testing this (each one is already handled in the YAML)

1. **Prometheus ignores your ServiceMonitors by default.** The chart only selects ServiceMonitors labelled with its Helm release. `prometheusSpec.*SelectorNilUsesHelmValues: false` in [the values file](cluster/kube-prometheus-stack-values.yaml) fixes it, and the first scrape can still take ~2 min to appear.
2. **Your own monitoring traffic pollutes the metric.** podinfo counts readiness/liveness probes *and Prometheus' own `/metrics` scrapes* as requests (measured: about 0.37 req/s per pod). All web queries exclude `path=~"readyz|healthz|metrics"`.
3. **Non-additive metrics need `metricType: Value`.** Summing p95s across pods is meaningless; with the default `AverageValue`, a p95 query would give nonsense replica counts.
4. **Empty result vs zero.** With no traffic a filtered `sum()` returns *no series* and `histogram_quantile` returns nothing. The queries end in `or vector(0)` / `> 0 or vector(0)` so KEDA always gets an explicit number.
5. **`pollingInterval` only matters for scale-to-zero.** With `minReplicaCount >= 1` the HPA's own 15 s loop drives scaling (KEDA 2.21 even warns about it), so only the worker scaler sets it.
6. **The HPA has a 10% tolerance.** At 52 req/s per pod against a target of 50 it will not add a pod. That is normal, not a bug.
7. **One `ScaledObject` per Deployment.** The web scaler files all use the name `podinfo`, so `make scaler S=...` simply swaps the triggers on the same object.
8. **Latency is the trickiest of the five.** (a) It only works when latency is capacity-bound; under the `latency` load every request takes 1 s by design, so adding pods never helps and it runs to `maxReplicaCount`. (b) podinfo's histogram times only the handler, so when a pod was CPU-throttled under `saturate`, p95 stayed ~5 ms and the latency scaler even scaled *down* to 1 pod while that pod was saturated. Real setups measure latency at the ingress/mesh, which includes queueing. (c) Used alone, its proportional rule keeps shrinking replicas while latency is low. That is why [06-combined.yaml](scalers/06-combined.yaml) pairs it with RPS, concurrency and CPU. (d) Histogram buckets limit precision: p95 of 1 s requests reads as 2.425 s because `histogram_quantile` interpolates inside the 1-2.5 s bucket.
9. **Per-pod views only make sense if the load is spread.** A Kubernetes Service balances per *connection*, and fortio keeps its connections open forever by default, so pods added by the autoscaler would get no traffic from a load that is already running: the dashboards would show one busy pod and idle ones while the HPA happily reports "50 per pod" (it divides the total by the replica count). The load files pass `-connection-reuse min:max` so connections are replaced regularly and new pods start receiving requests within seconds. (The flag comes from fortio's docs; I did not run it.)
10. **`fallback` is in the combined scaler but unverified.** KEDA accepts it next to the `Value`-type latency trigger, but I did not break Prometheus to see it kick in. Try it: point `serverAddress` at a wrong host and watch `kubectl -n app get scaledobject` for `FALLBACK=True` after ~3 polls.

---

## 1. Goal

1. One simple, reusable **sample Deployment** (the thing being scaled).
2. A **catalog of KEDA `ScaledObject` manifests**: one per scaler, each runnable on its own, covering as many KEDA scalers as can be demonstrated.
3. A **combined multi-trigger example** that looks like a production setup (several signals on one Deployment).
4. **Vertical scaling** with the Vertical Pod Autoscaler (VPA), set up so it does not fight KEDA.
5. Scripts and docs so the whole thing runs on a local cluster (kind) with one command per step.

---

## 2. Concepts (short version)

| Piece | What it does | Scales |
|---|---|---|
| **HPA** (built in) | Adds/removes pod replicas based on metrics | Horizontal, 1 to N |
| **KEDA** | Feeds *external* metrics (queue lag, Prometheus queries, DB rows, cloud metrics, cron windows...) to an HPA that KEDA creates for you. It also handles **0 to 1** and **1 to 0** itself | Horizontal, 0 to N |
| **VPA** | Recommends or changes pod CPU/memory *requests/limits* | Vertical |
| **metrics-server** | Provides CPU/memory usage to HPA and VPA | n/a |

How KEDA works: you create a `ScaledObject` that points at a Deployment and lists `triggers`. KEDA turns each trigger into an external metric, creates an HPA from them, and the HPA does the actual replica math.

**KEDA is horizontal only.** Vertical scaling comes from VPA, which is a separate project (section 6).

---

## 3. The sample workload

A deliberately boring Deployment, so the interesting part is the scaling config, not the app.

- `app/deployment.yaml`: Deployment (+ Service) with CPU/memory **requests and limits set** (required for CPU/memory scaling and for VPA to have something to adjust), readiness probe, and `replicas` left unset so the autoscaler owns it.
- Image: a stock image (nginx or similar). For queue/DB/cloud scalers the pods don't have to actually consume anything; the point is to watch the replica count react to the metric. **[DECISION 1]**

---

## 4. Horizontal scaling: KEDA scaler catalog

KEDA ships 60+ scalers. Below is what I plan to cover, grouped by how organisations actually use them. **Tier A** runs entirely inside a local minikube cluster (no cloud account). **Tier B** needs a cloud/SaaS account or credentials. **[DECISION 2]**

Scaler `type` names are from memory of the KEDA docs. During the build I'll check each against the installed KEDA version (`kubectl explain`, the keda.sh scaler docs) and drop or fix anything that doesn't exist.

### 4.1 Baseline resource metrics (every org starts here)

| Scaler | Real-world use | Tier |
|---|---|---|
| `cpu` | Scale on average CPU utilisation of the pods | A |
| `memory` | Scale on average memory utilisation | A |
| `kubernetes-workload` | Scale a worker based on the pod count of *another* workload (e.g. 1 worker per N API pods) | A |

Note: `cpu`/`memory` triggers can't scale to zero and need metrics-server.

### 4.2 Time-based (predictable traffic)

| Scaler | Real-world use | Tier |
|---|---|---|
| `cron` | Pre-scale for business hours, sales, batch windows, market open; scale down overnight | A |

### 4.3 Application/traffic metrics (most common in production)

| Scaler | Real-world use | Tier |
|---|---|---|
| `prometheus` | Requests/sec, p95 latency, error rate, in-flight requests, active sessions (any PromQL query) | A |
| `metrics-api` | Scale on any JSON HTTP endpoint (internal service reporting "current load") | A |
| `loki` | Scale on log rate or error-log count (LogQL) | A |
| `elasticsearch` | Scale on a search query result (e.g. count of pending docs) | A |
| `influxdb` | Scale on a Flux query over time-series data | A |
| `graphite` | Scale on a Graphite query | A |
| `opentelemetry` *(if available in the chosen KEDA version)* | Scale on OTel metrics | A |
| `external` / `external-push` | Your own custom gRPC scaler for business-specific logic | A |

### 4.4 Message queues and streams (the #1 real-world KEDA use case)

| Scaler | Real-world use | Tier |
|---|---|---|
| `kafka` | Consumer-group **lag** per topic | A |
| `rabbitmq` | Queue length or message rate | A |
| `redis` (lists) | Length of a Redis list used as a job queue (Sidekiq/Celery/Resque style) | A |
| `redis-streams` | Pending entries in a consumer group | A |
| `nats-jetstream` | Pending messages on a JetStream consumer | A |
| `pulsar` | Backlog of a Pulsar subscription | A |
| `activemq` / `artemis-queue` | Queue depth on legacy JMS brokers | A |
| `ibmmq` / `solace-event-queue` | Enterprise broker queue depth | B (licensed images) |
| `beanstalkd`, `liiklus` | Niche queue systems (if time allows) | A |

### 4.5 Databases (queue-in-a-table, backlog rows)

| Scaler | Real-world use | Tier |
|---|---|---|
| `postgresql` | `SELECT count(*) FROM jobs WHERE status='pending'` | A |
| `mysql` | Same pattern on MySQL | A |
| `mssql` | Same pattern on SQL Server | A |
| `mongodb` | Count of pending documents | A |
| `cassandra` | CQL query result as a metric | A |
| `couchdb` | Query result as a metric | A |
| `etcd` | Key value / revision as a signal | A |
| `solr` | Query hit count | A |
| `arangodb` | AQL query result | A |

### 4.6 Cloud provider queues and metrics

| Scaler | Real-world use | Tier |
|---|---|---|
| `aws-sqs-queue` | Visible messages in an SQS queue | B (or LocalStack) |
| `aws-cloudwatch` | Any CloudWatch metric (ALB request count, etc.) | B |
| `aws-kinesis-stream` | Shard count / iterator age | B (or LocalStack) |
| `aws-dynamodb` / `aws-dynamodb-streams` | Query result / stream shards | B (or LocalStack) |
| `azure-servicebus` | Service Bus queue/topic length | B |
| `azure-queue` (Storage Queue) | Storage queue length | B (or Azurite) |
| `azure-eventhub` | Event Hub unprocessed events | B |
| `azure-blob` | Blob count in a container | B (or Azurite) |
| `azure-monitor` / `azure-log-analytics` / `azure-data-explorer` | Azure metrics/KQL queries | B |
| `azure-pipelines` | Pending Azure DevOps jobs, to autoscale build agents | B |
| `gcp-pubsub` | Undelivered messages in a subscription | B (or Pub/Sub emulator) |
| `gcp-stackdriver` | Cloud Monitoring metric | B |
| `gcp-storage` | Object count in a bucket | B |
| `gcp-cloud-tasks` | Cloud Tasks queue depth | B |

### 4.7 SaaS observability platforms

| Scaler | Real-world use | Tier |
|---|---|---|
| `datadog` | Any Datadog query/metric | B |
| `new-relic` | NRQL query result | B |
| `dynatrace` | Dynatrace metric | B |
| `splunk` / `sumologic` | Search/metric query result | B |

### 4.8 CI/CD runners and automation

| Scaler | Real-world use | Tier |
|---|---|---|
| `github-runner` | Queued GitHub Actions jobs, to autoscale self-hosted runners | B (needs a repo + token) |
| `selenium-grid` | Pending browser sessions, to autoscale test nodes | A |
| `temporal` | Task queue backlog | A |
| `cloudeventsource` / event-driven | Scale on CloudEvents | A (if supported) |

### 4.9 Predictive

| Scaler | Real-world use | Tier |
|---|---|---|
| `predictkube` | Forecast-based scaling from Prometheus data | B (needs API key) |

**Realistic coverage:** all of Tier A (roughly 35-40 scalers) fully working locally. Tier B scalers get a manifest, a `TriggerAuthentication` template and a "how to test" note, but only run if you give me credentials or emulators. Where a Tier B service has a local emulator (LocalStack, Azurite, Pub/Sub emulator), I'll use that.

---

## 5. Horizontal scaling: patterns beyond "one trigger"

These are the things production setups use that a basic example skips. Each gets its own manifest under `combined/`.

| Pattern | KEDA feature | Why orgs use it |
|---|---|---|
| **Multiple triggers on one Deployment** | `triggers: [...]` | The HPA scales to the **highest** replica count any trigger asks for, so you can say "scale on CPU *or* queue lag *or* RPS, whichever is hottest" |
| **Combine triggers with a formula** | `advanced.scalingModifiers` | Custom logic across triggers (e.g. weighted sum, `max`, ratios) instead of the default "max wins" |
| **Scale to zero** | `minReplicaCount: 0` + `idleReplicaCount` | Save money on idle dev/batch workloads |
| **Slow scale-down, fast scale-up** | `advanced.horizontalPodAutoscalerConfig.behavior` (stabilization window, policies) | Prevents flapping |
| **Fallback when the metric source is down** | `fallback.failureThreshold` + `fallback.replicas` | Don't scale to 0 just because Prometheus is unreachable |
| **Cooldown / polling tuning** | `pollingInterval`, `cooldownPeriod` | Balance cost vs responsiveness |
| **Credentials handled safely** | `TriggerAuthentication` / `ClusterTriggerAuthentication` (Secrets, env, Vault, cloud workload identity) | No passwords in `ScaledObject` YAML |
| **Pause autoscaling** | annotations `autoscaling.keda.sh/paused` / `paused-replicas` | Freeze scaling during incidents or migrations |
| **Batch jobs** | `ScaledJob` | One Kubernetes Job per N messages, instead of long-running workers |

### Rules to know

- **One `ScaledObject` per target Deployment.** KEDA refuses two on the same workload. So the per-scaler manifests are alternatives: apply one at a time (the Makefile will swap them), while `combined/` shows several triggers in one object.
- **Don't also create your own HPA** for a Deployment KEDA manages.
- `cpu`/`memory` triggers need `resources.requests` on the container.

---

## 6. Vertical scaling (VPA)

KEDA doesn't resize pods. VPA does. I'll add:

| File | Mode | Purpose |
|---|---|---|
| `vertical/vpa-recommend.yaml` | `updateMode: "Off"` | **Recommend only.** Most orgs start here: read suggested requests with `kubectl describe vpa` and apply them via GitOps |
| `vertical/vpa-initial.yaml` | `Initial` | Right-size only when a pod is created (safe with HPA/KEDA) |
| `vertical/vpa-auto.yaml` | `Recreate` / `Auto` | VPA evicts and recreates pods with new requests |
| `vertical/vpa-inplace.yaml` | `InPlaceOrRecreate` *(newer VPA + K8s versions only)* | Resize without restarting, depending on cluster support. I'll verify what your cluster version supports before including it |

### KEDA + VPA together: the conflict to avoid

HPA and VPA both reacting to **CPU or memory** on the same Deployment will fight (VPA changes requests, which changes utilisation %, which makes the HPA scale, and so on).

The safe combination, which is what I'll configure:

- **KEDA scales on external metrics** (queue lag, RPS, Prometheus, ...), **not** on `cpu`/`memory`.
- **VPA manages CPU/memory requests** (`controlledResources: ["cpu","memory"]`) with `minAllowed`/`maxAllowed` guard rails.
- The `cpu`/`memory` KEDA examples in section 4.1 are therefore **not** combined with VPA in the same demo. The docs will say why.

---

## 7. Repo layout

Built so far is marked ✅; the rest is still the plan.

```
custom-metrics-hpa/
├── README.md
├── Makefile                       ✅ make up | scaler S=.. | load L=.. | grafana | destroy
├── cluster/
│   ├── kube-prometheus-stack-values.yaml   ✅ Prometheus + Grafana (chart 92.10.1)
│   └── keda-values.yaml                    ✅ KEDA (chart 2.21.0) + ServiceMonitors
├── app/
│   ├── 00-namespace.yaml          ✅ namespace "app"
│   ├── web.yaml                   ✅ podinfo Deployment + Service + ServiceMonitor
│   └── worker.yaml                ✅ Redis (+exporter) + queue-consumer Deployment
├── scalers/
│   ├── 01-rps.yaml ... 05-queue-backlog.yaml   ✅ one ScaledObject per Prometheus metric
│   └── 06-combined.yaml                        ✅ 4 triggers on one Deployment
├── loadgen/                       ✅ rps.yaml, slow.yaml, saturate.yaml, latency.yaml, queue.yaml
├── monitoring/dashboards/         ✅ 6 Grafana dashboards (generate.py -> *.json, auto-loaded by the sidecar)
│
├── backends/                      later: kafka, rabbitmq, postgres, mysql, mongodb, nats, ...
├── scalers/ (more)                later: other KEDA scalers from section 4
├── combined/                      later: scalingModifiers, fallback, ScaledJob
└── vertical/                      later: vpa-*.yaml
```

Every scaler file has the same shape: a header comment (what it measures, why orgs use it, the replica math, how to test), then the `ScaledObject`, and a `TriggerAuthentication` where credentials are needed.

---

## 8. Build phases

| Phase | Deliverable | You can verify with |
|---|---|---|
| ✅ 1 | minikube cluster + KEDA + kube-prometheus-stack (Prometheus, Grafana). metrics-server and VPA wait for the VPA phase | `kubectl get pods -A`, `kubectl get apiservices \| grep external.metrics` |
| ✅ 2 | sample app (`app/`) | `make status` |
| ✅ 3 | Prometheus scalers + Grafana dashboard (see the Quick start above) | Load generator, watch replicas follow a PromQL query |
| 4 | Queue and stream backends + `04-queues` | Publish N messages, replicas scale up, drain, scale down |
| 5 | Database backends + `05-databases` | Insert pending rows, watch scaling |
| 6 | `combined/` patterns | Multi-trigger, formula, fallback, scale-to-zero |
| 7 | `vertical/` VPA | `kubectl describe vpa` recommendations; auto-resize demo |
| 8 | Tier B (`06-cloud`, `07-saas`, `08-cicd`): manifests + auth templates (runnable only with your credentials/emulators) | Per-scaler notes |

I'll stop after each phase so you can review, rather than generating everything at once.

---

## 9. Prerequisites

- Docker (with ~5 GB free for the VM), `minikube`, `kubectl`, `helm`, `make`
- Apple Silicon and Intel both work; every image used is multi-arch.
- Later phases install backends **on demand per scaler**, so you won't need them all running together.

---

## 10. Known limitations / honesty section

- KEDA can't give you "every metric ever", only the scalers it ships. Anything else goes through `prometheus`, `metrics-api` or a custom `external` scaler.
- Some Tier B scalers can't be proven to work without real accounts. They'll be marked **untested** in their header comment.
- The scaler list and `type` names come from my knowledge of KEDA's docs and may be slightly off for the newest release; I'll verify against the installed version while building.
- In-place VPA resizing depends on cluster and VPA versions; I'll only include it if your cluster supports it.

---

## 11. Decisions I need from you

Answered by the build so far: the cluster is **minikube** (not kind), the sample app is **podinfo** (it exposes real Prometheus metrics, which a plain nginx wouldn't), and the layout is **one file per scaler**.

Still open:

1. **Scope [DECISION 2]**: (a) Tier A only (local, fully working), (b) Tier A + Tier B manifests with untested cloud templates, or (c) Tier A + Tier B with emulators (LocalStack/Azurite) where available. *Recommendation: (b).*
2. **Vertical scaling**: should I include the Auto/in-place VPA demos, or recommendation-only?
3. **Next phase**: queues and streams (Kafka, RabbitMQ, Redis streams, NATS), databases, or VPA?
