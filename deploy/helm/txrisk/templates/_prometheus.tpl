{{/* Prometheus's configuration. Targets are found by DNS on each service's headless metrics Service,
so every replica of a scaled service (--scale worker, the autoscaler) is a target. */}}
{{- define "txrisk.prometheusConfig" -}}
{{- $domain := printf "%s.svc.%s" .Release.Namespace .Values.clusterDomain -}}
global:
  scrape_interval: 5s
  evaluation_interval: 15s
rule_files:
  - /etc/prometheus/rules/*.yml
alerting:
  alertmanagers:
    - static_configs:
        - targets: [alertmanager:9093]
scrape_configs:
  {{- range $job, $port := dict "api" 9101 "worker" 9100 "outbox" 9102 "scheduler" 9103 }}
  - job_name: {{ $job }}
    dns_sd_configs:
      - names: [{{ $job }}-metrics.{{ $domain }}]
        type: A
        port: {{ $port }}
        refresh_interval: 5s
  {{- end }}
  # The monitoring job is a one-off pod that exits in seconds: it pushes its metrics here.
  - job_name: pushgateway
    honor_labels: true
    static_configs:
      - targets: [pushgateway:9091]
  - job_name: alert-sink
    static_configs:
      - targets: [alert-sink:9095]
  - job_name: alertmanager
    static_configs:
      - targets: [alertmanager:9093]
  - job_name: prometheus
    static_configs:
      - targets: [localhost:9090]
{{- end }}
