import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from candidate_lists import COLUMNS, Dataset, load_dataset
from run_csv_bot import load_progress, parse_labeled_slip, process_queue, retry, save_progress, save_candidate, SAVE_ACTIONS


def candidate(number=1):
    return {'jamb_reg_no': f'20250000{number:04d}AA', 'fullname': 'OLGA TEST PERSON',
            'department': 'CROP SCIENCE', 'faculty': 'AGRICULTURE', 'academic_session': '2025-2026',
            'list_name': '2025-2026/test', 'source_pdf': '2025-2026/test.pdf',
            'source_page': '1', 'source_row': str(number), 'queue_status': 'ready'}


def slip(row):
    return ('Fullname: OLGA TEST PERSON\nGender: FEMALE\nDate of birth: 2000-01-02\n'
            'State of origin: TEST STATE\nAddress: 12 TEST ROAD\nLocal government: TEST LGA\n'
            'Email address: test@example.invalid\nMobile phone: 08000000000\nJAMB Reg No: ' + row['jamb_reg_no'])


class FakeQuery:
    def __init__(self, client):
        self.client, self.list_name, self.bounds = client, '', (0, 999)

    def select(self, *args):
        return self

    def eq(self, field, value):
        self.list_name = value
        return self

    def order(self, *args):
        return self

    def range(self, start, end):
        self.bounds = start, end
        return self

    def execute(self):
        rows = [{'reg_no': row['reg_no']} for row in self.client.saved if row['list_name'] == self.list_name]
        return SimpleNamespace(data=rows[self.bounds[0]:self.bounds[1] + 1])


class FakeClient:
    def __init__(self, saved=None, fail_writes=False):
        self.saved, self.fail_writes = list(saved or []), fail_writes
        self.table_calls = []

    def table(self, *args):
        self.table_calls.append(args[0])
        return FakeQuery(self)

    def rpc(self, name, params):
        def execute():
            if self.fail_writes:
                raise PermissionError('denied')
            self.saved.append(params['p_candidate'])  # Observed writes, not a model of SQL deduplication.
            return SimpleNamespace(data={'action': 'inserted', 'candidate_id': len(self.saved)})
        return SimpleNamespace(execute=execute)


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def fixture(self, rows):
        content = io.StringIO(newline='')
        writer = csv.DictWriter(content, COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        raw = content.getvalue().encode()
        (self.root / 'list.csv').write_bytes(raw)
        counts = {}
        for row in rows:
            counts[row['queue_status']] = counts.get(row['queue_status'], 0) + 1
        item = {'path': 'list.csv', 'list_name': '2025-2026/test', 'academic_session': '2025-2026',
                'source_pdf': '2025-2026/test.pdf', 'pages': 1, 'rows': len(rows),
                'sha256': hashlib.sha256(raw).hexdigest(), 'status_counts': counts}
        manifest = {'schema_version': 1, 'columns': COLUMNS, 'total_rows': len(rows), 'lists': [item]}
        path = self.root / 'manifest.json'
        path.write_text(json.dumps(manifest), encoding='utf-8')
        return path

    def test_blank_registration_retained_but_excluded_and_identical_duplicates_collapsed(self):
        absent = {**candidate(2), 'jamb_reg_no': '', 'queue_status': 'missing_jamb_number'}
        dataset = load_dataset(self.fixture([candidate(), candidate(), absent]))
        self.assertEqual(len(dataset.candidates), 1)
        self.assertEqual(dataset.statistics['duplicate_rows'], 1)
        self.assertEqual(dataset.statistics['excluded_rows'], {'missing_jamb_number': 1})

    def test_changed_csv_fails_checksum_validation(self):
        manifest = self.fixture([candidate()])
        with (self.root / 'list.csv').open('a') as handle:
            handle.write('corrupt\n')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            load_dataset(manifest)

    def test_conflicting_ready_duplicate_is_rejected(self):
        manifest = self.fixture([candidate(), {**candidate(), 'department': 'OTHER COURSE'}])
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            load_dataset(manifest)

    def test_path_traversal_is_rejected(self):
        path = self.fixture([candidate()])
        manifest = json.loads(path.read_text())
        manifest['lists'][0]['path'] = '../outside.csv'
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'outside'):
            load_dataset(path)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'progress.json'
        self.addCleanup(self.temp.cleanup)

    def dataset(self, count=2):
        return Dataset([candidate(i) for i in range(1, count + 1)], 'test-fingerprint', {})

    def test_slip_labels_do_not_match_lga_inside_olga_name(self):
        row = candidate()
        parsed = parse_labeled_slip(slip(row), row)
        self.assertEqual(parsed['fullname'], 'OLGA TEST PERSON')
        self.assertEqual(parsed['local_government'], 'TEST LGA')
        self.assertEqual(parsed['email'], 'test@example.invalid')
        self.assertEqual(parsed['course'], 'CROP SCIENCE')

    def test_mismatched_slip_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'requested JAMB'):
            parse_labeled_slip(slip(candidate(2)), candidate(1))

    def test_budget_checkpoint_resumes_at_next_record(self):
        dataset = self.dataset()
        progress = load_progress(self.path, dataset)
        client = FakeClient()
        rows = {row['jamb_reg_no']: row for row in dataset.candidates}
        scrape = Mock(side_effect=lambda reg: slip(rows[reg]))
        complete = process_queue(dataset, progress, client, 'portal_candidates', scrape, self.path, 10, clock=Mock(side_effect=[0, 11]))
        self.assertFalse(complete)
        restored = load_progress(self.path, dataset)
        self.assertEqual(restored['next_index'], 1)
        complete = process_queue(dataset, restored, client, 'portal_candidates', scrape, self.path, 100, clock=lambda: 0)
        self.assertTrue(complete)
        self.assertEqual(len(client.saved), 2)
        self.assertEqual(scrape.call_count, 2)

    def test_saved_records_are_skipped_on_fresh_run(self):
        dataset = self.dataset(1)
        client = FakeClient([parse_labeled_slip(slip(candidate()), candidate())])
        progress = load_progress(self.path, dataset)
        scrape = Mock()
        self.assertTrue(process_queue(dataset, progress, client, 'portal_candidates', scrape, self.path, 100, clock=lambda: 0))
        scrape.assert_not_called()
        self.assertEqual(progress['skipped_existing'], 1)
        self.assertEqual(client.table_calls, ['portal_candidate_sources'])

    def test_database_failure_does_not_advance_checkpoint(self):
        dataset = self.dataset(1)
        progress = load_progress(self.path, dataset)
        save_progress(self.path, progress)
        with self.assertRaisesRegex(RuntimeError, 'Save candidate by email failed'):
            process_queue(dataset, progress, FakeClient(fail_writes=True), 'portal_candidates', lambda _: slip(candidate()), self.path, 100, clock=lambda: 0)
        self.assertEqual(load_progress(self.path, dataset)['next_index'], 0)

    def test_repeated_portal_failure_stops_chain_with_durable_failure_report(self):
        dataset = self.dataset(6)
        progress = load_progress(self.path, dataset)
        scrape = Mock(return_value='')
        with self.assertRaisesRegex(RuntimeError, 'Five consecutive'):
            process_queue(dataset, progress, FakeClient(), 'portal_candidates', scrape, self.path, 100, clock=lambda: 0)
        self.assertEqual(scrape.call_count, 10)
        restored = load_progress(self.path, dataset)
        self.assertEqual(restored['next_index'], 5)
        self.assertEqual(restored['failed'], 5)
        self.assertEqual(len(restored['failures']), 5)

    def test_changed_manifest_cannot_silently_reuse_progress(self):
        dataset = self.dataset()
        save_progress(self.path, load_progress(self.path, dataset))
        with self.assertRaisesRegex(ValueError, 'changed'):
            load_progress(self.path, Dataset(dataset.candidates, 'different', {}))

    def test_no_progress_cannot_trigger_an_endless_chain(self):
        dataset = self.dataset()
        with self.assertRaisesRegex(RuntimeError, 'No queue progress'):
            process_queue(dataset, load_progress(self.path, dataset), FakeClient(), 'portal_candidates', Mock(), self.path, 0, clock=lambda: 1)

    def test_transient_network_retry_is_bounded(self):
        operation = Mock(side_effect=TimeoutError())
        sleep = Mock()
        with self.assertRaisesRegex(RuntimeError, 'TimeoutError'):
            retry('Test request', operation, sleep=sleep)
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(1.5 <= sleep.call_args_list[0].args[0] <= 2.5)
        self.assertTrue(3 <= sleep.call_args_list[1].args[0] <= 5)

    def test_save_sends_authoritative_csv_session_to_atomic_database_function(self):
        client = Mock()
        client.rpc.return_value.execute.return_value = SimpleNamespace(data={'action': 'updated', 'candidate_id': 10})
        parsed = parse_labeled_slip(slip(candidate()), candidate())
        self.assertEqual(save_candidate(client, parsed, '2025-2026'), 'updated')
        client.rpc.assert_called_once_with('save_portal_candidate', {'p_candidate': parsed, 'p_academic_session': '2025-2026'})

    def test_bad_database_response_is_not_accepted_as_saved(self):
        client = Mock()
        client.rpc.return_value.execute.return_value = SimpleNamespace(data={'action': 'unknown'})
        with self.assertRaisesRegex(RuntimeError, 'Invalid email-save'):
            save_candidate(client, {}, '2025-2026')

    def test_pre_email_progress_artifacts_can_still_resume(self):
        dataset = self.dataset()
        old = load_progress(self.path, dataset)
        for action in SAVE_ACTIONS:
            old.pop(action)
        old['next_index'] = 1
        save_progress(self.path, old)
        restored = load_progress(self.path, dataset)
        self.assertEqual(restored['next_index'], 1)
        self.assertTrue(all(restored[action] == 0 for action in SAVE_ACTIONS))

    def test_email_update_is_reported_and_source_progress_advances(self):
        dataset = self.dataset(1)
        progress = load_progress(self.path, dataset)
        client = FakeClient()
        client.rpc = Mock(return_value=SimpleNamespace(execute=lambda: SimpleNamespace(data={'action': 'updated', 'candidate_id': 10})))
        self.assertTrue(process_queue(dataset, progress, client, 'portal_candidates', lambda _: slip(candidate()), self.path, 100, clock=lambda: 0))
        self.assertEqual(progress['updated'], 1)
        self.assertEqual(progress['inserted'], 0)
        self.assertEqual(load_progress(self.path, dataset)['next_index'], 1)

    def test_portal_pacing_is_applied_before_each_attempt(self):
        dataset = self.dataset(1)
        progress = load_progress(self.path, dataset)
        pace = Mock()
        scrape = Mock(side_effect=['', slip(candidate())])
        self.assertTrue(process_queue(dataset, progress, FakeClient(), 'portal_candidates', scrape,
                                     self.path, 100, clock=lambda: 0, pace=pace))
        self.assertEqual(pace.call_count, 2)


if __name__ == '__main__':
    unittest.main()
