#!/usr/bin/env python3
"""Generates the Grafana dashboards in this folder (standard library only).

    python3 monitoring/dashboards/generate.py

Dashboard JSON is verbose and repetitive, so the *.json files are generated from this
script. Change a name or a threshold here, re-run it, then `kubectl apply -k monitoring/dashboards`.

Names (namespace, Deployment, Service, ...) are NOT hard-coded in the queries. They are
hidden dashboard variables (Dashboard settings -> Variables) with the defaults in VARS below.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DS = {"type": "prometheus", "uid": "${datasource}"}

# Defaults for the hidden dashboard variables. They must match app/*.yaml and scalers/*.yaml.
VARS = {
    "namespace": "app",         # namespace of the demo app
    "web": "podinfo-d",         # web Deployment name (its pods are called <web>-<hash>-<id>)
    "job": "podinfo-svc",       # Prometheus `job` label of podinfo = name of its Service
    "worker": "worker-d",       # worker Deployment name
    "so_web": "podinfo.*",      # regex over the web ScaledObject names (podinfo, podinfo-rps, ...)
    "so_worker": "worker.*",    # regex over the worker ScaledObject names
}

# ---- selectors (plain strings: ${var} is resolved by Grafana, not by Python) -------------
NS = 'namespace="${namespace}"'
WEB = NS + ',job="${job}",path!~"readyz|healthz|metrics"'          # probes + /metrics scrapes excluded
WEB_PODS = NS + ',pod=~"${web}-.*"'
WEB_CTR = WEB_PODS + ',container!="",container!="POD"'
WRK_PODS = NS + ',pod=~"${worker}-.*"'
WRK_CTR = WRK_PODS + ',container!="",container!="POD"'
HPA_WEB = NS + ',horizontalpodautoscaler=~"keda-hpa-${so_web}"'
HPA_WRK = NS + ',horizontalpodautoscaler=~"keda-hpa-${so_worker}"'

# ---- reusable expressions ---------------------------------------------------------------
CNT = f'rate(http_request_duration_seconds_count{{{WEB}}}[1m])'
SUM = f'rate(http_request_duration_seconds_sum{{{WEB}}}[1m])'
RPS = f'sum({CNT}) or vector(0)'
RPS_POD = f'sum by (pod) ({CNT})'
CONC = f'sum({SUM}) or vector(0)'
CONC_POD = f'sum by (pod) ({SUM})'
AVG_DUR = f'sum({SUM}) / sum({CNT})'
AVG_DUR_POD = f'sum by (pod) ({SUM}) / sum by (pod) ({CNT})'


def q(p, by=""):
    return f'histogram_quantile({p}, sum by (le{by}) (rate(http_request_duration_seconds_bucket{{{WEB}}}[1m])))'


WEB_REPL = f'kube_deployment_status_replicas{{{NS},deployment="${{web}}"}}'
WEB_AVAIL = f'kube_deployment_status_replicas_available{{{NS},deployment="${{web}}"}}'
WEB_DESIRED = f'kube_horizontalpodautoscaler_status_desired_replicas{{{HPA_WEB}}}'
WRK_REPL = f'kube_deployment_status_replicas{{{NS},deployment="${{worker}}"}}'
WRK_DESIRED = f'kube_horizontalpodautoscaler_status_desired_replicas{{{HPA_WRK}}}'
KEDA_WEB = 'keda_scaler_metrics_value{exported_namespace="${namespace}",scaledObject=~"${so_web}"}'
KEDA_WRK = 'keda_scaler_metrics_value{exported_namespace="${namespace}",scaledObject=~"${so_worker}"}'
BACKLOG = f'sum(redis_key_size{{{NS},key="jobs"}}) or vector(0)'

CPU_POD = f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{WEB_CTR}}}[1m]))'
CPU_REQ_POD = f'sum by (pod) (kube_pod_container_resource_requests{{{WEB_PODS},resource="cpu"}})'
CPU_PCT_POD = f'{CPU_POD} / {CPU_REQ_POD}'
THROTTLE_POD = (f'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{WEB_CTR}}}[1m])) / '
                f'sum by (pod) (rate(container_cpu_cfs_periods_total{{{WEB_CTR}}}[1m]))')
MEM_POD = f'sum by (pod) (container_memory_working_set_bytes{{{WEB_CTR}}})'
RESTARTS_POD = f'sum by (pod) (kube_pod_container_status_restarts_total{{{WEB_PODS}}})'
REQ_5M_POD = f'sum by (pod) (increase(http_request_duration_seconds_count{{{WEB}}}[5m]))'

W_CPU_POD = f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{WRK_CTR}}}[1m]))'
W_MEM_POD = f'sum by (pod) (container_memory_working_set_bytes{{{WRK_CTR}}})'
W_RESTARTS_POD = f'sum by (pod) (kube_pod_container_status_restarts_total{{{WRK_PODS}}})'
W_AGE_POD = f'time() - max by (pod) (kube_pod_created{{{WRK_PODS}}})'


# ---- panel builders ---------------------------------------------------------------------
def _targets(pairs, **extra):
    out = []
    for i, (expr, legend) in enumerate(pairs):
        t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": chr(65 + i)}
        t.update(extra)
        out.append(t)
    return out


def stat(title, expr, unit="short", desc="", decimals=None):
    d = {"unit": unit, "color": {"mode": "fixed", "fixedColor": "blue"},
         "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}}
    if decimals is not None:
        d["decimals"] = decimals
    return {"type": "stat", "title": title, "description": desc, "datasource": DS,
            "targets": _targets([(expr, "")]),
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "value", "graphMode": "area", "textMode": "auto"},
            "fieldConfig": {"defaults": d, "overrides": []}}


def ts(title, pairs, unit="short", threshold=None, desc="", legend_table=False, dashed=(), min0=True):
    steps = [{"color": "green", "value": None}]
    if threshold is not None:
        steps.append({"color": "red", "value": threshold})
    legend = {"displayMode": "table", "placement": "bottom", "showLegend": True,
              "calcs": ["mean", "max", "lastNotNull"]} if legend_table else \
             {"displayMode": "list", "placement": "bottom", "showLegend": True}
    defaults = {"unit": unit, "color": {"mode": "palette-classic"},
                "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 8, "showPoints": "never",
                           "lineInterpolation": "linear", "spanNulls": True,
                           "thresholdsStyle": {"mode": "line" if threshold is not None else "off"}},
                "thresholds": {"mode": "absolute", "steps": steps}}
    if min0:
        defaults["min"] = 0
    overrides = [{"matcher": {"id": "byName", "options": n},
                  "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                                 {"id": "custom.fillOpacity", "value": 0}]} for n in dashed]
    return {"type": "timeseries", "title": title, "description": desc, "datasource": DS,
            "targets": _targets(pairs),
            "options": {"legend": legend, "tooltip": {"mode": "multi", "sort": "desc"}},
            "fieldConfig": {"defaults": defaults, "overrides": overrides}}


def bar(title, expr, unit="short", desc=""):
    return {"type": "bargauge", "title": title, "description": desc, "datasource": DS,
            "targets": _targets([(expr, "{{pod}}")], instant=True),
            "options": {"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
            "fieldConfig": {"defaults": {"unit": unit, "min": 0, "color": {"mode": "continuous-BlPu"},
                                         "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}},
                            "overrides": []}}


def heat(title, expr, desc=""):
    t = _targets([(expr, "{{le}}")])
    t[0]["format"] = "heatmap"
    return {"type": "heatmap", "title": title, "description": desc, "datasource": DS, "targets": t,
            "options": {"calculate": False, "cellGap": 1, "legend": {"show": True},
                        "yAxis": {"axisPlacement": "left", "unit": "s"},
                        "rowsFrame": {"layout": "auto"},
                        "color": {"mode": "scheme", "scheme": "Oranges", "fill": "dark-orange", "scale": "exponential",
                                  "exponent": 0.5, "steps": 64, "reverse": False},
                        "tooltip": {"mode": "single", "showColorScale": False}},
            "fieldConfig": {"defaults": {"custom": {"scaleDistribution": {"type": "linear"}}}, "overrides": []}}


def tbl(title, cols, desc=""):
    """One row per pod. `cols` = [(column header, PromQL returning one value per `pod`), ...]."""
    targets, rename, index = [], {"pod": "Pod"}, {"pod": 0}
    for i, (header, expr) in enumerate(cols):
        ref = chr(65 + i)
        targets.append({"datasource": DS, "expr": expr, "format": "table", "instant": True,
                        "legendFormat": "", "refId": ref})
        rename[f"Value #{ref}"] = header
        index[f"Value #{ref}"] = i + 1
    return {"type": "table", "title": title, "description": desc, "datasource": DS, "targets": targets,
            "transformations": [{"id": "merge", "options": {}},
                                {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                               "indexByName": index, "renameByName": rename}}],
            "options": {"showHeader": True, "sortBy": [{"displayName": "Pod", "desc": False}]},
            "fieldConfig": {"defaults": {"decimals": 2, "custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                            "overrides": []}}


def text(content):
    return {"type": "text", "title": "", "options": {"mode": "markdown", "content": content}, "transparent": True}


def layout(rows):
    """rows = [(height, [panels...]), ...]; panels in a row share the 24 columns equally."""
    out, y, pid = [], 0, 1
    for height, panels in rows:
        width = 24 // len(panels)
        for i, p in enumerate(panels):
            p["id"] = pid
            pid += 1
            p["gridPos"] = {"x": i * width, "y": y, "w": width, "h": height}
            out.append(p)
        y += height
    return out


def dashboard(uid, title, description, rows):
    variables = [{"type": "datasource", "name": "datasource", "label": "Data source", "query": "prometheus",
                  "current": {}, "hide": 0, "refresh": 1, "regex": "", "options": []}]
    for name, value in VARS.items():
        variables.append({"type": "constant", "name": name, "label": name, "query": value, "hide": 2,
                          "current": {"text": value, "value": value},
                          "options": [{"selected": True, "text": value, "value": value}]})
    return {"uid": uid, "title": title, "description": description, "tags": ["keda", "keda-demo"],
            "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True,
            "refresh": "5s", "time": {"from": "now-15m", "to": "now"}, "graphTooltip": 1,
            "links": [{"type": "dashboards", "title": "KEDA dashboards", "tags": ["keda-demo"], "asDropdown": True,
                       "includeVars": False, "keepTime": True, "icon": "external link", "tooltip": "", "url": "",
                       "targetBlank": False}],
            "templating": {"list": variables}, "annotations": {"list": []}, "panels": layout(rows)}


# ---- shared panel groups ------------------------------------------------------------------
def replicas_panel(title="Web pods: desired (HPA) vs current"):
    return ts(title, [(WEB_DESIRED, "desired (HPA)"), (WEB_REPL, "current"), (WEB_AVAIL, "available")],
              desc="'desired' is what the HPA (created by KEDA) asks for; 'current' is what exists; 'available' passed readiness.")


def keda_web_panel(desc):
    return ts("KEDA: metric value handed to the HPA", [(KEDA_WEB, "{{scaledObject}} / {{metric}}")], desc=desc)


def rps_by_pod_panel(threshold=None, desc="Requests per second served by each pod (probes and /metrics scrapes excluded)."):
    return ts("Requests/s on each pod", [(RPS_POD, "{{pod}}")], "reqps", threshold=threshold, desc=desc,
              legend_table=True)


# =========================================================================================
# 0. Overview (all five metrics on one page)
# =========================================================================================
def overview():
    return dashboard(
        "keda-prom-autoscaling", "KEDA + Prometheus Autoscaling",
        "Overview of the 5 Prometheus metrics KEDA scales on. Use the 'KEDA dashboards' dropdown for one dashboard per load type.",
        [(3, [text("**Overview of everything.** For one dashboard per load type (with per-pod breakdowns) use the "
                   "**KEDA dashboards** dropdown at the top: RPS, latency, concurrency, CPU, queue.")]),
         (4, [stat("Web pods", WEB_REPL), stat("Requests / s", RPS, "reqps"), stat("p95 latency", q(0.95), "s"),
              stat("Concurrency", CONC, desc="Average in-flight requests (Little's law)"),
              stat("Queue backlog", BACKLOG), stat("Worker pods", WRK_REPL)]),
         (8, [replicas_panel(),
              ts("1. Requests/s per pod (threshold 50)", [(f'({RPS}) / scalar({WEB_REPL})', "req/s per pod"), (RPS, "req/s total")],
                 "reqps", threshold=50, dashed=["req/s total"], desc="01-rps.yaml: the HPA keeps the PER-POD value near the red line.")]),
         (8, [ts("2. Latency p50 / p95 / p99 (threshold 100ms on p95)", [(q(0.5), "p50"), (q(0.95), "p95"), (q(0.99), "p99")],
                 "s", threshold=0.1, desc="02-latency-p95.yaml scales on p95 (metricType Value)."),
              ts("3. Concurrency per pod (threshold 5)", [(f'({CONC}) / scalar({WEB_REPL})', "in-flight per pod"), (CONC, "in-flight total")],
                 threshold=5, dashed=["in-flight total"], desc="03-concurrency.yaml: average requests in flight, from rate(duration_sum).")]),
         (8, [ts("4. CPU per pod, cores (request 0.05, threshold 0.035)", [(CPU_POD, "{{pod}}")], threshold=0.035,
                 desc="04-cpu.yaml: cAdvisor CPU read from Prometheus. Red line = per-pod scaling target."),
              keda_web_panel("Straight from KEDA's own /metrics. Flat 0 usually means the PromQL query matches nothing.")]),
         (8, [ts("5. Queue backlog and backlog per worker (threshold 10)",
                 [(BACKLOG, "backlog (jobs)"), (f'({BACKLOG}) / clamp_min(sum({WRK_REPL}), 1)', "backlog per worker")],
                 threshold=10, dashed=["backlog per worker"], desc="05-queue-backlog.yaml: redis_key_size{key='jobs'} from redis_exporter."),
              ts("Worker pods: desired (HPA) vs current (0 -> N -> 0)", [(WRK_DESIRED, "desired (HPA)"), (WRK_REPL, "current")],
                 desc="At 0 the HPA is inactive: KEDA itself handles the 0<->1 transition.")])])


# =========================================================================================
# 1. RPS
# =========================================================================================
def rps_dashboard():
    return dashboard(
        "keda-1-rps", "KEDA 1 - RPS (loadgen: rps)",
        "Scaler scalers/01-rps.yaml with load loadgen/rps.yaml. Per-pod request counts.",
        [(4, [text("**Scaler** `scalers/01-rps.yaml` &nbsp;·&nbsp; **Load** `loadgen/rps.yaml` (steady 200 req/s) &nbsp;·&nbsp; "
                   "**Expect** 1 → ~4 pods, each serving ~50 req/s.\n\n"
                   "Replicas = `ceil(total req/s ÷ 50)`. The red line on *Requests/s on each pod* is that target: every pod's line "
                   "should settle near it. The load generator recycles its connections (`-connection-reuse`), so pods added during the "
                   "scale-up start receiving traffic within a couple of seconds; with plain keep-alive connections they would get none.")]),
         (4, [stat("Total req/s", RPS, "reqps"), stat("Web pods", WEB_REPL), stat("HPA desired", WEB_DESIRED),
              stat("Avg req/s per pod", f'({RPS}) / scalar({WEB_REPL})', "reqps", "The number the HPA compares with the 50 target"),
              stat("Busiest pod", f'max({RPS_POD})', "reqps"), stat("Quietest pod", f'min({RPS_POD})', "reqps")]),
         (8, [rps_by_pod_panel(50, "One line per pod. Red line = scaling target (50 req/s per pod)."),
              ts("Total req/s vs capacity (50 x pods)", [(RPS, "total req/s"), (f'50 * {WEB_REPL}', "capacity (50 per pod x pods)")],
                 "reqps", dashed=["capacity (50 per pod x pods)"],
                 desc="When total rises above capacity the HPA adds pods; capacity then steps up to meet it.")]),
         (8, [replicas_panel(), keda_web_panel("For this scaler the value is the total req/s; the HPA divides it by 50 per pod.")]),
         (8, [bar("Requests served per pod, last 5 min", REQ_5M_POD, desc="How the load was split. New pods start at 0 and catch up."),
              bar("Requests served per pod, since the pod started",
                  f'sum by (pod) (http_request_duration_seconds_count{{{WEB}}})', desc="Raw counter per pod (resets when a pod restarts).")]),
         (9, [tbl("Per-pod detail", [("req/s", RPS_POD), ("requests (5 min)", REQ_5M_POD),
                                    ("CPU (millicores)", f'1000 * {CPU_POD}'), ("memory (MiB)", f'{MEM_POD} / 1048576'),
                                    ("restarts", RESTARTS_POD)])])])


# =========================================================================================
# 2. Latency
# =========================================================================================
def latency_dashboard():
    return dashboard(
        "keda-2-latency", "KEDA 2 - Latency p95 (loadgen: latency)",
        "Scaler scalers/02-latency-p95.yaml with load loadgen/latency.yaml. Latency percentiles and per-pod p95.",
        [(4, [text("**Scaler** `scalers/02-latency-p95.yaml` &nbsp;·&nbsp; **Load** `loadgen/latency.yaml` (10 connections × `/delay/1`) "
                   "&nbsp;·&nbsp; **Expect** p95 jumps far above the 100 ms target, so the pods run quickly to the maximum (10) and stay there.\n\n"
                   "p95 reads ~2.4 s, not 1 s: the histogram has 1 s and 2.5 s buckets and `histogram_quantile` interpolates inside the bucket. "
                   "Adding pods cannot make a deliberately slow endpoint faster, so latency stays high. That is the trap of scaling on latency "
                   "alone; see the notes in `scalers/02-latency-p95.yaml`.")]),
         (4, [stat("p50", q(0.5), "s", decimals=3), stat("p95", q(0.95), "s", decimals=3), stat("p99", q(0.99), "s", decimals=3),
              stat("Average latency", AVG_DUR, "s", decimals=3), stat("Requests / s", RPS, "reqps"), stat("Web pods", WEB_REPL)]),
         (8, [ts("Latency p50 / p95 / p99, all pods (target 100 ms on p95)", [(q(0.5), "p50"), (q(0.95), "p95"), (q(0.99), "p99")],
                 "s", threshold=0.1, desc="The scaler uses p95 with metricType: Value (replicas = ceil(current x p95 / 0.1))."),
              ts("p95 latency on each pod", [(q(0.95, ", pod"), "{{pod}}")], "s", threshold=0.1, legend_table=True,
                 desc="Per-pod p95. Pods with no traffic yet show no line.")]),
         (8, [heat("Latency distribution (all pods)", f'sum by (le) (increase(http_request_duration_seconds_bucket{{{WEB}}}[1m]))',
                   "Each row is a latency bucket (seconds); colour = number of requests. Bands show where requests actually land."),
              rps_by_pod_panel(desc="Requests/s per pod. Only ~10 req/s overall: this load is about slowness, not volume.")]),
         (8, [replicas_panel(), keda_web_panel("The p95 (in seconds) that KEDA hands to the HPA, compared with the 0.1 target.")]),
         (9, [tbl("Per-pod latency", [("req/s", RPS_POD), ("p50 (ms)", f'1000 * {q(0.5, ", pod")}'),
                                     ("p95 (ms)", f'1000 * {q(0.95, ", pod")}'), ("p99 (ms)", f'1000 * {q(0.99, ", pod")}'),
                                     ("avg (ms)", f'1000 * {AVG_DUR_POD}')])])])


# =========================================================================================
# 3. Concurrency
# =========================================================================================
def concurrency_dashboard():
    return dashboard(
        "keda-3-concurrency", "KEDA 3 - Concurrency (loadgen: slow)",
        "Scaler scalers/03-concurrency.yaml with load loadgen/slow.yaml. In-flight requests per pod.",
        [(4, [text("**Scaler** `scalers/03-concurrency.yaml` &nbsp;·&nbsp; **Load** `loadgen/slow.yaml` (20 connections × `/delay/2`) "
                   "&nbsp;·&nbsp; **Expect** ~4-5 pods, each with ~5 requests in flight.\n\n"
                   "Only ~10 req/s but ~20 requests are *in flight* at any moment, so RPS-based scaling would stay at 1 pod while concurrency "
                   "scaling reacts. Replicas = `ceil(in-flight ÷ 5)`. In-flight is derived with Little's law: `rate(duration_sum)`.")]),
         (4, [stat("In flight (total)", CONC, decimals=1), stat("Avg in flight per pod", f'({CONC}) / scalar({WEB_REPL})', decimals=1,
                                                               desc="The number the HPA compares with the 5 target"),
              stat("Web pods", WEB_REPL), stat("HPA desired", WEB_DESIRED), stat("Requests / s", RPS, "reqps"),
              stat("Avg request duration", AVG_DUR, "s", decimals=2)]),
         (8, [ts("In-flight requests on each pod (target 5)", [(CONC_POD, "{{pod}}")], threshold=5, legend_table=True,
                 desc="One line per pod; the HPA keeps these near the red line."),
              ts("Requests/s vs requests in flight", [(RPS, "requests/s"), (CONC, "in flight")],
                 desc="Why concurrency matters: slow requests keep many in flight while requests/s stays low.")]),
         (8, [ts("Average request duration", [(AVG_DUR, "avg duration")], "s",
                 desc="Roughly 2 s here: in-flight = requests/s x duration."),
              replicas_panel()]),
         (8, [keda_web_panel("Total in-flight requests, which the HPA divides by 5 per pod."),
              bar("Requests served per pod, last 5 min", REQ_5M_POD, desc="How the slow requests were spread across pods.")]),
         (9, [tbl("Per-pod detail", [("in flight", CONC_POD), ("req/s", RPS_POD), ("avg duration (s)", AVG_DUR_POD),
                                    ("CPU (millicores)", f'1000 * {CPU_POD}'), ("memory (MiB)", f'{MEM_POD} / 1048576')])])])


# =========================================================================================
# 4. CPU
# =========================================================================================
def cpu_dashboard():
    return dashboard(
        "keda-4-cpu", "KEDA 4 - CPU (loadgen: saturate)",
        "Scaler scalers/04-cpu.yaml with load loadgen/saturate.yaml. CPU, throttling and memory per pod.",
        [(4, [text("**Scaler** `scalers/04-cpu.yaml` &nbsp;·&nbsp; **Load** `loadgen/saturate.yaml` (~3000 req/s) &nbsp;·&nbsp; "
                   "**Expect** ~3-4 pods, each near 35 millicores (70% of its 50m request).\n\n"
                   "Replicas = `ceil(total CPU cores ÷ 0.035)`. A pod's limit is 100m, so a single pod gets CPU-throttled under this load: "
                   "watch *CPU throttling per pod*. CPU data comes from cAdvisor via the kubelet ServiceMonitor, not from metrics-server.")]),
         (4, [stat("Total CPU (cores)", f'sum({CPU_POD})', decimals=3), stat("Avg CPU per pod (cores)", f'avg({CPU_POD})', decimals=3),
              stat("Avg CPU % of request", f'100 * avg({CPU_PCT_POD})', "percent", "Target is 70%", decimals=0),
              stat("Avg throttled", f'100 * avg({THROTTLE_POD})', "percent", "Share of CPU periods in which the pod hit its limit", decimals=0),
              stat("Web pods", WEB_REPL), stat("Requests / s", RPS, "reqps")]),
         (8, [ts("CPU usage on each pod, cores (target 0.035)",
                 [(CPU_POD, "{{pod}}"),
                  (f'max(kube_pod_container_resource_requests{{{WEB_PODS},resource="cpu"}})', "request"),
                  (f'max(kube_pod_container_resource_limits{{{WEB_PODS},resource="cpu"}})', "limit")],
                 threshold=0.035, dashed=["request", "limit"], legend_table=True,
                 desc="Red line = per-pod scaling target (70% of the request). Dashed = the pod's request and limit."),
              ts("CPU as % of request, per pod (target 70%)", [(CPU_PCT_POD, "{{pod}}")], "percentunit", threshold=0.7, legend_table=True,
                 desc="Usage divided by the CPU request. 100% = using exactly what it asked for.")]),
         (8, [ts("CPU throttling per pod", [(THROTTLE_POD, "{{pod}}")], "percentunit", legend_table=True,
                 desc="Fraction of scheduling periods where the pod ran out of its CPU limit. High = saturated, even if latency looks fine."),
              keda_web_panel("Total CPU cores, which the HPA divides by 0.035 per pod.")]),
         (8, [replicas_panel(), rps_by_pod_panel(desc="The cause: requests per second on each pod.")]),
         (8, [ts("Memory working set per pod", [(MEM_POD, "{{pod}}")], "bytes", legend_table=True),
              bar("CPU right now, millicores per pod", f'1000 * {CPU_POD}', desc="Instantaneous view of how evenly the load is shared.")]),
         (9, [tbl("Per-pod detail", [("CPU (millicores)", f'1000 * {CPU_POD}'), ("CPU % of request", f'100 * {CPU_PCT_POD}'),
                                    ("throttled %", f'100 * {THROTTLE_POD}'), ("memory (MiB)", f'{MEM_POD} / 1048576'),
                                    ("req/s", RPS_POD), ("restarts", RESTARTS_POD)])])])


# =========================================================================================
# 5. Queue backlog (worker)
# =========================================================================================
def queue_dashboard():
    return dashboard(
        "keda-5-queue", "KEDA 5 - Queue backlog (loadgen: queue)",
        "Scaler scalers/05-queue-backlog.yaml with load loadgen/queue.yaml. Backlog, workers and Redis.",
        [(4, [text("**Scaler** `scalers/05-queue-backlog.yaml` &nbsp;·&nbsp; **Load** `loadgen/queue.yaml` (pushes 100 jobs) &nbsp;·&nbsp; "
                   "**Expect** workers 0 → several (up to 10) → back to 0 once the queue is empty.\n\n"
                   "Replicas = `ceil(backlog ÷ 10)`. The workers are Redis consumers (`worker-d`); each takes ~2 s per job. "
                   "The backlog is `redis_key_size{key=\"jobs\"}` from the redis_exporter sidecar. The workers expose no metrics of their own, "
                   "so per-worker numbers here are pod CPU/memory, and throughput is estimated from how fast the backlog falls.")]),
         (4, [stat("Queue backlog", BACKLOG), stat("Worker pods", WRK_REPL), stat("HPA desired", WRK_DESIRED),
              stat("Backlog per worker", f'({BACKLOG}) / clamp_min(sum({WRK_REPL}), 1)', desc="The HPA aims for 10"),
              stat("Jobs drained / s (approx.)", f'clamp_min(-deriv(sum(redis_key_size{{{NS},key="jobs"}})[1m:15s]), 0)', decimals=2,
                   desc="Estimated from the falling backlog; only meaningful while nothing is being pushed"),
              stat("Redis clients", f'sum(redis_connected_clients{{{NS}}})', desc="Roughly workers + the exporter")]),
         (8, [ts("Queue backlog (jobs waiting)", [(BACKLOG, "backlog")], desc="Jumps when the producer pushes, then drains as workers scale up."),
              ts("Backlog per worker (target 10)", [(f'({BACKLOG}) / clamp_min(sum({WRK_REPL}), 1)', "backlog per worker")], threshold=10,
                 desc="With 0 workers this equals the whole backlog; the HPA scales out until it falls to ~10.")]),
         (8, [ts("Worker pods: desired (HPA) vs current (0 -> N -> 0)", [(WRK_DESIRED, "desired (HPA)"), (WRK_REPL, "current")],
                 desc="At 0 the HPA is inactive: KEDA itself handles the 0<->1 transition (that is why minReplicaCount: 0 works)."),
              ts("KEDA: metric value handed to the HPA", [(KEDA_WRK, "{{scaledObject}} / {{metric}}")],
                 desc="The backlog as KEDA sees it, which the HPA divides by 10 per pod.")]),
         (8, [ts("Jobs drained per second (approx.)", [(f'clamp_min(-deriv(sum(redis_key_size{{{NS},key="jobs"}})[1m:15s]), 0)', "jobs/s")],
                 desc="Rate of decrease of the backlog. Each worker handles ~0.5 job/s (2 s per job)."),
              ts("Redis connected clients", [(f'sum(redis_connected_clients{{{NS}}})', "clients")],
                 desc="Each blocked worker (BLPOP) holds a connection, so this follows the worker count.")]),
         (8, [ts("Redis commands / s", [(f'sum(rate(redis_commands_processed_total{{{NS}}}[1m]))', "commands/s")],
                 desc="Workers polling with BLPOP, the producer's RPUSHes and the exporter's scrapes."),
              ts("Worker CPU per pod (millicores)", [(f'1000 * {W_CPU_POD}', "{{pod}}")], legend_table=True)]),
         (9, [tbl("Worker pods", [("age (s)", W_AGE_POD), ("CPU (millicores)", f'1000 * {W_CPU_POD}'),
                                 ("memory (MiB)", f'{W_MEM_POD} / 1048576'), ("restarts", W_RESTARTS_POD)],
                  desc="Pods that exist right now. Empty when the worker is scaled to 0.")])])


DASHBOARDS = {
    "keda-prometheus.json": overview,
    "keda-1-rps.json": rps_dashboard,
    "keda-2-latency.json": latency_dashboard,
    "keda-3-concurrency.json": concurrency_dashboard,
    "keda-4-cpu.json": cpu_dashboard,
    "keda-5-queue.json": queue_dashboard,
}

if __name__ == "__main__":
    for filename, build in DASHBOARDS.items():
        with open(os.path.join(HERE, filename), "w") as f:
            json.dump(build(), f, indent=2)
            f.write("\n")
        print("wrote", filename)
