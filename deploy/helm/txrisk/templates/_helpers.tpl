{{/* Labels every object carries; `component` tells the stack's parts apart. */}}
{{- define "txrisk.labels" -}}
app.kubernetes.io/name: txrisk
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version }}
{{- end }}

{{- define "txrisk.selector" -}}
app.kubernetes.io/name: txrisk
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "txrisk.image" -}}
{{ .repository }}:{{ .tag }}
{{- end }}

{{/* Pods of the txrisk image run as the image's own non-root user, with nothing extra. */}}
{{- define "txrisk.podSecurity" -}}
runAsNonRoot: true
runAsUser: {{ .Values.podSecurity.runAsUser }}
runAsGroup: {{ .Values.podSecurity.runAsGroup }}
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "txrisk.containerSecurity" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: [ALL]
{{- end }}

{{/* What every service that stores or scores Transactions needs to find its neighbours. */}}
{{- define "txrisk.environment" -}}
- name: TXRISK_OIDC_ISSUER
  value: {{ .Values.signIn.issuer | quote }}
- name: TXRISK_OIDC_JWKS_URL
  value: {{ .Values.signIn.jwksUrl | quote }}
- name: TXRISK_OIDC_ALLOWED_CLIENTS
  value: {{ .Values.signIn.allowedClients | quote }}
- name: TXRISK_KAFKA_BOOTSTRAP_SERVERS
  value: redpanda:9092
- name: TXRISK_REDIS_URL
  value: redis://redis:6379/0
- name: TXRISK_TRACING_ENABLED
  value: {{ .Values.tracing.enabled | quote }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: http://jaeger:4318
{{- if .Values.corsOrigins }}
- name: TXRISK_CORS_ORIGINS
  value: {{ .Values.corsOrigins | quote }}
{{- end }}
{{- if .Values.modelBundle }}
- name: TXRISK_MODEL_BUNDLE
  value: {{ .Values.modelBundle | quote }}
{{- end }}
{{- end }}

{{/* The API, workers and outbox connect as txrisk_app, whose login the application reads from its
own environment, so the URL names the role and nothing more. */}}
{{- define "txrisk.appDatabase" -}}
- name: TXRISK_DATABASE_URL
  value: postgresql://txrisk_app@postgres:5432/txrisk
- name: HOME
  value: /tmp
{{- end }}

{{- define "txrisk.ownerDatabase" -}}
- name: TXRISK_DATABASE_URL
  value: postgresql://txrisk@postgres:5432/txrisk
- name: HOME
  value: /tmp
{{- end }}

{{/* Starts the pod only once `txrisk migrate` has finished (the role it creates can log in and no
migration is pending). */}}
{{- define "txrisk.waitForMigration" -}}
- name: wait-for-migration
  image: {{ include "txrisk.image" .Values.image.txrisk }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command:
    - python
    - -c
    - |
      import os, time
      from txrisk.store import create_app_engine
      from txrisk.store.database import pending_migrations
      engine = create_app_engine(os.environ["TXRISK_DATABASE_URL"])
      for _ in range(300):
          try:
              with engine.connect() as connection:
                  if not pending_migrations(connection):
                      raise SystemExit(0)
          except SystemExit:
              raise
          except Exception:
              pass
          time.sleep(2)
      raise SystemExit("the migrations did not finish")
  env:
    {{- include "txrisk.appDatabase" . | nindent 4 }}
  envFrom:
    - secretRef:
        name: {{ .Values.settingsFrom.app }}
  securityContext:
    {{- include "txrisk.containerSecurity" . | nindent 4 }}
  resources:
    requests: {cpu: 10m, memory: 128Mi}
    limits: {cpu: 500m, memory: 768Mi}
{{- end }}

{{/* The models volume, read-only unless a job saves bundles to it. */}}
{{- define "txrisk.modelsMount" -}}
- name: models
  mountPath: /models
  readOnly: true
{{- end }}

{{- define "txrisk.modelsVolume" -}}
- name: models
  persistentVolumeClaim:
    claimName: models
{{- end }}

{{/* A Service for metrics that resolves to every pod behind it, so Prometheus scrapes each replica. */}}
{{- define "txrisk.metricsService" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ .component }}-metrics
  labels:
    {{- include "txrisk.labels" . | nindent 4 }}
spec:
  clusterIP: None
  selector:
    {{- include "txrisk.selector" . | nindent 4 }}
  ports:
    - name: metrics
      port: {{ .port }}
{{- end }}
