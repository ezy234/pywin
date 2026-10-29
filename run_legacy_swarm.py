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
OLD_PROJECT = 'fkndpskocoatskwzrqwj'
NEW_PROJECT = 'mcvphfhozzfcipkfmsbv'


def client():
    from supabase import create_client
    from supabase.client import ClientOptions
    if os.environ.get('SUPABASE_URL', '').rstrip('/') != f'https://{NEW_PROJECT}.supabase.co':
        raise ValueError('Legacy writes must target the new Supabase project')
    return create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_KEY'],
                         options=ClientOptions(postgrest_client_timeout=30))


def old_client():
    from supabase import create_client
    from supabase.client import ClientOptions
    if os.environ.get('OLD_SUPABASE_URL', '').rstrip('/') != f'https://{OLD_PROJECT}.supabase.co':
        raise ValueError('Missing or incorrect read-only old-project settings')
    return create_client(os.environ['OLD_SUPABASE_URL'], os.environ['OLD_SUPABASE_KEY'],
                         options=ClientOptions(postgrest_client_timeout=30))


def load_old_sources(source):
    """Read only the source keys into memory; never copy old contacts/receipts."""
    keys, offset = set(), 0
    while True:
        response = retry('Read old-project saved source keys', lambda: source.table('portal_candidate_sources').select(
            'list_name,reg_no').order('list_name').order('reg_no').range(offset, offset + 999).execute())
        if not isinstance(response.data, list):
            raise RuntimeError('Invalid old-project source response; refusing to continue')
        for row in response.data:
            if not isinstance(row, dict) or not isinstance(row.get('list_name'), str) or not row['list_name'] or not isinstance(row.get('reg_no'), str) or not row['reg_no']:
                raise RuntimeError('Invalid old-project source key; refusing to continue')
            keys.add((row['list_name'], row['reg_no'].upper()))
        if len(response.data) < 1000:
            return keys
        offset += len(response.data)


def skip_old_sources(db, run, candidates, old_sources):
    # These keys already exist in the public CSV queue. Persist only completion
    # flags, not old contact data or copied source-receipt records.
    matches = [{'list_name': row['list_name'], 'reg_no': row['jamb_reg_no']}
               for row in candidates if (row['list_name'], row['jamb_reg_no']) in old_sources]
    skipped = 0
    for start in range(0, len(matches), 500):
        batch = matches[start:start+500]
        result = retry('Skip sources saved in old project', lambda: db.rpc('skip_external_portal_swarm',
            {'p_run': run, 'p_sources': batch}).execute()).data
        if not isinstance(result, int) or isinstance(result, bool) or result < 0:
            raise RuntimeError('Invalid old-source reconciliation response')
        skipped += result
    print(f'Bulk skipped {skipped} entries saved in the old project; no old contacts copied.', flush=True)
    return skipped


def output(name, value):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as handle:
            handle.write(f'{name}={value}\n')


def run_record(db, run):
    rows = retry('Read legacy swarm run', lambda: db.table('portal_swarm_runs').select('*').eq('id', run).execute()).data
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError('Unknown legacy swarm run')
    return rows[0]


def setup(db, requested, workers, session, old_sources=None):
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
        if old_sources is not None:
            skip_old_sources(db, run, dataset.candidates, old_sources)
        retry('Activate complete legacy queue', lambda: db.rpc('activate_portal_swarm', {'p_run': run}).execute())
        record = run_record(db, run)
    if record['status'] == 'running':
        if old_sources is not None:
            skip_old_sources(db, run, dataset.candidates, old_sources)
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


def process_job(db, job, token, scrape, pace, old_sources=None):
    source = job['candidate']
    if old_sources is not None and (job['list_name'], job['reg_no']) in old_sources:
        result = retry('Finish old-project source skip', lambda: db.rpc('finish_portal_swarm', {
            'p_id': job['id'], 'p_token': token, 'p_outcome': 'external_saved', 'p_data': None}).execute()).data
        return 'external_saved', result
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


def work(db, run, minutes, old_sources):
    import bot
    from playwright.sync_api import sync_playwright
    bot.PRINT_BUTTON_SELECTOR = 'button:text-is("Print PUTME Slip")'
    deadline = time.monotonic() + minutes * 60
    counts = {'saved': 0, 'external_saved': 0, 'closed': 0, 'retry': 0}
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
                                         lambda: time.sleep(random.uniform(3, 7)), old_sources)
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
    if args.mode in ('setup', 'worker'):
        try:
            old_sources = load_old_sources(old_client())
        except Exception:
            if args.run_id:
                retry('Pause run because old project cannot be checked', lambda: db.table('portal_swarm_runs').update(
                    {'status': 'paused', 'stop_reason': 'old_project_source_check_unavailable'}).eq(
                        'id', str(uuid.UUID(args.run_id))).eq('status', 'running').execute())
            raise RuntimeError('Old-project saved sources could not be checked; no new contacts will be inserted') from None
    if args.mode == 'setup':
        setup(db, args.run_id, args.workers, args.session, old_sources)
    elif args.mode == 'worker':
        work(db, str(uuid.UUID(args.run_id)), args.minutes, old_sources)
    else:
        check(db, str(uuid.UUID(args.run_id)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Legacy swarm failed: ' + type(error).__name__, flush=True)
        raise SystemExit(1)
