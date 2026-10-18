# Everything is pinned to ONE minikube profile via --context / --kube-context, so
# these targets never touch whatever cluster your current kubectl context points at.

PROFILE      ?= custom-metrics-hpa
KUBECTL      := kubectl --context $(PROFILE)
HELM         := helm --kube-context $(PROFILE)

# Chart versions: prometheus-community/kube-prometheus-stack and kedacore/keda
KPS_VERSION  ?= 92.10.1
KEDA_VERSION ?= 2.21.0

# S = scaler file in scalers/ (without .yaml), L = load file in loadgen/ (without .yaml)
S ?= 01-rps
L ?= rps

.PHONY: help up cluster monitoring keda app dashboards scaler combined load stop-load \
        watch status grafana prometheus unscale destroy

help:
	@echo "make up                 create cluster + Prometheus/Grafana + KEDA + app + dashboard"
	@echo "make scaler S=01-rps    activate a scaler (01-rps 02-latency-p95 03-concurrency 04-cpu 05-queue-backlog 06-combined)"
	@echo "make load L=rps         start a load test   (rps slow saturate latency queue)"
	@echo "make stop-load          stop all load tests"
	@echo "make watch              live view of HPA + pods"
	@echo "make grafana            open Grafana at http://localhost:3000  (admin / admin)"
	@echo "make prometheus         open Prometheus at http://localhost:9090"
	@echo "make unscale            remove all ScaledObjects"
	@echo "make destroy            delete the whole minikube profile"

up: cluster monitoring keda app dashboards

cluster:
	minikube start -p $(PROFILE) --cpus=4 --memory=4096 --keep-context

monitoring:
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
	helm repo update prometheus-community
	$(HELM) upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
		--version $(KPS_VERSION) -n monitoring --create-namespace \
		-f cluster/kube-prometheus-stack-values.yaml --wait --timeout 10m

keda:
	helm repo add kedacore https://kedacore.github.io/charts --force-update
	helm repo update kedacore
	$(HELM) upgrade --install keda kedacore/keda \
		--version $(KEDA_VERSION) -n keda --create-namespace \
		-f cluster/keda-values.yaml --wait --timeout 5m

app:
	$(KUBECTL) apply -f app/00-namespace.yaml
	$(KUBECTL) apply -f app/web.yaml -f app/worker.yaml
	$(KUBECTL) -n app rollout status deploy/podinfo-d deploy/redis-d --timeout=3m

dashboards:
	$(KUBECTL) apply -k monitoring/dashboards

# Web scalers (01-04, 06) all use the ScaledObject name "podinfo", so applying a
# different file just swaps the triggers on the same object.
scaler:
	$(KUBECTL) apply -f scalers/$(S).yaml
	@echo "--- watch it: make watch"

load:
	-$(KUBECTL) -n app delete job load-$(L) --ignore-not-found
	$(KUBECTL) apply -f loadgen/$(L).yaml

stop-load:
	-$(KUBECTL) -n app delete jobs -l app=loadgen --ignore-not-found

# Portable replacement for `watch` (not installed by default on macOS). Ctrl-C to exit.
watch:
	@while true; do clear; date; $(KUBECTL) -n app get scaledobject,hpa,deploy,pods; sleep 2; done

status:
	$(KUBECTL) -n app get scaledobject,hpa,deploy,pods

grafana:
	@echo "Grafana: http://localhost:3000  (admin / admin)  -> Dashboards -> KEDA + Prometheus"
	$(KUBECTL) -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80

prometheus:
	@echo "Prometheus: http://localhost:9090"
	$(KUBECTL) -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090

unscale:
	-$(KUBECTL) -n app delete scaledobject --all

destroy:
	minikube delete -p $(PROFILE)
