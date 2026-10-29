# Older admission lists: local swarm preparation

Deployment authorized on 29 October 2026. The swarm adds its own run/queue tables and workflow;
existing dashboards, credentials, candidate data, and portal scraping code are preserved.

## Prepared files

- `.github/workflows/run_bot_legacy_swarm.yml`: separate manual workflow and concurrency group.
- `run_legacy_swarm.py`: setup, worker, coordinator, and offline validation commands.
- `supabase/migrations/20261028030000_portal_legacy_swarm.sql`: isolated run/queue tables and RPCs.
- `tests/test_legacy_swarm.py`: offline runner behavior tests.
- `tests/legacy_swarm.sql`: rollback-only database regression script, prepared for later authorized validation.

## Behavior

The source remains the existing 46 checked-in CSVs, with 60,630 ready `(list_name,JAMB number)`
observations. Default session is `all`; individual academic sessions remain selectable.
At setup and every continuation, `skip_saved_portal_swarm` reconciles all pending entries against
the existing source receipts in one database operation. Matching entries become saved immediately;
active leases, unsaved entries, and existing candidate records are not changed by reconciliation.
This uses durable per-list receipts rather than guessing a single last successful queue position.
The validated dataset orders 2025/2026 first, then older sessions. Queue priority preserves this
claim order; parallel workers may finish in a different order. Existing `save_portal_candidate`
session comparison prevents older observations from overwriting newer department/faculty details.

The destination stays `portal_candidates`, using the existing `portal_candidate_sources` receipts
and normalized-email identity rules. Already saved list/JAMB pairs skip browser scraping. Historical
progress artifacts are not imported: old artifacts track one sequential index, while the new queue
tracks every observation independently. Previously failed or closed rows are rechecked on a fresh
queue because they lack saved-source receipts. No saved records need to be deleted or copied.

The draft adds `portal_swarm_runs` and `portal_swarm_queue`; no migration alters existing person
records. Queue uniqueness includes list name so one JAMB appearing in multiple lists isn't dropped.
Runs start in `preparing` status and only activate when all expected rows have been seeded.
Partial setup retries insert missing rows without resetting completed work.

Default worker count is **4**, adjustable from 1 to 20 when creating a run. Continuations keep
the stored worker count and selected session. Each Windows runner launches visible Google Chrome,
claims one queue row, and processes for **40 minutes plus the current candidate**. A coordinator
then dispatches another session. There is **no 24-hour cutoff** for this older dataset.
The draft does not add resizing controls or another local dashboard.

Claims expire after 20 minutes and require a UUID ownership token. Retries use at least a one-minute
cooldown, with at most three claims per observation. A `closed` application is terminal without an
extra retry. Saving the candidate, its receipt, and queue completion share a transaction. Finish RPCs
accept repeated calls with the last completed token/outcome, protecting against a lost HTTP response.
Portal attempts use randomized 3–7 second pacing and fresh browser cookies per candidate.
Six coordinator sessions with no new terminal outcomes pause the chain instead of restarting forever.

Only one legacy swarm can be preparing, running, or paused at a time. Its concurrency group is
separate from both current workflows. **At handoff, stop the old sequential workflow and its pending
continuations before starting this swarm**, so they do not scrape the same historical candidates
at once. That is a future deployment step, not something performed during this local preparation.
The 2026/2027 swarm remains independent and should not be stopped for this migration.

## Offline checks

```powershell
python run_legacy_swarm.py validate --session all
python -m unittest discover -s tests -q
python -m py_compile run_legacy_swarm.py
```

The `validate` mode does not create a Supabase client or open Chrome. It checks the full source
manifest/CSV integrity and reports counts. Offline tests use simulated database responses; they do
not alone prove the SQL runs against PostgreSQL. The SQL regression checks also passed in rollback-only
transactions during the authorized 29 October deployment, before and after applying the migration.
No test contacts were retained.

## Authorized deployment procedure

1. Review the final local diff and choose the initial worker count.
2. Confirm a working project access token; apply and validate the new migration and rollback test.
3. Checkpoint/stop the older sequential workflow and any queued continuations.
4. Publish only the reviewed files, then manually dispatch the legacy swarm.
5. Verify saved-receipt skips, first queue claims, source counts, and the first self-continuation.

Do not run both historical scrapers concurrently during the handoff. Starting a swarm does not
cancel another workflow automatically. Existing encrypted Actions secrets can be reused; no new
service-role credentials need to be published in code or embedded in HTML.
