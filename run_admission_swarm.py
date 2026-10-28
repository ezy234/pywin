"""Isolated 2026 admission queue: setup, workers, and continuation decisions."""
import argparse
import json
import os
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from run_csv_bot import parse_labeled_slip, retry

MANIFEST = 'data/admission_2026/manifest.json'


def client():
    from supabase import create_client
    from supabase.client import ClientOptions
    return create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_KEY'],
                         options=ClientOptions(postgrest_client_timeout=30))


def output(name, value):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as handle:
            handle.write(f'{name}={value}\n')


def run_record(db, run):
    rows = retry('Read swarm run', lambda: db.table('admission_2026_runs').select('*').eq('id', run).execute()).data
    if len(rows) != 1:
        raise ValueError('Unknown admission run')
    return rows[0]


def setup(db, requested, workers):
    catalog = retry('Read private admission catalog', lambda: db.table('admission_2026_catalog').select('fingerprint,candidates').eq('id', 'primary').execute()).data
    if len(catalog) != 1 or len(catalog[0]['candidates']) != 6488:
        raise ValueError('Private admission catalog is missing or incomplete')
    candidates, fingerprint = catalog[0]['candidates'], catalog[0]['fingerprint']
    if not 1 <= workers <= 20:
        raise ValueError('Workers must be between 1 and 20')
    run = str(uuid.UUID(requested)) if requested else str(uuid.uuid5(uuid.NAMESPACE_URL, 'ezy234/pywin/admission-2026/' + os.environ['GITHUB_RUN_ID']))
    rows = db.table('admission_2026_runs').select('*').eq('id', run).execute().data
    if not rows:
        retry('Create isolated run', lambda: db.table('admission_2026_runs').upsert(
            {'id': run, 'fingerprint': fingerprint, 'workers': workers}, on_conflict='id', ignore_duplicates=True).execute())
    record = run_record(db, run)
    if record['fingerprint'] != fingerprint:
        raise ValueError('Manifest changed: refuse to mix queue versions')
    # Retrying a partially completed setup only inserts missing rows.
    for start in range(0, len(candidates), 250):
        batch = [{'run_id': run, 'reg_no': row['jamb_reg_no'], 'candidate': row} for row in candidates[start:start+250]]
        retry('Seed admission queue', lambda: db.table('admission_2026_queue').upsert(
            batch, on_conflict='run_id,reg_no', ignore_duplicates=True).execute())
    output('run_id', run)
    output('matrix', json.dumps(list(range(record['workers']))))
    output('active', str(record['status'] == 'running').lower())
    print(f'Isolated run {run}: {len(candidates)} candidates, {record["workers"]} workers.', flush=True)


def work(db, run, minutes):
    import bot
    from playwright.sync_api import sync_playwright
    # Pin this list's verified controls; old workflow secrets remain untouched.
    bot.PRINT_BUTTON_SELECTOR = 'button:text-is("Print PUTME Slip")'
    deadline = time.monotonic() + minutes * 60
    counts = {'saved': 0, 'closed': 0, 'retry': 0}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=False)
        try:
            while time.monotonic() < deadline:
                token = str(uuid.uuid4())
                # Do not retry an ambiguous claim: wait for any lease to expire.
                job = db.rpc('claim_admission_2026', {'p_run': run, 'p_token': token}).execute().data
                if not job:
                    break
                existing = retry('Read isolated source receipt', lambda: db.table('admission_2026_sources').select('candidate_id').eq(
                    'list_name', job['candidate']['list_name']).eq('reg_no', job['reg_no']).limit(1).execute()).data
                if existing:
                    source = job['candidate']
                    payload = {'fullname': source['fullname'], 'reg_no': job['reg_no'],
                        'list_name': source['list_name'], 'course': source['department'], 'faculty': source['faculty']}
                    db.rpc('finish_admission_2026', {'p_id': job['id'], 'p_token': token,
                        'p_outcome': 'saved', 'p_data': payload}).execute()
                    counts['saved'] += 1
                    continue
                time.sleep(random.uniform(3, 7))
                # Isolate cookies between users and close all print popups.
                with browser.new_context() as context:
                    raw = bot.scrape_slip(context, job['reg_no'])
                outcome, parsed = 'retry', None
                if getattr(raw, 'reason', None) == 'application_closed':
                    outcome = 'closed'
                elif raw:
                    try:
                        parsed = parse_labeled_slip(raw, job['candidate'])
                        outcome = 'saved'
                    except ValueError:
                        pass
                # Finish and candidate save share one database transaction.
                db.rpc('finish_admission_2026', {'p_id': job['id'], 'p_token': token,
                       'p_outcome': outcome, 'p_data': parsed}).execute()
                counts[outcome] += 1
                print(json.dumps(counts), flush=True)
        finally:
            browser.close()


def check(db, run):
    record = run_record(db, run)
    pending = db.table('admission_2026_queue').select('id', count='exact', head=True).eq('run_id', run).in_('status', ['pending', 'leased']).execute().count
    expired = datetime.now(timezone.utc) >= datetime.fromisoformat(record['deadline'].replace('Z', '+00:00'))
    if record['status'] == 'running' and (expired or not pending):
        db.table('admission_2026_runs').update({'status': 'deadline' if expired and pending else 'complete'}).eq('id', run).eq('status', 'running').execute()
    again = record['status'] == 'running' and bool(pending) and not expired
    output('continue', str(again).lower())
    output('workers', record['workers'])
    print(f'Unfinished queue entries: {pending}; continue: {again}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['setup', 'worker', 'check'])
    parser.add_argument('--run-id', default='')
    parser.add_argument('--workers', type=int, default=12)
    parser.add_argument('--minutes', type=int, default=40)
    args = parser.parse_args()
    try:
        if not 1 <= args.minutes <= 40:
            raise ValueError('Worker budget must be 1 to 40 minutes')
        db = client()
        if args.mode == 'setup':
            setup(db, args.run_id, args.workers)
        elif args.mode == 'worker':
            work(db, str(uuid.UUID(args.run_id)), args.minutes)
        else:
            check(db, str(uuid.UUID(args.run_id)))
    except Exception as error:
        print('Swarm failed: ' + type(error).__name__, flush=True)
        raise SystemExit(1)
