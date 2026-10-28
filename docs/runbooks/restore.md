# Runbook: back up and restore Postgres

Postgres holds the parties, the history the features read, Risk assessments and Alerts. A backup is one
custom-format `pg_dump` file, `txrisk-<UTC timestamp>.dump`, so the newest sorts last. CI restores a fresh
backup on every change (the `restore` job, and `tests/test_backup.py`), so this procedure is exercised, not
just written down.

## Taking backups

`txrisk backup` dumps the database in `TXRISK_DATABASE_URL` into a directory and then deletes all but the
newest `keep` backups. Both settings live in `config/backup.toml`; `--out` and `--keep` override them. The
password is `PGPASSWORD` from `.env`; it is never in the URL or on a command line.

```bash
make backup                           # = docker compose run --rm backup, into the `backups` volume
docker compose run --rm backup ls -l /backups
```

The job is a one-off, so whatever drives schedules calls it. `config/scheduler.toml` has no backup job, so
nothing in the stack schedules it; use host cron (or a Kubernetes CronJob, which the chart does not provide):

```cron
# daily at 02:10, from the repository checkout
10 2 * * *  cd /srv/txrisk && docker compose run --rm backup >> /var/log/txrisk-backup.log 2>&1
```

A failed dump is written aside and never renamed, so it cannot become the "latest" backup or push a good one
out of the retention count. The file name carries the wall-clock time the dump ran, which is operational
metadata only; no business logic reads it.

Copy the `backups` volume off the host (object storage, another machine) too: a backup on the same disk as the
database does not survive losing that disk.

## Restoring

Restore always targets an **empty** database; `txrisk restore` refuses one that already has tables.

1. Stop whatever writes to the database (`docker compose stop api`).
2. Create the empty target. To replace the live database, rename or drop it first, deliberately:

   ```bash
   docker compose exec postgres createdb --username txrisk restored
   ```

3. Restore the newest backup in the folder (or pass one file), and compare it with the source if the source
   is still readable:

   ```bash
   docker compose run --rm backup txrisk restore /backups \
     --database-url postgresql://txrisk@postgres:5432/restored \
     --compare-with postgresql://txrisk@postgres:5432/txrisk
   ```

   `--compare-with` prints the latest migration and row counts per table, and exits non-zero if either
   differs from the source. Without a source, check by hand:

   ```bash
   docker compose exec postgres psql --username txrisk restored \
     --command "SELECT max(version) FROM schema_migrations" \
     --command "SELECT count(*) FROM transactions"
   ```

4. Point the system at it (`TXRISK_DATABASE_URL`, or swap the database names) and run `txrisk migrate`: it
   applies nothing if the backup was current, and brings an older backup up to the current schema.
5. Start the API again and check `GET /v1/ready`.

Outside Docker the same commands work with `txrisk restore <file-or-folder> --database-url ...`, given
`PGPASSWORD` and `pg_dump`/`pg_restore` on the path.

**Client and server major versions must match.** Postgres 17's tools write settings Postgres 16 rejects
(`SET transaction_timeout`), and older tools refuse newer servers. The compose `postgres` service, the
Testcontainers image in `tests/conftest.py` and the `postgresql-client-17` package in the `Dockerfile` are
all 17; move them together when upgrading.

## What a restore does not bring back

Only Postgres. Model bundles (`models/`) and the generated dataset are not in the backup. Data written after
the backup was taken is lost, so the retention count and schedule set how much that is.
