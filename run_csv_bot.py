"""Resume checked-in admission lists without AI or PDF parsing at runtime."""
import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

from candidate_lists import load_dataset

LABELS = {
    'fullname': r'Full\s*name', 'gender': r'Gender', 'date_of_birth': r'Date\s+of\s+birth',
    'email': r'Email(?:\s+address)?', 'mobile_phone': r'Mobile(?:\s+phone)?',
    'reg_no': r'JAMB\s+Reg(?:istration)?\.?\s*(?:No\.?|Number)',
    'state_of_origin': r'State\s+of\s+origin', 'address': r'(?<!Email )\bAddress',
    'local_government': r'Local\s+government(?:\s+area)?|L\.?G\.?A\.?',
}


def parse_labeled_slip(raw, candidate):
    """Use explicit portal labels and PDF metadata; never request an AI model."""
    pattern = re.compile(r'(?<!\w)(?:' + '|'.join(f'(?P<{field}>{label})' for field, label in LABELS.items()) + r')(?!\w)', re.I)
    matches, seen_fields = [], set()
    for match in pattern.finditer(raw):
        if match.lastgroup not in seen_fields:
            matches.append(match)
            seen_fields.add(match.lastgroup)
    parsed = {field: '' for field in LABELS}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        value = re.sub(r'\s+', ' ', raw[match.end():end]).strip(' :\t\r\n-')
        if match.lastgroup and not parsed[match.lastgroup]:
            parsed[match.lastgroup] = value
    expected = candidate['jamb_reg_no']
    actual = re.match(r'[0-9A-Z]+', parsed['reg_no'].upper())
    if not actual or actual.group() != expected:
        raise ValueError('Portal slip does not contain the requested JAMB registration number')
    parsed['reg_no'] = expected
    # The downloaded list provides an authoritative name if the slip omits it.
    parsed['fullname'] = parsed['fullname'] or candidate['fullname']
    parsed['gender'] = parsed['gender'].upper() if parsed['gender'].upper() in {'MALE', 'FEMALE'} else ''
    for field, expression in [('date_of_birth', r'\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}'),
                              ('email', r'[^\s@]+@[^\s@]+\.[^\s@]+'), ('mobile_phone', r'\+?\d[\d ()-]{8,18}\d')]:
        found = re.search(expression, parsed[field])
        parsed[field] = found.group().strip() if found else ''
    parsed.update(course=candidate['department'], faculty=candidate['faculty'], list_name=candidate['list_name'])
    return parsed


def retry(operation, callback, attempts=3, sleep=time.sleep):
    for attempt in range(1, attempts + 1):
        try:
            return callback()
        except Exception as error:
            code = str(getattr(error, 'code', ''))
            permanent = isinstance(error, (ValueError, PermissionError)) or code.startswith(('22', '23', '42')) or code in {'PGRST204', 'PGRST205', 'PGRST301', 'PGRST302'}
            print(f'WARNING: {operation} failed ({type(error).__name__}); attempt {attempt}/{attempts}.', flush=True)
            if permanent or attempt == attempts:
                raise RuntimeError(f'{operation} failed ({type(error).__name__}). Check connectivity, Actions secrets, and destination table permissions/schema.') from None
            sleep(min(2 ** attempt, 15))


def save_progress(path, progress):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(progress, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def load_progress(path, dataset):
    if Path(path).exists():
        try:
            progress = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError('Progress checkpoint is unreadable; restore a valid artifact or start a fresh run') from None
        if progress.get('fingerprint') != dataset.fingerprint:
            raise ValueError('CSV manifest/session changed since this checkpoint; start a fresh run to resume from Supabase records')
        if not isinstance(progress.get('next_index'), int) or not 0 <= progress['next_index'] <= len(dataset.candidates):
            raise ValueError('Invalid next_index in progress checkpoint')
        for field in ['saved', 'skipped_existing', 'failed', 'consecutive_failures']:
            if not isinstance(progress.get(field), int) or progress[field] < 0:
                raise ValueError('Invalid progress counters in checkpoint')
        if not isinstance(progress.get('failures'), list):
            raise ValueError('Invalid failures in progress checkpoint')
        return progress
    return {'fingerprint': dataset.fingerprint, 'next_index': 0, 'saved': 0, 'skipped_existing': 0,
            'failed': 0, 'consecutive_failures': 0, 'failures': [], 'complete': False}


def fetch_done(client, table, list_name):
    done, start = set(), 0
    while True:
        response = retry('Read saved candidate checkpoint', lambda: client.table(table).select('reg_no').eq('list_name', list_name).order('reg_no').range(start, start + 999).execute())
        rows = response.data
        if not isinstance(rows, list):
            raise RuntimeError('Supabase returned an invalid checkpoint response')
        done.update(str(row['reg_no']).upper() for row in rows if row.get('reg_no'))
        if len(rows) < 1000:
            return done
        start += 1000


def process_queue(dataset, progress, client, table, scrape, checkpoint, deadline, clock=time.monotonic):
    done_lists = {}
    initial_index = progress['next_index']
    while progress['next_index'] < len(dataset.candidates):
        if clock() >= deadline:
            if progress['next_index'] == initial_index:
                raise RuntimeError('No queue progress within the session budget; refusing to restart indefinitely')
            save_progress(checkpoint, progress)
            return False
        candidate = dataset.candidates[progress['next_index']]
        list_name, reg = candidate['list_name'], candidate['jamb_reg_no']
        if list_name not in done_lists:
            done_lists[list_name] = fetch_done(client, table, list_name)
            print(f'Processing list {list_name}; saved registration numbers: {len(done_lists[list_name])}.', flush=True)
        if reg in done_lists[list_name]:
            progress['skipped_existing'] += 1
            progress['next_index'] += 1
            save_progress(checkpoint, progress)
            continue
        parsed = None
        for attempt in range(1, 3):
            raw = scrape(reg)
            if raw:
                try:
                    parsed = parse_labeled_slip(raw, candidate)
                    break
                except ValueError:
                    pass
            print(f'WARNING: No matching labeled slip at queue position {progress["next_index"] + 1}; attempt {attempt}/2.', flush=True)
        if parsed is None:
            progress['failed'] += 1
            progress['consecutive_failures'] += 1
            progress['failures'].append({'list_name': list_name, 'jamb_reg_no': reg, 'source_page': candidate['source_page'], 'reason': 'portal_unavailable_or_slip_mismatch', 'attempts': 2})
        else:
            retry('Save candidate', lambda: client.table(table).upsert(parsed, on_conflict='reg_no,list_name', ignore_duplicates=True).execute())
            done_lists[list_name].add(reg)
            progress['saved'] += 1
            progress['consecutive_failures'] = 0
        progress['next_index'] += 1
        save_progress(checkpoint, progress)
        if progress['consecutive_failures'] >= 5:
            raise RuntimeError('Five consecutive portal failures: stopping the chain. Check portal availability and selectors; failed positions are in the progress artifact.')
    progress['complete'] = True
    save_progress(checkpoint, progress)
    return True


def reports(directory, dataset, progress, status):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    summary = {'status': status, **dataset.statistics, **{key: progress[key] for key in ['next_index', 'saved', 'skipped_existing', 'failed', 'complete']}}
    (directory / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    with (directory / 'failures.csv').open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, ['list_name', 'jamb_reg_no', 'source_page', 'reason', 'attempts'])
        writer.writeheader()
        writer.writerows(progress['failures'])
    print(json.dumps(summary), flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as handle:
            handle.write('### Admission CSV progress\n\n```json\n' + json.dumps(summary, indent=2) + '\n```\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', default='data/admission_lists/manifest.json')
    parser.add_argument('--session', default='all')
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--minutes', type=int, default=45)
    parser.add_argument('--state-dir', default='state')
    args = parser.parse_args()
    if not 1 <= args.minutes <= 45:
        raise ValueError('Processing budget must be between 1 and 45 minutes')
    dataset = load_dataset(args.manifest, args.session)
    if args.validate_only:
        print(json.dumps(dataset.statistics), flush=True)
        return 0
    required = ['SUPABASE_URL', 'SUPABASE_KEY', 'SUPABASE_TABLE', 'PORTAL_URL', 'REG_INPUT_SELECTOR', 'LOGIN_BUTTON_SELECTOR', 'PRINT_BUTTON_SELECTOR', 'WINDOW_TITLE_RE']
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError('Missing Actions settings: ' + ', '.join(missing))
    checkpoint = Path(args.state_dir) / 'progress.json'
    progress = load_progress(checkpoint, dataset)
    save_progress(checkpoint, progress)
    Path('.continue').unlink(missing_ok=True)
    try:
        import bot
        from playwright.sync_api import sync_playwright
        from supabase import create_client
        from supabase.client import ClientOptions
        client = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_KEY'], options=ClientOptions(postgrest_client_timeout=30))
        table = os.environ['SUPABASE_TABLE']
        retry('Validate destination table', lambda: client.table(table).select('reg_no,list_name').limit(1).execute())
        deadline = time.monotonic() + args.minutes * 60
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=False)
            try:
                context = browser.new_context()
                complete = process_queue(dataset, progress, client, table, lambda reg: bot.scrape_slip(context, reg), checkpoint, deadline)
            finally:
                browser.close()
        reports(args.state_dir, dataset, progress, 'complete' if complete else 'continuing')
        if not complete:
            Path('.continue').write_text('More CSV candidates remain.\n', encoding='utf-8')
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as handle:
                handle.write('complete=' + str(complete).lower() + '\n')
        return 1 if complete and progress['failed'] else 0
    except Exception:
        reports(args.state_dir, dataset, progress, 'failed')
        raise


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # API exception strings may contain record values; emit a safe diagnostic.
        detail = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        print('ERROR: ' + detail, file=sys.stderr, flush=True)
        raise SystemExit(1)
