# Running the 3h job on a host (systemd)

How to run the 3h job (`run`, `settle`, `prune`) on a Linux host under systemd
instead of the GitHub Actions cron. The store stays Supabase Postgres, so the
dashboard and every other workflow are unaffected. GitHub Actions keeps CI,
daily diagnostics, backups and keepalive.

Everything below is an operator step. The files live in `deploy/`; nothing in
this repo installs or enables them for you.

## What gets installed

| Path | Purpose |
|---|---|
| `/opt/rainmaker` | the git checkout and its `.venv`, owned by the `rainmaker` user, read-only at run time |
| `/var/lib/rainmaker` | home and `StateDirectory=`: reports, the overlap lock `run.lock`, the uv cache |
| `/etc/rainmaker/rainmaker.env` | secrets and settings, root-owned, mode 600 |
| `/etc/systemd/system/rainmaker-run.{service,timer}` | the job and its schedule |
| `/etc/systemd/system/rainmaker-run-failure.service` | the `OnFailure=` alert |

What the job does each time the timer fires (`deploy/rainmaker-run.sh`):

1. Refuses to run unless `DATABASE_URL` is a Postgres DSN (the same guard as
   `daily-run.yml`; otherwise the CLI would write to a local SQLite file).
2. Takes a non-blocking lock on `/var/lib/rainmaker/run.lock`. If another run
   holds it, the script exits 75, which the unit counts as success, so a skip
   raises no alert. This replaces the Actions concurrency group, with one
   difference: the new run is skipped and the in-flight run is left alone.
3. Pings the optional dead-man URL (`/start`).
4. Deletes reports older than 7 days, then runs `rainmaker run --reports-dir
   /var/lib/rainmaker/reports`, `rainmaker settle` and `rainmaker prune` from
   the venv's console script. It never calls `uv` at run time.
5. Pings the dead-man URL again on success.

On any failure the unit's `OnFailure=` starts `rainmaker-run-failure.service`,
which pings `<url>/fail` and files or updates one GitHub issue titled
`ops: host-run workflow failing` through `scripts/notify_failure.sh`. The repo
is public, so that issue holds only a pointer (unit result, exit status and a
`journalctl` hint), never journal text or a host name.

The timer fires at `00/3:00:00 UTC` (the same slots as the Actions cron) plus a
random delay of up to 10 minutes, and `Persistent=true` makes it run once after
downtime. `daily-diagnostics` runs at 21:45 UTC and expects the 21:00 settle to
be finished: 21:00 plus 10 minutes of delay plus the 20 minute timeout ends by
21:30, so that still holds.

## Prerequisites

- Linux with systemd. The units are checked on systemd 255 (Ubuntu 24.04).
- Python 3.11 or newer installed system-wide (the venv is built with
  `UV_PYTHON_DOWNLOADS=never`, so uv uses the system Python).
- `git`, `curl`, `flock` (util-linux) and the `gh` CLI. `gh` is used only by
  the failure alert.
- `uv` on a path the service user can read, such as `/usr/local/bin/uv`. Copy
  the binary there if it is installed under a private home directory.
- Time sync running. Check with `timedatectl show -p NTPSynchronized --value`
  (expect `yes`). The timer pins UTC, so the host's time zone does not matter.
- Network: outbound DNS and HTTPS (443), plus the Postgres port in the Supabase
  pooler DSN. Nothing needs an inbound port. If the host filters outbound
  traffic, allow exactly that.
- A Supabase session-pooler DSN (the same value as the `DATABASE_URL` repo
  secret).
- Optional: a healthchecks.io-style check, with period 3 hours and a grace time
  of about 45 minutes (10 minutes of random delay plus the 20 minute timeout,
  with margin). Skipped runs (exit 75) send no ping, which is fine.
- A fine-grained GitHub token for the alert: resource owner `sv-tmueller`,
  repository `sv-tmueller/rainmaker-bot` only, permission Issues read and
  write, and an expiry you will remember to rotate.

## One-time setup

Run as root.

```bash
# Service user: no login shell, home is the state directory.
useradd --system --create-home --home-dir /var/lib/rainmaker \
  --shell /usr/sbin/nologin --user-group rainmaker

# Checkout. The repo is public, so https works without credentials.
install -d -o rainmaker -g rainmaker /opt/rainmaker
sudo -u rainmaker git clone https://github.com/sv-tmueller/rainmaker-bot.git /opt/rainmaker

# Env file: root-owned, mode 600. Fill in the values.
install -d -m 755 /etc/rainmaker
install -m 600 -o root -g root /opt/rainmaker/deploy/env.example /etc/rainmaker/rainmaker.env
${EDITOR:-nano} /etc/rainmaker/rainmaker.env
```

`deploy/env.example` documents every variable. systemd reads the file as plain
`KEY=value` lines with no shell expansion, so keep the DSN in single quotes.
Because the file is root-only, the service user cannot read it directly;
systemd loads it before dropping privileges.

Then deploy a tag (next section), which also installs the units.

## Deploy by tag

The first deploy and every update use the same procedure. Deploys are by tag so
the host always runs a commit that passed CI.

1. On a workstation, tag a commit on `main` whose CI is green, and push the tag:

   ```bash
   git tag -a deploy-2026-11-03 -m "deploy: <what changed>" <sha>
   git push origin deploy-2026-11-03
   ```

   Use `deploy-YYYY-MM-DD`, adding `.1`, `.2` for a second tag on the same day.

2. On the host, update the checkout and venv as the service user, holding the
   run lock so a deploy never swaps the venv under a running job. `flock -w
   1800` waits up to 30 minutes for a run in flight:

   ```bash
   TAG=deploy-2026-11-03
   sudo -u rainmaker flock -w 1800 /var/lib/rainmaker/run.lock bash -c '
     set -euo pipefail
     cd /opt/rainmaker
     git fetch --tags origin
     git checkout --detach "$0"
     UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --compile-bytecode
   ' "$TAG"
   ```

3. Install the units and check the installed copies. The check must print
   nothing:

   ```bash
   cd /opt/rainmaker/deploy
   install -m 644 rainmaker-run.service rainmaker-run-failure.service rainmaker-run.timer /etc/systemd/system/
   systemctl daemon-reload
   systemd-analyze verify --man=no /etc/systemd/system/rainmaker-run.service \
     /etc/systemd/system/rainmaker-run-failure.service /etc/systemd/system/rainmaker-run.timer
   ```

4. Confirm the version: `sudo -u rainmaker git -C /opt/rainmaker describe --tags --always`.
   Each run also prints it as `rainmaker version: ...` in the journal.

Units are copied, not symlinked, so a deploy that changes them needs step 3
again.

## Smoke test before cutover

Do this with the timer disabled and the Actions cron still on.

1. Pick a time with no Actions run in progress and none about to start (the
   Actions slots are every 3 hours on the hour). Check
   `gh run list --workflow scheduled-run --status in_progress`.
2. Start one run and watch it:

   ```bash
   systemctl start rainmaker-run.service    # blocks until the run ends
   journalctl -u rainmaker-run.service --no-pager | tail -40
   ```

   Expect exit 0, a `rainmaker version:` line, the usual report output, and a
   new run in the dashboard. If you set a healthcheck URL, expect the check to
   show a start and a success ping.
3. Test the failure path end to end. Blank the DSN in the env file, start the
   job, restore the DSN, then look for the alert:

   ```bash
   # edit /etc/rainmaker/rainmaker.env: DATABASE_URL='' (remember the old value)
   systemctl start rainmaker-run.service || true
   # edit it back to the real DSN
   journalctl -u rainmaker-run-failure.service --no-pager | tail
   ```

   The job exits 1 with "refusing to run", the failure unit starts, and the
   issue `ops: host-run workflow failing` is created. Close that issue
   afterwards. If the token is unset, the failure unit logs that and exits 1.
4. Overlap lock: start the job twice in quick succession and check the second
   journal shows `skipping this run` and a clean exit.

## Cutover

Do these on the same day, in this order. Never let the job run on two hosts at
once. The lock is per host, so it cannot stop an Actions run and a host run
from overlapping and writing duplicate rows.

1. Open an operator PR that removes the `schedule:` block from
   `.github/workflows/daily-run.yml` and keeps `workflow_dispatch`. Do not
   disable the workflow in the Actions tab: that also disables manual runs.
   Merge it.
2. Check no Actions run is in progress:
   `gh run list --workflow scheduled-run --status in_progress` prints nothing.
3. Enable the timer:

   ```bash
   systemctl enable --now rainmaker-run.timer
   systemctl list-timers rainmaker-run.timer
   ```

4. After the first fire, check `journalctl -u rainmaker-run.service` and the
   healthcheck.

A manual `workflow_dispatch` of `scheduled-run` after cutover is a second host.
Before one, run `systemctl stop rainmaker-run.timer` and wait until
`flock -w 1800 /var/lib/rainmaker/run.lock true` returns, then start the timer
again afterwards.

## The trade step (batch 3, not built yet)

Batch 3 adds paper trading to this job. It is not part of this kit and nothing
reads `RAINMAKER_TRADE_MODE` today.

- Planned shape: a `rainmaker trade` step between `run` and `settle` in
  `deploy/rainmaker-run.sh`, gated by `RAINMAKER_TRADE_MODE` in the env file.
  Unset or `off` means no trade step. `paper` enables paper trading.
- Roll it out as one clean day with the mode `off` on the new code, then switch
  to `paper`.
- Kill switch: set `RAINMAKER_TRADE_MODE=off` in the env file. The next run
  picks it up, no restart needed. Hard stop: `systemctl disable --now
  rainmaker-run.timer`.
- Never wire the trade step into `daily-run.yml`. Trading runs only on the host.

## Calibration refits

A prod calibration refit (the `backfill` workflow against Supabase) changes the
forecast distribution the paper bank trades on, so it is a paper era change:
add a `PAPER_ERAS` entry for it (see #492). This is step 3 of the regime refit
rollout in `docs/operations/README.md`.

## Rotating secrets

- DSN or healthcheck URL: edit `/etc/rainmaker/rainmaker.env`. Each run reads
  the file fresh, so no restart is needed.
- Alert token: create the new fine-grained token, edit the env file, then
  revoke the old one. Put the expiry date in a calendar.
- Keep the file root-owned and mode 600 (`ls -l /etc/rainmaker/rainmaker.env`).

## Rollback

Roll back the code: deploy an earlier tag with the same procedure (step 2 of
"Deploy by tag"). The lock makes it wait for a run in flight.

Roll back to the Actions cron:

1. `systemctl disable --now rainmaker-run.timer`, then wait for any run in
   flight: `flock -w 1800 /var/lib/rainmaker/run.lock true`.
2. Revert the cutover PR so `daily-run.yml` has its `schedule:` block again.
3. Pause the healthcheck so it does not page for the missing pings.

Never do step 2 before step 1 finishes.

## Troubleshooting

| Symptom | Likely cause and check |
|---|---|
| Journal says `refusing to run`, exit 1 | `DATABASE_URL` is empty or not `postgres://` or `postgresql://`. Fix the env file. |
| Unit shows `status=217/USER` | The `rainmaker` user does not exist. Repeat the `useradd` step. |
| Unit shows `status=203/EXEC` | `/usr/bin/bash` or `/opt/rainmaker/deploy/rainmaker-run.sh` is missing. Check the checkout. |
| `Permission denied` writing under `/opt` | The venv and checkout are read-only at run time by design. Reports go to `/var/lib/rainmaker/reports`. |
| `rainmaker: No such file` | The venv is missing. Re-run step 2 of "Deploy by tag". |
| Run exits 0 with `skipping this run` | Another run, or a deploy, held the lock. Expected. |
| Timer never fires | `systemctl list-timers rainmaker-run.timer`. Not listed means it is not enabled. |
| Failed run, no GitHub issue | `RAINMAKER_ALERT_GH_TOKEN` unset, expired, or missing Issues write. See `journalctl -u rainmaker-run-failure.service`. |
| Healthcheck pages but the job is fine | Grace time too short, or `RAINMAKER_HEALTHCHECK_URL` points at the wrong check. |

Find the log for one failed run with the hint in the alert issue:
`journalctl -u rainmaker-run.service _SYSTEMD_INVOCATION_ID=<id>`.

## Checking the units locally

Without installing anything, from a repo checkout:

```bash
systemd-analyze verify --man=no deploy/*.service deploy/*.timer
```

The command must print nothing. `systemd-analyze verify` exits 0 even for
unknown keys and bad values, so empty output is the real pass condition.
`tests/test_deploy_units.py` runs the same check in CI and skips only when
`systemd-analyze` is not installed.
