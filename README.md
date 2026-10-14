# Portal bot admission lists

The **Portal Bot - CSV lists (45 minute sessions)** action checks out the CSVs in
[`data/admission_lists`](data/admission_lists) directly from this repository.
There is one CSV for each of the 46 downloaded PDFs, with 62,719 source rows.
The [conversion report](data/admission_lists/REPORT.md) shows per-file counts,
excluded rows, source issues, and the four indexed PDFs absent from the download.

The CSV workflow reads standardized fields and parses known portal labels
deterministically. It uses no AI model, Groq key, runtime PDF parser, or Storage
bucket. It writes to `public.portal_candidates` in Supabase project
`fkndpskocoatskwzrqwj` (theProject), using `CONTINUOUS_SUPABASE_URL` and
`CONTINUOUS_SUPABASE_KEY` from encrypted Actions secrets. Existing portal URL
and selector secrets are reused.

| CSV label | Meaning / destination |
| --- | --- |
| `jamb_reg_no` | Uppercase JAMB number; saved as `reg_no` |
| `fullname` | Name printed in the PDF; fallback if the slip omits its name |
| `department` | Printed course/department, uppercased; saved as `course` |
| `faculty` | Printed faculty column or section heading, uppercased without `FACULTY OF` |
| `academic_session` | Session from the PDF heading, falling back to its source folder |
| `list_name` | Stable `session/original-filename` identifier used for resume and uniqueness |
| `source_pdf`, `source_page`, `source_row` | Original file, page, and printed serial number |
| `queue_status` | `ready` or a specific source-data issue requiring review |

Names, source spelling, and missing values are preserved. Conflicting rows with
the same JAMB number in the same PDF are retained and flagged; the action does
not guess which department is correct. Identical ready-row duplicates are
collapsed in the processing queue. Only `ready` rows are submitted to the portal.
Each CSV is checked against its SHA-256 and manifest row/status counts before use.

## Running and resuming

Open `run_bot_continuous.yml` in GitHub Actions and choose **Run workflow**.
Select a session or `all` (recent sessions are processed first). Leave
`resume_run_id` blank for a fresh run, which skips records already in Supabase.
The job has a 45-minute processing budget; it finishes the current candidate,
saves a `portal-progress` artifact, and queues the next session with the same
session and previous run ID. The job timeout is 65 minutes to allow setup and
checkpoint upload. The chain stops when all queued candidates are attempted.

Each artifact contains atomic `progress.json`, `summary.json`, and `failures.csv`.
Failed candidates advance the checkpoint so they cannot trap subsequent sessions
in an infinite retry loop. Five consecutive portal failures stop the chain with
an error. A completed queue with failed candidates also reports a failed job.
Database failures stop immediately after bounded retries and never advance past
an unsaved record. Unique `(reg_no, list_name)` upserts make retries safe.

To recover after an error, fix the reported problem and start a run with the
failed run's `resume_run_id` (artifacts are retained for 14 days). To retry all
previous portal failures, start a fresh run with that field blank; successful
database records will still be skipped. A changed manifest/session cannot reuse
an old checkpoint. Cancel the active run to stop the chain; there is no cron.

Installation and dispatch use bounded retries. Database requests have 30-second
timeouts and up to three attempts; each portal candidate has up to two attempts.
Missing secrets, corrupt CSVs/checkpoints, changed inputs, schema errors, and
failed checkpoint uploads stop continuation with an actionable error. Logs show
counts and queue positions, without raw slips or secret values.

The original `run_bot.yml` remains available for the Storage/PDF bot and reads
`GROQ_MODEL` from Actions secrets, defaulting to `openai/gpt-oss-120b`.

## Local validation and conversion

Validation and tests require only the Python standard library:

```powershell
python run_csv_bot.py --validate-only
python -m unittest discover -s tests -v
```

To regenerate all CSVs and the manifest/report from the local PDFs:

```powershell
python -m pip install pymupdf
python tools/convert_admission_lists.py C:\Users\nwang\Desktop\utmesearch\UNN-admission-lists data/admission_lists
```

The converter uses PDF text coordinates and explicit headers. It fails if a
recognized registration token is lost or duplicated during conversion. PyMuPDF
is required for conversion only, not by the action.
