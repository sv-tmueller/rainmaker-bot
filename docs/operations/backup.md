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

Row data only (`pg_dump --data-only`). Schema is in git (`store/db.py` base
tables and `store/migrate.py` migrations); it is not included in the dump.

## Restore procedure

1. Download the artifact from GitHub Actions (the run's "db-backup-<id>" artifact).
2. Decrypt: `gpg --decrypt rainmaker-<timestamp>.sql.gpg > dump.sql`
   Enter the `BACKUP_PASSPHRASE` when prompted.
3. Ensure the target database has the schema. Any `uv run rainmaker` command
   (e.g. `uv run rainmaker run --help`) will call `init_schema` and apply
   all migrations on connect. Run against an empty database first to initialize
   it, or apply `store/db.py` and `store/migrate.py` directly if you prefer.
4. Load the data into the database (it must already have the schema and be
   empty; the dump is data-only, so inserting into populated tables produces
   duplicate-key errors):
   `psql "$DATABASE_URL" < dump.sql`
5. Delete the plaintext dump: `rm dump.sql`

## Paid alternative

Supabase Pro ($25/month) includes daily automated backups and point-in-time
recovery (PITR). That path is not implemented here.
