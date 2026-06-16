# Database backup runbook

The backup workflow (`.github/workflows/backup.yml`) runs weekly (Sunday 02:00
UTC) and on manual dispatch. It dumps the Supabase Postgres row data, encrypts
the dump, and uploads it as a GitHub Actions artifact retained for 90 days.

## Prerequisites

Add two repo secrets in GitHub -> Settings -> Secrets and variables -> Actions:

- `DATABASE_URL` - the Supabase session-pooler connection string (already used
  by the scheduled run). Must use the session-pooler port (5432), not the
  transaction-pooler port (6543); `pg_dump` is incompatible with the
  transaction pooler.
- `BACKUP_PASSPHRASE` - a strong random passphrase for symmetric GPG encryption.
  Keep a copy somewhere safe (a password manager). If you lose it, the artifact
  is unrecoverable.

The workflow refuses to run if either value is missing or if `DATABASE_URL` is
not a Postgres DSN.

## What is backed up

Row data only (`pg_dump --data-only`). Schema is in git (`store/migrate.py` and
the base schema); it is not included in the dump.

## Restore procedure

1. Download the artifact from GitHub Actions (the run's "db-backup-<id>" artifact).
2. Decrypt: `gpg --decrypt rainmaker-<timestamp>.sql.gpg > dump.sql`
   Enter the `BACKUP_PASSPHRASE` when prompted.
3. Apply schema to the target database if it does not already have it:
   `uv run python -m rainmaker.store.migrate` (or replay migrations manually).
4. Load the data into a database that already has the schema and is empty (the
   dump is data-only; inserting into tables that already have rows will produce
   duplicate-key errors):
   `psql "$DATABASE_URL" < dump.sql`
5. Delete the plaintext dump: `rm dump.sql`

## Paid alternative

Supabase Pro ($25/month) includes daily automated backups and point-in-time
recovery (PITR). That path is not implemented here.
