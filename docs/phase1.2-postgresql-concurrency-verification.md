# Phase 1.2 — PostgreSQL Concurrency Verification Using GitHub Actions

- **Date:** 2026-11-17
- **Branch:** `fix/security-tests-and-tracking-api`
- **HEAD:** `7b6e508afd364683cf230e7ddde648c46c01e2c6` (identical to `origin/fix/security-tests-and-tracking-api`)
- **Scope:** execute `apps/menu/tests_phase11_pg.py` (scenarios A–E) against a real
  PostgreSQL server supplied by a GitHub Actions service container.

## 1. Status

**BLOCKED — WORKFLOW CREATED, NOT YET EXECUTED**

The workflow and the settings module exist and were validated as far as a
machine without PostgreSQL/Docker can validate them. No GitHub Actions run has
happened, so no scenario has been proven against PostgreSQL yet.

| Claim | State |
| --- | --- |
| Workflow file created and syntactically valid | **Yes** (parsed as YAML) |
| PostgreSQL settings module created, fails closed | **Yes** (verified locally) |
| Workflow executed on GitHub Actions | **No** |
| Tests skipped on SQLite by accident | **Prevented** (verified locally) |
| Scenarios A–E passed against PostgreSQL | **Not proven** |
| Three-run repeatability recorded | **Not executed** |
| Migration run against CI PostgreSQL | **Not executed** |

`Testing authorization is not cutover authorization.` Nothing in this phase
executed a cutover, changed a tracking mode, wrote to the live SQLite database,
touched inventory/orders/reservations/finance records, or modified the Phase 0
backup (`db.sqlite3.backup-20261114`).

## 2. Why GitHub Actions

1. The owner's laptop has no Docker Desktop and no PostgreSQL server, and this
   phase must not install either.
2. GitHub-hosted runners provide a disposable PostgreSQL service container per
   job, with no local installation and no repository secrets required.
3. The workflow is reproducible, reviewable, and can be re-run on demand.
4. PostgreSQL is only reachable from inside the job (`localhost` port map on
   the ephemeral runner); it is destroyed with the runner.
5. The same job can run on `pull_request`, so future changes are checked
   automatically without a local server.

## 3. Inspected baseline

| Item | Finding |
| --- | --- |
| `.github/workflows/` | One existing workflow: `ci.yml` (SQLite job + PostgreSQL job) |
| Existing CI conventions | `actions/checkout@v4`, `actions/setup-python@v5`, Python `3.12`, pip cache, `postgres:16` service, `DJANGO_SETTINGS_MODULE` via env |
| Python | 3.12.10 locally; 3.12 in CI (supported by Django 4.2.16) |
| Django | 4.2.16 (`requirements.txt`) |
| PostgreSQL driver | `psycopg[binary]==3.2.3` (Django 4.2 supports psycopg 3) |
| `kape_de_manubag/test_settings.py` | Inherits `settings.py`, overrides only the staticfiles backend |
| Database selection | `settings.py`: `DATABASE_URL` set → PostgreSQL; unset → SQLite `db.sqlite3` |
| `apps/menu/tests_phase11_pg.py` | 5 `TransactionTestCase` classes using threads + `SELECT FOR UPDATE`; skipped unless `connection.vendor == 'postgresql'` |
| Skip convention | Same `_require_postgresql` decorator as `apps/inventory/tests_phase2a_pg.py`, `apps/inventory/tests_phase4b6_pg.py`, `apps/orders/tests_phase5b7_pg.py` |

Existing `ci.yml` was **not** modified, and no existing workflow was
overwritten. The new verification lives in its own workflow file.

## 4. PostgreSQL settings module

**File:** `kape_de_manubag/test_postgres_settings.py`

- Imports `kape_de_manubag.test_settings` (so staticfiles, apps, middleware,
  auth, templates and business settings stay identical).
- Overrides **only** `DATABASES['default']`.
- `ENGINE` is always `django.db.backends.postgresql`.
- Connection details come from environment variables; no credentials are
  hardcoded.
- **Fails closed:** if any required variable is missing or empty, importing the
  module raises `ImproperlyConfigured`. There is no SQLite fallback.
- Rejects a database name containing `sqlite` or ending in `.sqlite3`, and
  re-checks the resolved `ENGINE`/`NAME` at import time.
- `CONN_MAX_AGE = 0` because the concurrency tests open their own connections
  from worker threads.
- Django's test database defaults to `test_<POSTGRES_DB>`; `POSTGRES_TEST_DB`
  can override it.

| Variable | Required | Purpose |
| --- | --- | --- |
| `POSTGRES_DB` | Yes | dedicated CI database (`kdm_phase12_test`) |
| `POSTGRES_USER` | Yes | CI-only role (`kdm_phase12`) |
| `POSTGRES_PASSWORD` | Yes | CI-only disposable password |
| `POSTGRES_HOST` | Yes | `localhost` on the runner |
| `POSTGRES_PORT` | Yes | `5432` |
| `POSTGRES_TEST_DB` | No | test database name, default `test_kdm_phase12_test` |
| `KDM_REQUIRE_POSTGRESQL` | No | `1` turns a non-PostgreSQL skip into a failure |

Verified locally:

```
POSTGRES_DB=kdm_phase12_test POSTGRES_USER=kdm_phase12 \
POSTGRES_PASSWORD=kdm_phase12_ci_only POSTGRES_HOST=localhost POSTGRES_PORT=5432 \
python -c "import django; django.setup(); ..."

ENGINE    : django.db.backends.postgresql
NAME      : kdm_phase12_test
HOST      : localhost
PORT      : 5432
TEST NAME : test_kdm_phase12_test
STORAGE   : django.contrib.staticfiles.storage.StaticFilesStorage
```

Without the variables:

```
ImproperlyConfigured: kape_de_manubag.test_postgres_settings requires a
PostgreSQL environment; missing or empty variable(s): POSTGRES_DB,
POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST, POSTGRES_PORT. This module
never falls back to SQLite.
```

## 5. Workflow

**File:** `.github/workflows/postgresql-concurrency.yml`

| Setting | Value |
| --- | --- |
| Name | `PostgreSQL Concurrency Verification (Phase 1.2)` |
| Runner | `ubuntu-latest` |
| Permissions | `contents: read` only |
| Triggers | `workflow_dispatch`, `pull_request` (all branches) |
| Deployment | None |
| Service | `postgres:16`, job-scoped, `5432:5432` |
| Credentials | fixed disposable CI values `kdm_phase12` / `kdm_phase12_ci_only` (no repository secrets) |
| Health check | `pg_isready -U kdm_phase12 -d kdm_phase12_test`, 5 s interval, 20 retries |
| Python | 3.12 with pip cache |
| Driver | `psycopg[binary]==3.2.3` from `requirements.txt` |

Job `postgresql-concurrency` steps:

1. **Verify PostgreSQL isolation** (before any test) — prints `ENGINE`, `NAME`,
   `HOST`, `PORT`, test database name, Django `connection.vendor`, the
   server's `server_version`, `current_database()` and `current_user`, then
   asserts the job aborts unless all of these hold:
   - `ENGINE == 'django.db.backends.postgresql'`;
   - `connection.vendor == 'postgresql'`;
   - database name is not a SQLite file and equals `POSTGRES_DB`;
   - the server reports the same database;
   - `HOST`, `PORT`, `USER` match the environment.
2. **Run migrations** against the dedicated CI database:
   `python manage.py migrate --noinput` with
   `DJANGO_SETTINGS_MODULE=kape_de_manubag.test_postgres_settings`.
3. **Run the concurrency suite three consecutive times**, each teeing to
   `pg-run-N.log`.
4. **Assert every run** collected exactly 5 tests, reported 5 `... ok`
   results, ended with an unskipped `OK`, and contains no `skipped` text.
5. **Upload logs** as `phase12-postgresql-run-logs` (always, 30-day
   retention).

The suite command (identical in all three runs):

```
python manage.py test apps.menu.tests_phase11_pg \
  --settings=kape_de_manubag.test_postgres_settings \
  --verbosity=2 --noinput
```

`KDM_REQUIRE_POSTGRESQL=1` is set for the job. `apps/menu/tests_phase11_pg.py`
now raises `AssertionError` instead of calling `skipTest()` when the backend
is not PostgreSQL and that variable is `1`; on SQLite without the variable the
original skip behaviour is unchanged.

### How to trigger manually

- Actions → **PostgreSQL Concurrency Verification (Phase 1.2)** →
  **Run workflow** → choose the branch → **Run workflow**
  (`workflow_dispatch`). Note: GitHub only lists this button once the workflow
  file exists on the repository's default branch. Opening a pull request
  triggers it immediately without that step.
- CLI: `gh workflow run "PostgreSQL Concurrency Verification (Phase 1.2)" --ref <branch>`

### How to view results

- Actions → the workflow run → job **Phase 1.1 concurrency tests (PostgreSQL 16)**.
- Per-run test output in the three "Run PostgreSQL concurrency suite" steps.
- Artifact `phase12-postgresql-run-logs` contains `pg-run-1.log`…`pg-run-3.log`.

## 6. Database isolation verification

Enforced in two independent places:

1. **Import-time** in `test_postgres_settings.py` — missing variables, a
   SQLite-looking database name, or a non-PostgreSQL backend cannot even load
   the settings.
2. **Pre-test step** in the workflow — live connection + printed
   `ENGINE`/`NAME`/`HOST`/server version, with hard failure on any mismatch.

The test module itself additionally fails (rather than skips) when
`KDM_REQUIRE_POSTGRESQL=1` and the backend is not PostgreSQL. There is no path
by which this job can run the suite against `db.sqlite3`.

## 7. Migrations

- The workflow applies the project's existing migration history to the
  dedicated CI PostgreSQL database (`kdm_phase12_test`) with
  `--noinput`. Django's test runner then creates `test_kdm_phase12_test` and
  applies the same history there.
- No migration was edited, squashed, or added to work around drift.
- **Status: not executed against PostgreSQL** (no workflow run yet).
- Locally, the suite's test-database setup applied all migrations to a fresh
  in-memory SQLite database without error (this only proves SQLite applies the
  history; it says nothing about PostgreSQL).
- Pre-existing metadata drift in `accounts/0006` and `orders/0026` noted in
  Phase 1.1 is untouched. If PostgreSQL migrations fail, the workflow stops
  at that step and the exact error must be reported — not worked around.

## 8. Scenarios A–E

| # | Test | Proves | PostgreSQL status |
| --- | --- | --- | --- |
| A | `ConcurrentCutoverSameProductTests.test_exactly_one_wins_one_audit_row` | Two concurrent cutovers serialise; exactly one audit row; loser revalidates and is rejected | Not executed |
| B | `CutoverRacingOrderPlacementTests.test_order_mode_snapshot_is_consistent` | Placement racing cutover cannot produce a torn `inventory_mode` snapshot; no deadlock | Not executed |
| C | `ReservationBlocksCutoverTests.test_open_reservation_rejects_cutover_atomically` | Committed outstanding reservation blocks cutover (gate 10); mode and audit unchanged | Not executed |
| D | `OverlappingInventoryCutoverTests.test_overlapping_cutovers_serialise_without_deadlock` | Overlapping multi-product batches sharing inventory items serialise (Product then Item ascending); one audit row per product | Not executed |
| E | `MultiProductAtomicFailureTests.test_failure_rolls_back_entire_batch` | Mid-batch configuration failure and mid-batch audit failure roll back every product; zero audit rows | Not executed |

Local collection check (SQLite, `kape_de_manubag.test_settings`):

```
Ran 5 tests in 0.535s
OK (skipped=5)
```

Local guard check (`KDM_REQUIRE_POSTGRESQL=1`, SQLite):

```
Ran 5 tests in 0.497s
FAILED (failures=5)
AssertionError: PostgreSQL required ... Current backend: sqlite.
KDM_REQUIRE_POSTGRESQL=1 forbids skipping; the test runner is not using PostgreSQL.
```

## 9. Three-run repeatability

**NOT EXECUTED.** No GitHub Actions run has taken place, so there are no
results to record. The workflow is built to produce them:

| Run | Tests collected | Passed | Skipped | Deadlocks | Result |
| --- | --- | --- | --- | --- | --- |
| 1 of 3 | — | — | — | — | Not executed |
| 2 of 3 | — | — | — | — | Not executed |
| 3 of 3 | — | — | — | — | Not executed |

The job fails unless every run reports `Ran 5 tests`, five `... ok`, `OK`
without parentheses, and no `skipped`.

## 10. Reservation concurrency review

Reviewed: `create_order_item`, `place_order_inventory`, `settle`,
`_settle_reservation`, `on_status_changed`, `legacy_restore_by_disposition`,
`change_inventory_tracking_mode`, `_validate_rollback_products`, and the
readiness gates in `assess_cutover_readiness`.

### Lock hierarchy actually implemented

| Level | Resource | Where |
| --- | --- | --- |
| 1 | `Order` row | Checkout/POS atomic block; `place_order_inventory` Step 1; `on_status_changed` |
| 2 | `Product` rows, ascending pk | `place_order_inventory` Step 3; cutover; cancellation (legacy products only) |
| 3 | `OrderIngredientReservation` header | `place_order_inventory` Step 6; `_settle_reservation` Step 6 |
| 3b | Reservation items, ascending pk | `_settle_reservation` Step 9 |
| 4 | `InventoryItem` rows, ascending pk | `lock_inventory_items()` in placement Step 9, settlement Step 12, cutover |

Every writer uses descending-free ascending pk ordering on Products and
InventoryItems, so no lock-order inversion exists between placement, cutover
and cancellation.

### Transaction boundaries

- `place_order_inventory` and `on_status_changed` intentionally open no
  outermost transaction; both call sites (`apps/orders/views.py` checkout and
  POS creation) wrap item creation + coordinator call in one
  `transaction.atomic()` block.
- `change_inventory_tracking_mode` owns its `transaction.atomic()`: Product
  locks → first readiness pass → InventoryItem locks → **second** readiness
  pass → mode writes + strict audit rows. Any failure rolls back all products
  and all audit rows.
- Reservation rows are created only after Products and InventoryItems are
  locked, and reserved quantities use a conditional `UPDATE ... WHERE
  on_hand_quantity >= reserved_quantity + required` backstop.

### "Reserved legacy line after readiness is assessed"

This was the specific race to disprove. It is closed:

1. `create_order_item` snapshots `inventory_mode` before any lock, so a line
   can briefly be stamped `legacy` while a cutover is in flight.
2. `place_order_inventory` Step 4 re-snapshots every line from the locked
   Product row. A cutover and a placement cannot both hold the same Product
   row, so the placement either sees `legacy` (cutover not committed) or
   `ingredient` (cutover committed) — never a stale mix.
3. If placement commits first with legacy lines, the cutover's locked
   revalidation sees an active order line (gate 9) and refuses. If the cutover
   commits first, the placement re-snapshots to `ingredient`, calls
   `resolve_plan()` under the lock, and creates the reservation.
4. A reservation can only be created for `ingredient` lines, so a `legacy`
   line can never receive a reservation.
5. Cancellation releases/consumes through `settle()` with the reservation
   header locked, and cutover's gate 10 only trusts committed reservation
   state, which is authoritative under the Product lock.

**No race condition was found; no product code was changed and no new
regression test was added.** The area is already covered by scenario B
(placement versus cutover) and scenario C (committed reservation versus
cutover).

Residual observations (not defects):

- The readiness assessment queries are plain reads; the authoritative decision
  is the second pass under the Product/InventoryItem locks.
- Cancelling an ingredient-only order does not lock its Product (nothing
  legacy to restore). That is safe because cutover's gate 10 reads committed
  reservation state and both commit orders produce a consistent outcome.
- There is no post-placement "add item to an existing order" flow in the
  views; placement is always called in the same transaction that creates the
  lines. If such a flow is added later, it must call `place_order_inventory`
  (or re-snapshot under the Product lock) itself.

## 11. Discovered defects and fixes

| Item | Result |
| --- | --- |
| Product-code defect | None found in the reservation/cutover review |
| Silent-skip risk in CI | **Fixed** — `KDM_REQUIRE_POSTGRESQL=1` makes a non-PostgreSQL backend a failure instead of a skip |
| Silent SQLite fallback | **Fixed** — `test_postgres_settings.py` fails at import without PostgreSQL variables and rejects SQLite names |
| Local Docker/PostgreSQL requirement | **Removed** — the database is a GitHub Actions service container |

## 12. SQLite regression job

A second, separate job `sqlite-regression` runs in the same workflow with
`DJANGO_SETTINGS_MODULE=kape_de_manubag.test_settings` and **no** `POSTGRES_*`
variables. It:

1. asserts `ENGINE == 'django.db.backends.sqlite3'`, `connection.vendor ==
   'sqlite'`, and that no `POSTGRES_*` variable is set;
2. runs `manage.py check` and `manage.py migrate`;
3. runs the Phase 1 / 1.1, menu, inventory and order suites:

```
python manage.py test apps.menu apps.inventory apps.orders \
  --settings=kape_de_manubag.test_settings --verbosity=1 --noinput
```

`apps.menu` includes `tests_phase1_cutover` and `tests_phase11_pg`; the
PostgreSQL-only modules in those apps skip on SQLite by design. The existing
`ci.yml` is unchanged, so its own SQLite and PostgreSQL jobs remain.

**Status: not executed on GitHub Actions.**

## 13. Local verification actually performed

| Check | Command (abridged) | Result |
| --- | --- | --- |
| Settings fail closed | import module with no `POSTGRES_*` | `ImproperlyConfigured`, lists all 5 missing variables |
| Settings resolve correctly | import with all 5 variables | ENGINE/HOST/PORT/NAME/TEST correct; storage override preserved |
| Django check | `manage.py check --settings=kape_de_manubag.test_postgres_settings` with env | `System check identified no issues (0 silenced).` |
| Collection + skip behaviour | `manage.py test apps.menu.tests_phase11_pg --settings=kape_de_manubag.test_settings -v2` | `Ran 5 tests`, `OK (skipped=5)` |
| CI guard | same with `KDM_REQUIRE_POSTGRESQL=1` | `Ran 5 tests`, `FAILED (failures=5)` with the explicit assertion message |
| Workflow YAML | parse with PyYAML (installed and removed afterwards) | valid; triggers `workflow_dispatch`, `pull_request`; `contents: read`; two jobs |
| Passing-test format | one `TestCase` at `-v2` | lines end with `... ok`, confirming the log assertion |

No PostgreSQL server was installed, started, or required. No Docker was used.
`manage.py test` used Django's in-memory SQLite test database; the live
`db.sqlite3` was not modified.

## 14. Git preservation

Initial state: 116 porcelain entries — 13 modified, 103 untracked. Nothing
staged. HEAD `7b6e508`, branch `fix/security-tests-and-tracking-api`
(0 ahead / 0 behind `origin`).

Files added or modified by this phase:

| File | Change | Git state |
| --- | --- | --- |
| `kape_de_manubag/test_postgres_settings.py` | new | `??` |
| `.github/workflows/postgresql-concurrency.yml` | new | `??` |
| `docs/phase1.2-postgresql-concurrency-verification.md` | new | `??` |
| `apps/menu/tests_phase11_pg.py` | modified (`KDM_REQUIRE_POSTGRESQL` guard, docstring) | already `??` before this phase, so porcelain shows no new entry |

Final porcelain count: 119 entries (116 + 3 new files). No pre-existing entry
was removed or changed; no file was staged, committed, stashed, reset,
checked out, cleaned, pushed, or deleted. Existing `ci.yml` was not touched.

## 15. What must reach GitHub before the workflow can pass

**Critical:** the current HEAD does **not** contain the cutover service. On
`HEAD`, `apps/menu/services.py` has no `change_inventory_tracking_mode` /
`CUTOVER_AUDIT_ACTION`, `apps/audit/services.py` has no `strict=` argument, and
`apps/menu/tests_phase11_pg.py` is untracked. A workflow run from HEAD alone
would fail at import/collect time, not because of concurrency.

The owner must commit at least:

- `.github/workflows/postgresql-concurrency.yml`
- `kape_de_manubag/test_postgres_settings.py`
- `apps/menu/tests_phase11_pg.py`
- `docs/phase1.2-postgresql-concurrency-verification.md`
- `apps/menu/services.py` (Phase 1 cutover service — modified)
- `apps/audit/services.py` (`strict=True` audit support — modified)
- `apps/inventory/services.py` (Phase 1 implementation surface — modified; review with `git diff`)

Review the remaining modified files with `git diff` and include only the ones
that belong to this change set; unrelated owner work must be left alone.

## 16. Exact next steps for the owner

These commands were **not** executed and require separate authorization:

```
# 1. review exactly what would be committed
git status --porcelain
git diff -- apps/menu/services.py apps/audit/services.py apps/inventory/services.py

# 2. stage only the Phase 1.2 files listed in §15
git add .github/workflows/postgresql-concurrency.yml \
        kape_de_manubag/test_postgres_settings.py \
        apps/menu/tests_phase11_pg.py \
        docs/phase1.2-postgresql-concurrency-verification.md \
        apps/menu/services.py apps/audit/services.py apps/inventory/services.py

# 3. commit and push the branch (or a dedicated branch)
git commit -m "ci(menu): add PostgreSQL concurrency verification workflow"
git push origin fix/security-tests-and-tracking-api

# 4. open a pull request (pull_request trigger runs automatically)
gh pr create --fill

# 5. optional manual run (button appears once the workflow is on the default branch)
gh workflow run "PostgreSQL Concurrency Verification (Phase 1.2)" --ref fix/security-tests-and-tracking-api
```

GitHub Actions can only execute committed code. Until steps 3–4 are done, the
workflow is **prepared, not executed**, and the status remains
`BLOCKED — WORKFLOW CREATED, NOT YET EXECUTED`.

## 17. Remaining blockers

1. Phase 1.2 files and the Phase 1 cutover service are uncommitted, so GitHub
   Actions cannot run them yet.
2. No PostgreSQL run has been recorded; scenarios A–E are verified by source
   review and SQLite collection only.
3. The three-run repeatability record and migration output must be read from
   the first successful workflow run and appended here.
4. `workflow_dispatch` appears in the UI only after the workflow reaches the
   default branch; the `pull_request` trigger works before that.
5. Pre-existing data/drift considerations from Phase 1.1 (sample recipes,
   `client_confirmed: false`, migration metadata drift, demo stock consumption)
   remain open and are unaffected by this phase.

**Phase 1.2 stops here. The ingredient inventory cutover was not executed and
Phase 2 was not started.**
