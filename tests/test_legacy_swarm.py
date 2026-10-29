from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from candidate_lists import Dataset
from portal_status import ApplicationClosed
import run_legacy_swarm as swarm


def candidate(session='2025-2026'):
    return {'jamb_reg_no': '202500000001AA', 'fullname': 'TEST PERSON', 'department': 'TEST COURSE',
            'faculty': 'TEST FACULTY', 'academic_session': session, 'list_name': session+'/test'}


class Query:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self.operation, self.payload = db, table, {}, 'select', None
        self.states = None

    def select(self, *args, **kwargs):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def limit(self, *args):
        return self

    def in_(self, key, values):
        self.states = values
        return self

    def upsert(self, payload, **kwargs):
        self.operation, self.payload = 'upsert', deepcopy(payload)
        return self

    def update(self, payload):
        self.operation, self.payload = 'update', payload
        return self

    def execute(self):
        self.db.queries.append((self.table, self.operation, self.filters, self.payload))
        if self.table == 'portal_swarm_runs':
            if self.operation == 'upsert' and self.db.record is None:
                self.db.record = {**self.payload, 'status': 'preparing', 'stalled_cycles': 0, 'last_terminal_count': 0}
            elif self.operation == 'update':
                self.db.record.update(self.payload)
            return SimpleNamespace(data=[deepcopy(self.db.record)] if self.db.record else [])
        if self.table == 'portal_swarm_queue':
            if self.operation == 'upsert':
                self.db.batches.extend(self.payload)
            count = self.db.pending if self.states == ['pending', 'leased'] else self.db.terminal
            return SimpleNamespace(data=[], count=count)
        if self.table == 'portal_candidate_sources':
            return SimpleNamespace(data=[{'candidate_id': 7}] if self.db.receipt else [])
        raise AssertionError('Unexpected destination '+self.table)


class FakeDB:
    def __init__(self, record=None, receipt=False, pending=1, terminal=0):
        self.record, self.receipt = record, receipt
        self.pending, self.terminal = pending, terminal
        self.queries, self.batches, self.calls = [], [], []

    def table(self, name):
        return Query(self, name)

    def rpc(self, name, params):
        self.calls.append((name, deepcopy(params)))
        def execute():
            if name == 'activate_portal_swarm':
                self.record['status'] = 'running'
                return SimpleNamespace(data=True)
            if name == 'finish_portal_swarm':
                return SimpleNamespace(data={'action': 'already_processed' if self.receipt else 'inserted'})
            if name == 'skip_saved_portal_swarm':
                return SimpleNamespace(data=17)
            raise AssertionError('Unexpected RPC '+name)
        return SimpleNamespace(execute=execute)


class LegacySwarmTests(unittest.TestCase):
    def record(self, **overrides):
        return {'id': '12345678-1234-4234-8234-123456789012', 'fingerprint': 'fingerprint',
            'academic_session': 'all', 'workers': 3, 'expected_rows': 2, 'status': 'running',
            'stalled_cycles': 0, 'last_terminal_count': 0, **overrides}

    def job(self):
        source = candidate()
        return {'id': 1, 'list_name': source['list_name'], 'reg_no': source['jamb_reg_no'], 'candidate': source}

    def test_setup_preserves_same_jamb_in_different_lists_and_persists_priority(self):
        db = FakeDB()
        dataset = Dataset([candidate(), candidate('2024-2025')], 'fingerprint', {})
        with patch.object(swarm, 'load_dataset', return_value=dataset), patch.object(swarm, 'output'), patch.dict('os.environ', GITHUB_RUN_ID='123'):
            swarm.setup(db, '', 3, 'all')
        self.assertEqual(len(db.batches), 2)
        self.assertEqual([r['priority'] for r in db.batches], [0, 1])
        self.assertNotEqual(db.batches[0]['list_name'], db.batches[1]['list_name'])
        self.assertEqual(db.record['status'], 'running')
        self.assertEqual([call[0] for call in db.calls], ['activate_portal_swarm', 'skip_saved_portal_swarm'])

    def test_continuation_uses_original_session_and_does_not_reseed_running_queue(self):
        db = FakeDB(self.record(academic_session='2024-2025'))
        with patch.object(swarm, 'load_dataset', return_value=Dataset([candidate(), candidate()], 'fingerprint', {})) as load, patch.object(swarm, 'output') as out:
            swarm.setup(db, db.record['id'], 20, 'all')
        load.assert_called_once_with(swarm.MANIFEST, '2024-2025')
        self.assertEqual(db.batches, [])
        self.assertEqual(db.calls[-1], ('skip_saved_portal_swarm', {'p_run': db.record['id']}))
        self.assertIn(('matrix', '[0, 1, 2]'), [call.args for call in out.call_args_list])

    def test_changed_manifest_refuses_resume(self):
        db = FakeDB(self.record())
        with patch.object(swarm, 'load_dataset', return_value=Dataset([candidate(), candidate()], 'changed', {})):
            with self.assertRaisesRegex(ValueError, 'changed'):
                swarm.setup(db, db.record['id'], 3, 'all')
        self.assertEqual(db.calls, [])

    def test_paused_setup_does_not_reconcile_or_resume_work(self):
        db = FakeDB(self.record(status='paused'))
        with patch.object(swarm, 'load_dataset', return_value=Dataset([candidate(), candidate()], 'fingerprint', {})), patch.object(swarm, 'output') as out:
            swarm.setup(db, db.record['id'], 3, 'all')
        self.assertEqual(db.calls, [])
        self.assertIn(('active', 'false'), [call.args for call in out.call_args_list])

    def test_saved_receipt_avoids_browser_and_pacing(self):
        db, scrape, pace = FakeDB(receipt=True), Mock(), Mock()
        outcome, _ = swarm.process_job(db, self.job(), 'token', scrape, pace)
        self.assertEqual(outcome, 'saved')
        scrape.assert_not_called()
        pace.assert_not_called()
        self.assertEqual(db.calls[0][1]['p_data']['course'], 'TEST COURSE')

    def test_closed_application_has_one_scrape_and_closed_outcome(self):
        db, scrape = FakeDB(), Mock(return_value=ApplicationClosed())
        self.assertEqual(swarm.process_job(db, self.job(), 'token', scrape, Mock())[0], 'closed')
        scrape.assert_called_once()
        self.assertIsNone(db.calls[0][1]['p_data'])

    def test_wrong_candidate_slip_retries_without_saving_wrong_person(self):
        db = FakeDB()
        outcome, _ = swarm.process_job(db, self.job(), 'token', Mock(return_value='JAMB Reg No: 202599999999AA'), Mock())
        self.assertEqual(outcome, 'retry')
        self.assertIsNone(db.calls[0][1]['p_data'])

    def test_valid_slip_uses_original_destination_rpc_and_pdf_department(self):
        db = FakeDB()
        raw = 'Fullname: TEST PERSON\nEmail: test@example.invalid\nJAMB Reg No: 202500000001AA'
        outcome, _ = swarm.process_job(db, self.job(), 'token', Mock(return_value=raw), Mock())
        self.assertEqual(outcome, 'saved')
        name, params = db.calls[0]
        self.assertEqual(name, 'finish_portal_swarm')
        self.assertEqual(params['p_data']['email'], 'test@example.invalid')
        self.assertEqual(params['p_data']['faculty'], 'TEST FACULTY')

    def test_progress_continues_without_24_hour_deadline(self):
        db = FakeDB(self.record(), pending=10, terminal=2)
        with patch.object(swarm, 'output') as out:
            swarm.check(db, db.record['id'])
        self.assertIn(('continue', 'true'), [c.args for c in out.call_args_list])
        self.assertEqual(db.record['stalled_cycles'], 0)

    def test_empty_queue_marks_complete_and_does_not_dispatch(self):
        db = FakeDB(self.record(), pending=0)
        with patch.object(swarm, 'output') as out:
            swarm.check(db, db.record['id'])
        self.assertEqual(db.record['status'], 'complete')
        self.assertIn(('continue', 'false'), [c.args for c in out.call_args_list])

    def test_paused_run_never_continues(self):
        db = FakeDB(self.record(status='paused'), pending=10)
        with patch.object(swarm, 'output') as out:
            swarm.check(db, db.record['id'])
        self.assertIn(('continue', 'false'), [c.args for c in out.call_args_list])

    def test_six_sessions_without_progress_pause_the_chain(self):
        db = FakeDB(self.record(stalled_cycles=5), pending=10)
        with patch.object(swarm, 'output') as out:
            swarm.check(db, db.record['id'])
        self.assertEqual(db.record['status'], 'paused')
        self.assertEqual(db.record['stop_reason'], 'no_progress_after_six_sessions')
        self.assertIn(('continue', 'false'), [c.args for c in out.call_args_list])

    def test_missing_count_cannot_mark_run_complete(self):
        db = FakeDB(self.record(), pending=None)
        with self.assertRaisesRegex(RuntimeError, 'Missing queue count'):
            swarm.check(db, db.record['id'])
        self.assertEqual(db.record['status'], 'running')
