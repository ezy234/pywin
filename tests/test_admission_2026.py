import importlib.util
from pathlib import Path
import unittest
from candidate_lists import load_dataset

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dashboard', ROOT/'tools/admission_dashboard.py')
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class Admission2026Tests(unittest.TestCase):
    def test_new_manifest_is_complete_and_independent(self):
        import json
        manifest = json.loads((ROOT/'data/admission_2026/manifest.json').read_text())
        self.assertEqual(manifest['total_rows'], 6488)
        self.assertEqual(manifest['lists'][0]['status_counts'], {'ready': 6488})
        if not (ROOT/'data/admission_2026/2026-2027/primary.csv').exists():
            return  # Private candidate CSV is validated locally, not published.
        data = load_dataset(ROOT/'data/admission_2026/manifest.json')
        self.assertEqual(len(data.candidates), 6488)
        self.assertEqual(len({r['jamb_reg_no'] for r in data.candidates}), 6488)
        self.assertEqual({r['academic_session'] for r in data.candidates}, {'2026-2027'})
        self.assertEqual(len({r['faculty'] for r in data.candidates}), 18)
        self.assertEqual(len({r['department'] for r in data.candidates}), 112)

    def test_vcard_escaping_and_utf8_folding(self):
        value = dashboard.vcard([{'fullname': 'É'*100+'; A,B\nC', 'course':'TEST', 'faculty':'FAC',
            'email':'test@example.invalid', 'mobile_phone':'+234123456789'}])
        self.assertTrue(all(len(line)<=75 for line in value.split(b'\r\n')))
        unfolded = value.decode().replace('\r\n ', '')
        self.assertIn(r'\; A\,B\nC', unfolded)
        self.assertIn('TEL;TYPE=CELL:+234123456789', unfolded)
        self.assertEqual(unfolded.count('BEGIN:VCARD'), 1)
