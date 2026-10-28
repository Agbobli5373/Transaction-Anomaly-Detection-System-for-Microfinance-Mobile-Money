# Runbook: the stack on Kubernetes (kind)

The Helm chart in `deploy/helm/txrisk` deploys everything `docker-compose.yml` runs, to a local kind
cluster: Postgres, Redis, Redpanda, Keycloak, the migration job, the API, the scoring workers, the outbox
publisher, the scheduler, the Case workbench, Prometheus, the Pushgateway, Alertmanager, the alert sink,
Grafana and Jaeger. The `kubernetes` CI job does exactly the steps below on every change and then runs
`deploy/kind/smoke.sh`.

## Deploy

```bash
make kind-up        # builds txrisk:dev and txrisk-workbench:dev, creates the kind cluster, loads them
make kind-deploy    # creates the Secrets, installs metrics-server and the chart, waits until ready
make kind-smoke     # scores a Transaction end to end and checks the pieces around it
make kind-down      # deletes the cluster
```

Needs `docker`, `kind`, `kubectl`, `helm` and `openssl`. `deploy/kind/deploy.sh` reads `NAMESPACE`
(default `txrisk`), `RELEASE`, `IMAGE_TAG` and `EXTRA_HELM_ARGS`.

## Credentials

Nothing credential-shaped is in the chart, its values or the images. The chart names five Secrets in
`settingsFrom` and never creates them:

| Secret | Holds | Read by |
| --- | --- | --- |
| `txrisk-database` | the Postgres owner's login, and the `txrisk_app` login | Postgres, the migration job, scheduled jobs |
| `txrisk-app` | the `txrisk_app` login alone | API, workers, outbox, scheduler |
| `txrisk-grafana` | Grafana's admin login | Grafana |
| `txrisk-workbench` | the key that encrypts the workbench's session cookie | the workbench |
| `txrisk-keycloak` | the demo users' sign-in values (development only) | Keycloak |

The entries are the keys `.env.example` and `keycloak.env.example` list. `deploy.sh` generates random values
into a private temporary file, creates the Secrets from it and removes it; a second run keeps the Secrets it
finds, so the Postgres volume's login keeps matching. To bring your own, create the Secrets before
`helm install` and the script leaves them alone. The API and workers connect as `txrisk_app` and read its
login from their own environment, so no database URL carries one.

## What the chart does

- **Probes and limits.** Every workload has readiness and liveness probes (a startup probe where the service
  is slow to start) and CPU and memory requests and limits, all in `values.yaml`. `tests/test_helm_chart.py`
  asserts it for every workload in the rendered chart.
- **Order.** `txrisk migrate` runs as a Job named for the release revision. The API, workers, outbox and
  scheduler wait in an init container until the role it creates can log in and no migration is pending.
- **Autoscaling.** The workers have a HorizontalPodAutoscaler on CPU (60%, one to three replicas; three is
  the partition count of `transaction.created`, so more would idle). It reads metrics-server, which
  `deploy.sh` installs because kind does not ship it (with `--kubelet-insecure-tls`: local clusters only).
  `--set worker.autoscaling.enabled=false --set worker.replicas=N` fixes the count instead.
- **Metrics.** Each of the API, workers, outbox and scheduler has a headless `<name>-metrics` Service, so
  Prometheus finds every replica by DNS, as `--scale worker=3` does in compose.
- **Scheduled jobs.** The scheduler runs `config/scheduler.toml`'s jobs as Kubernetes Jobs
  (`--launcher kubernetes`). The chart supplies the `[kubernetes]` table (`--kubernetes-config`): this
  release's namespace and image, the database Secret by name, the plain settings, the models volume.
- **Models.** Bundles live on the `models` volume (mounted read-only by the API and workers, read-write by
  jobs); it survives `helm uninstall`. Set `modelBundle=/models/<version>` once a bundle is on it; without
  one the rules alone score. The default `ReadWriteOnce` claim is shared by pods on one node, which is a kind
  cluster; several nodes need a `ReadWriteMany` storage class (`models.accessMode`).

## Reaching it

Everything is cluster-internal; use `kubectl port-forward`. The default addresses suit forwarding to
localhost, as in compose:

```bash
kubectl -n txrisk port-forward svc/keycloak 8080:8080 &
kubectl -n txrisk port-forward svc/workbench 3000:3000 &
kubectl -n txrisk port-forward svc/api 8000:8000 &
kubectl -n txrisk port-forward svc/grafana 3001:3000 &
```

Tokens name the realm as a browser reaches it (`signIn.issuer`), so Keycloak must be forwarded to
`localhost:8080` for people to sign in. Seed demo Cases with a one-off Job running `txrisk seed-cases` as the
owner, as `smoke.sh` does for its Wallets.

## Limits

- One replica of every stateful service; this is a development deployment, not a production one.
- The dev Keycloak keeps no state between restarts, and Alertmanager's silences are lost with its pod.
- Root filesystems are writable (`.trivyignore.yaml` says why); workloads run non-root with every
  capability dropped where the image allows it.
