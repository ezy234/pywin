"""Swarm runner for the older, checked-in 2022-2026 CSV lists."""
import argparse
from datetime import datetime, timezone
import json
import os
import random
import time
import uuid

from candidate_lists import load_dataset
from run_csv_bot import parse_labeled_slip, retry

MANIFEST = 'data/admission_lists/manifest.json'


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
    rows = retry('Read legacy swarm run', lambda: db.table('portal_swarm_runs').select('*').eq('id', run).execute()).data
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError('Unknown legacy swarm run')
    return rows[0]


def setup(db, requested, workers, session):
    if not 1 <= workers <= 20:
        raise ValueError('Workers must be between 1 and 20')
    run = str(uuid.UUID(requested)) if requested else str(uuid.uuid5(uuid.NAMESPACE_URL,
        'ezy234/pywin/legacy-swarm/' + os.environ['GITHUB_RUN_ID']))
    rows = retry('Find legacy swarm run', lambda: db.table('portal_swarm_runs').select('*').eq('id', run).execute()).data
    # Continuations use the saved session; inputs cannot silently change the queue.
    dataset = load_dataset(MANIFEST, rows[0]['academic_session'] if rows else session)
    if not rows:
        retry('Create legacy swarm run', lambda: db.table('portal_swarm_runs').upsert({
            'id': run, 'fingerprint': dataset.fingerprint, 'academic_session': session,
            'workers': workers, 'expected_rows': len(dataset.candidates)}, on_conflict='id', ignore_duplicates=True).execute())
    record = run_record(db, run)
    if record['fingerprint'] != dataset.fingerprint or record['expected_rows'] != len(dataset.candidates):
        raise ValueError('CSV queue changed; refuse to mix checkpoints')
    if record['status'] == 'preparing':
        for start in range(0, len(dataset.candidates), 250):
            batch = [{'run_id': run, 'list_name': row['list_name'], 'reg_no': row['jamb_reg_no'],
                      'priority': index, 'candidate': row}
                     for index, row in enumerate(dataset.candidates[start:start+250], start=start)]
            retry('Seed legacy queue', lambda: db.table('portal_swarm_queue').upsert(batch,
                on_conflict='run_id,list_name,reg_no', ignore_duplicates=True).execute())
        retry('Activate complete legacy queue', lambda: db.rpc('activate_portal_swarm', {'p_run': run}).execute())
        record = run_record(db, run)
    if record['status'] == 'running':
        skipped = retry('Bulk reconcile saved legacy sources', lambda: db.rpc('skip_saved_portal_swarm', {'p_run': run}).execute()).data
        if not isinstance(skipped, int) or isinstance(skipped, bool) or skipped < 0:
            raise RuntimeError('Invalid saved-source reconciliation response')
        print(f'Bulk skipped {skipped} queue entries already saved in Supabase.', flush=True)
    output('run_id', run)
    output('matrix', json.dumps(list(range(record['workers']))))
    output('active', str(record['status'] == 'running').lower())
    print(f'Legacy swarm {run}: {len(dataset.candidates)} candidates; {record["workers"]} workers.', flush=True)


def source_payload(source):
    return {'fullname': source['fullname'], 'reg_no': source['jamb_reg_no'], 'list_name': source['list_name'],
            'course': source['department'], 'faculty': source['faculty']}


def process_job(db, job, token, scrape, pace):
    source = job['candidate']
    existing = retry('Read existing legacy receipt', lambda: db.table('portal_candidate_sources').select('candidate_id').eq(
        'list_name', job['list_name']).eq('reg_no', job['reg_no']).limit(1).execute()).data
    outcome, parsed = 'retry', None
    if existing:
        outcome, parsed = 'saved', source_payload(source)
    else:
        pace()
        raw = scrape(job['reg_no'])
        if getattr(raw, 'reason', None) == 'application_closed':
            outcome = 'closed'
        elif raw:
            try:
                parsed = parse_labeled_slip(raw, source)
                outcome = 'saved'
            except ValueError:
                pass
    result = retry('Complete legacy queue item', lambda: db.rpc('finish_portal_swarm', {
        'p_id': job['id'], 'p_token': token, 'p_outcome': outcome, 'p_data': parsed}).execute()).data
    return outcome, result


def work(db, run, minutes):
    import bot
    from playwright.sync_api import sync_playwright
    bot.PRINT_BUTTON_SELECTOR = 'button:text-is("Print PUTME Slip")'
    deadline = time.monotonic() + minutes * 60
    counts = {'saved': 0, 'closed': 0, 'retry': 0}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=False)
        try:
            while time.monotonic() < deadline:
                token = str(uuid.uuid4())
                # An ambiguous claim is not retried; its lease will recover.
                job = db.rpc('claim_portal_swarm', {'p_run': run, 'p_token': token}).execute().data
                if not job:
                    break
                def scrape(reg):
                    with browser.new_context() as context:
                        return bot.scrape_slip(context, reg)
                outcome, _ = process_job(db, job, token, scrape,
                                         lambda: time.sleep(random.uniform(3, 7)))
                counts[outcome] += 1
                print(json.dumps(counts), flush=True)
        finally:
            browser.close()


def check(db, run):
    record = run_record(db, run)
    pending = retry('Count unfinished legacy queue', lambda: db.table('portal_swarm_queue').select(
        'id', count='exact', head=True).eq('run_id', run).in_('status', ['pending', 'leased']).execute()).count
    if pending is None:
        raise RuntimeError('Missing queue count; refusing to declare completion')
    if record['status'] == 'running' and pending > 0:
        terminal = retry('Count terminal legacy outcomes', lambda: db.table('portal_swarm_queue').select(
            'id', count='exact', head=True).eq('run_id', run).in_('status', ['saved', 'closed', 'failed']).execute()).count
        if terminal is None:
            raise RuntimeError('Missing terminal count; refusing continuation')
        stalled = record['stalled_cycles'] + 1 if terminal <= record['last_terminal_count'] else 0
        update = {'last_terminal_count': terminal, 'stalled_cycles': stalled}
        if stalled >= 6:
            update.update(status='paused', stop_reason='no_progress_after_six_sessions')
        retry('Record legacy session progress', lambda: db.table('portal_swarm_runs').update(update).eq('id', run).eq('status', 'running').execute())
        if stalled >= 6:
            record = {**record, 'status': 'paused'}
    if record['status'] == 'running' and pending == 0:
        retry('Complete legacy run', lambda: db.table('portal_swarm_runs').update({
            'status': 'complete', 'completed_at': datetime.now(timezone.utc).isoformat()
        }).eq('id', run).eq('status', 'running').execute())
    again = record['status'] == 'running' and pending > 0
    output('continue', str(again).lower())
    output('workers', record['workers'])
    output('session', record['academic_session'])
    print(f'Legacy unfinished queue: {pending}; continue: {again}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['validate', 'setup', 'worker', 'check'])
    parser.add_argument('--run-id', default='')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--session', default='all')
    parser.add_argument('--minutes', type=int, default=40)
    args = parser.parse_args()
    if not 1 <= args.minutes <= 40 or not 1 <= args.workers <= 20:
        raise ValueError('Budget must be 1-40 minutes and worker count 1-20')
    if args.mode == 'validate':
        print(json.dumps(load_dataset(MANIFEST, args.session).statistics))
        return
    db = client()
    if args.mode == 'setup':
        setup(db, args.run_id, args.workers, args.session)
    elif args.mode == 'worker':
        work(db, str(uuid.UUID(args.run_id)), args.minutes)
    else:
        check(db, str(uuid.UUID(args.run_id)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Legacy swarm failed: ' + type(error).__name__, flush=True)
        raise SystemExit(1)
