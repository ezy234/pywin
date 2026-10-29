# Older swarm: new writes, old data stays in place

Destination access was confirmed using `Top-bot/.env`. The requested portal schema and dedicated
encrypted connections are provisioned in `mcvphfhozzfcipkfmsbv`. Existing Wesynk tables are preserved.
The local `wesynk/.env` anonymous key is not used for administrative writes.

## Routing

- Old: `fkndpskocoatskwzrqwj`. Existing contacts and source receipts remain there.
- New: `mcvphfhozzfcipkfmsbv`. New contacts, new receipts, and the new swarm queue live here.
- No existing contact, receipt table, or old queue is copied or moved.
- The 2026/2027 workflow keeps its existing secrets and destination.

At each setup/worker startup, read only `list_name,reg_no` from the old receipt table into memory.
Bulk-mark matching public CSV queue entries as saved in the NEW queue. These are completion flags,
not copied contacts or inserted receipt rows. A per-worker old-key check covers any remaining claim.
The new project's receipt check then avoids scraping records already saved there. Only if both checks
have no matching source key does the worker scrape and save a new candidate to the new project.

An unavailable or malformed old-source response is fatal, rather than treated as an empty database.
The known new run is paused when startup cannot verify the old project. Startup checks are sufficient
once old writes have stopped, because the old saved-source key set is stable. New-project read/write
failures stop the worker; owned leases expire for later recovery.

Duplicate emails are merged within the new project only. An unsaved list observation for a person
already in the old project can create a new-project person; the old person is never copied or updated.
Matching by JAMB alone or by a single last queue position is deliberately avoided.

## Required dedicated encrypted Actions secrets

- `LEGACY_SUPABASE_URL`, `LEGACY_SUPABASE_KEY`: NEW destination service-role connection.
- `LEGACY_OLD_SUPABASE_URL`, `LEGACY_OLD_SUPABASE_KEY`: OLD source-reader connection.

The old connection is used only by the SELECT-only reader. The application enforces the exact new
project URL for write clients, with no fallback to old shared connection secrets.
Never put keys in Git, logs, exported contact files, or browser HTML.

## Deployment after destination access is available

1. Inspect destination schema; preserve all existing unrelated application tables.
2. Apply only the portal migrations in the new project, in this order:
   - `20261014000000_portal_candidates.sql`
   - `20261014010000_portal_candidate_email_identity.sql`
   - `20261028030000_portal_legacy_swarm.sql`
   - `20261029000000_portal_swarm_skip_saved.sql`
   - `20261029010000_portal_swarm_external_sources.sql`
3. Run `tests/legacy_swarm.sql` and `tests/legacy_swarm_external_sources.sql` as rollback-only tests,
   before any real new swarm run exists. Verify old-source skips create no new people or receipts.
4. Set the dedicated secrets. Leave `CONTINUOUS_SUPABASE_*` unchanged for the 2026 workflow.
5. Disable old legacy-swarm continuation, let current candidates finish, and ensure no old-version
   workers remain before publishing the changed workflow and dispatching its new queue.
6. Re-enable and dispatch with blank `run_id`, session `all`, and four workers. Old run UUIDs are not
   imported into the new project. The new queue skips every saved source found remotely in the old DB.
7. Verify old contact/receipt timestamps stay unchanged while new inserts occur in the destination.

All 52 offline tests passed, including source-key pagination, read-only old access, correct list/JAMB
matching, no-copy skips, and destination receipts. Both database regression scripts passed in rollback
transactions before and after schema deployment. The new tables were verified empty before cutover.
An operational handoff must finish stopping old-destination workers before dispatching the new run.
