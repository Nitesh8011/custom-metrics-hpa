# cluster/: installing the Helm charts

Two charts, installed in this order:

| Order | Chart | Version | Namespace | Values file | Gives you |
|---|---|---|---|---|---|
| 1 | `prometheus-community/kube-prometheus-stack` | 92.10.1 | `monitoring` | [kube-prometheus-stack-values.yaml](kube-prometheus-stack-values.yaml) | Prometheus, Grafana, kube-state-metrics, node-exporter, Prometheus Operator CRDs |
| 2 | `kedacore/keda` | 2.21.0 | `keda` | [keda-values.yaml](keda-values.yaml) | KEDA operator, metrics API server, KEDA CRDs (`ScaledObject`, ...) |

**Why this order:** `keda-values.yaml` makes KEDA create `ServiceMonitor` objects so Prometheus scrapes KEDA. That CRD comes from kube-prometheus-stack, so KEDA would fail to install without it.

All commands below are run from the **repo root**, against whatever cluster your current `kubectl` context points at. (The Makefile runs the same commands but pins them to the `custom-metrics-hpa` minikube profile with `--kube-context`.)

## 0. A cluster

```bash
minikube start -p custom-metrics-hpa --cpus=4 --memory=4096 --keep-context
kubectl config use-context custom-metrics-hpa      # or add --context custom-metrics-hpa to every command below
```

## 1. Add the chart repositories

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add kedacore https://kedacore.github.io/charts
helm repo update

# see which versions exist (the pinned ones above are the latest at the time of writing)
helm search repo prometheus-community/kube-prometheus-stack --versions | head -5
helm search repo kedacore/keda --versions | head -5
```

## 2. Install kube-prometheus-stack (Prometheus + Grafana)

```bash
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --version 92.10.1 \
  --namespace monitoring --create-namespace \
  -f cluster/kube-prometheus-stack-values.yaml \
  --wait --timeout 10m
```

The release name `kube-prometheus-stack` matters: it makes the Prometheus Service `kube-prometheus-stack-prometheus`, which is the address every ScaledObject in [../scalers/](../scalers/) queries (`http://kube-prometheus-stack-prometheus.monitoring:9090`). If you rename the release, change `serverAddress` in the scalers too.

## 3. Install KEDA

```bash
helm upgrade --install keda kedacore/keda \
  --version 2.21.0 \
  --namespace keda --create-namespace \
  -f cluster/keda-values.yaml \
  --wait --timeout 5m
```

## 4. Check both are healthy

```bash
kubectl -n monitoring get pods          # grafana, operator, kube-state-metrics, node-exporter, prometheus-...-0 all Running
kubectl -n keda get pods                # keda-operator, keda-operator-metrics-apiserver, keda-admission-webhooks Running

kubectl get apiservice v1beta1.external.metrics.k8s.io     # AVAILABLE should be True: this is how the HPA reads KEDA's metrics
kubectl get crd | grep -E 'keda.sh|monitoring.coreos.com'  # ScaledObject, ServiceMonitor, ...
helm list -A                                               # both releases "deployed"
```

## 5. Deploy the app and the dashboard

```bash
kubectl apply -f app/00-namespace.yaml
kubectl apply -f app/web.yaml -f app/worker.yaml
kubectl apply -k monitoring/dashboards          # Grafana's sidecar loads it automatically
```

After this, **wait ~2 minutes** before testing: Prometheus takes a moment to start scraping the new ServiceMonitors (Status -> Targets should list `app/podinfo-sm` and `app/redis-sm`).

## 6. Open the UIs

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80        # http://localhost:3000  (admin / admin)
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090   # http://localhost:9090
```

## Changing settings later

Edit the values file, then re-run the same `helm upgrade --install ...` command from step 2 or 3. To preview what would change first (needs the `helm-diff` plugin): `helm diff upgrade ...`.

To see every setting a chart supports:

```bash
helm show values prometheus-community/kube-prometheus-stack --version 92.10.1 | less
helm show values kedacore/keda --version 2.21.0 | less
```

## Uninstall (reverse order)

```bash
kubectl delete -k monitoring/dashboards
kubectl delete -f app/web.yaml -f app/worker.yaml -f app/00-namespace.yaml
helm uninstall keda -n keda
helm uninstall kube-prometheus-stack -n monitoring
kubectl delete namespace keda monitoring
```

`helm uninstall` normally leaves CRDs behind. To remove them too (this deletes every ScaledObject / ServiceMonitor in the cluster):

```bash
kubectl get crd -o name | grep -E 'keda.sh|monitoring.coreos.com' | xargs kubectl delete
```

Or just throw the whole cluster away: `minikube delete -p custom-metrics-hpa`.
