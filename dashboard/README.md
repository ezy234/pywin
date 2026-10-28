# Dedicated 2026/2027 admission run

Source: `2026.2027-Primary-Admission-list-for-upload_copy.pdf` from the supplied ShareOps Documents folder.
The 238-page PDF converts to 6,488 validated candidates in `data/admission_2026/`.
There are 112 source department labels and 18 normalized faculty labels. The manifest records checksums,
provenance, and the one documented faculty spelling alias. No AI is needed.

Rebuild from the original PDF:

```powershell
python tools/convert_admission_2026.py "C:\path\to\2026.2027-Primary-Admission-list-for-upload_copy.pdf"
```

## Database and workflow

Apply the three `20261028...` migrations in order to project `fkndpskocoatskwzrqwj`.
They create `admission_2026_candidates`, `admission_2026_sources`, `admission_2026_runs`, and
`admission_2026_queue`, and `admission_2026_catalog`; none modifies the existing portal tables. All five use RLS and deny
anonymous/authenticated access. Only server-side service-role access is used.

The **Admission 2026-2027 - parallel swarm** workflow has its own concurrency group, with
`cancel-in-progress: false`. It does not dispatch or cancel the existing portal workflow.
It reuses the existing encrypted connection secrets without changing them. The new run uses the
exact tested `Print PUTME Slip` selector. Candidates with the portal's application-closed message
are marked closed immediately.

Start with an empty `run_id` and 12 workers. Each worker processes at most 40 minutes plus the
current candidate. A coordinator dispatches another session while work remains and the original
24-hour deadline has not passed. It also runs after worker errors, allowing expired leases to recover.
Windows runner availability may limit simultaneous workers; the matrix requests parallelism but
cannot guarantee GitHub supplies every runner immediately. Existing jobs share that account's
capacity even though this workflow never stops them.

Database leases last 20 minutes. Claims use row locks and `SKIP LOCKED`; each completion verifies
the lease token. Saving a person, the source receipt, and marking the queue item saved occur in one
transaction. Transient failures retry up to three times with at least a one-minute cooldown.
Successful source receipts are reused by a new run. A new run rechecks closed/failed entries.
Email deduplication applies only within the dedicated candidates table. Blank emails remain separate.
Completion means every queue item has a saved, closed, or failed outcome; inspect those counts rather
than treating completion as proof that every contact was obtainable.

The old portal clipboard flow is reused; readiness waits are bounded, not fixed three-minute sleeps.
The new runner isolates browser cookies per candidate and adds randomized 3–7 second pacing.
No service-role key, PAT, scraped contacts, or browser artifacts belong in source control.

## 24-hour estimate

Ideal workers = `ceil(6488 × average seconds per candidate / 86400)`.

| Average processing time | Ideal minimum | With about 20% overhead |
| --- | ---: | ---: |
| 60 seconds | 5 | 6 |
| 90 seconds | 7 | 9 |
| 120 seconds | 10 | 12 |
| 180 seconds | 14 | 17 |

Twelve workers is an initial estimate, not measured throughput for this list. At 120 seconds each,
12 workers take about 18 hours before setup, retries, and queue delays. All Windows workers share
the portal's network/rate limits; additional workers may not increase throughput proportionally.
The workflow stops new claims at the deadline, then finishes in-flight candidates.

## Local dashboard

Run from the repository:

```powershell
python tools/admission_dashboard.py
```

It opens `http://127.0.0.1:8876`. The HTML is `dashboard/admission_2026.html`; a local server is
required for database access and exports. Credentials live only in the ignored
`state/admission-dashboard-config.json` with keys `supabase_url`, `supabase_key`, `github_pat`.
Never put credentials in the HTML or publish this configuration. The server binds only to localhost,
requires a per-process token for API requests, checks the Host header, and checks Origin on controls.

Controls: start a fresh 24-hour run, pause new claims, resume the latest run within its existing
deadline, retry failed entries, and refresh. Pause does not cancel existing portal jobs or interrupt
an in-flight candidate. If the deadline expired, start a new run to retry remaining entries.
Only one new admission run can have running status at a time.

Filters: faculty, department, search, and queue status. Exports include all saved contacts matching
the selected faculty/department, independent of the displayed 500-row cap, search, and queue-status
filter. CSV escapes spreadsheet formulas; vCard (`.vcf`) exports names, phone, email, department,
and faculty with UTF-8 folding and escaping. No records are sent to a third-party export service.

Tests: `python -m unittest discover -s tests -q`; database queue regression checks are in
`tests/admission_2026_queue.sql` and run inside a rollback-only transaction.

The converted CSV is published to GitHub at the user's explicit request. The protected catalog also
stores the validated source rows; the workflow reads that catalog using its existing encrypted
service-role secret. Credentials and scraped portal contact details stay outside source control.
