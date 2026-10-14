"""Exercise resume behavior without a browser, portal, or database."""
import ast
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, mock_open, patch

SOURCE = Path(__file__).resolve().parents[1] / 'bot.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))


def function(name, namespace):
    node = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace[name]


class ResumeTests(unittest.TestCase):
    def setup_run(self, done=(), clock=(0, 1, 2), candidates=None):
        browser = Mock()
        playwright = Mock()
        playwright.chromium.launch.return_value = browser
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        fake_os = SimpleNamespace(environ={}, path=SimpleNamespace(exists=Mock(return_value=False)), remove=Mock())
        ns = dict(os=fake_os, io=io, time=SimpleNamespace(time=Mock(side_effect=clock)),
                  REQUIRED_SECRETS=[], CONTINUE_FILE='.continue', MAX_CANDIDATES=0,
                  RUN_BUDGET_MINUTES=45, SAMPLE_SIZE=3,
                  fetch_pdf_from_storage=Mock(return_value=('test-list', b'pdf')),
                  read_records=Mock(return_value=candidates or [{'reg': 'A'}, {'reg': 'B'}]),
                  fetch_done_regs=Mock(return_value=set(done)),
                  sync_playwright=Mock(return_value=manager), scrape_slip=Mock(return_value='synthetic slip'),
                  llm_learn_template=Mock(return_value=None), _process_and_save=Mock())
        return function('run_bot', ns), ns

    def test_completed_list_does_not_launch_browser_or_restart(self):
        run, ns = self.setup_run(done=('A', 'B'))
        with patch('builtins.open', mock_open()) as opened:
            run()
        ns['sync_playwright'].assert_not_called()
        opened.assert_not_called()

    def test_budget_flushes_buffer_before_requesting_restart(self):
        run, ns = self.setup_run(clock=(0, 1, 2701))
        with patch('builtins.open', mock_open()) as opened:
            run()
        self.assertEqual(ns['_process_and_save'].call_count, 1)
        self.assertEqual(ns['_process_and_save'].call_args.args[0]['reg'], 'A')
        opened.assert_called_once_with('.continue', 'w')
        opened().write.assert_called_once_with('more work remaining')

    def test_resume_skips_saved_records_and_finishes_without_restart(self):
        run, ns = self.setup_run(done=('A',), clock=(0, 1))
        with patch('builtins.open', mock_open()) as opened:
            run()
        self.assertEqual(ns['scrape_slip'].call_args.args[1], 'B')
        self.assertEqual(ns['_process_and_save'].call_count, 1)
        opened.assert_not_called()

    def test_model_default_and_override(self):
        node = next(n for n in TREE.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'GROQ_MODEL' for t in n.targets))
        code = compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec')
        for variables, expected in [({}, 'openai/gpt-oss-120b'), ({'GROQ_MODEL': 'test-model'}, 'test-model')]:
            namespace = {'os': SimpleNamespace(environ=variables)}
            exec(code, namespace)
            self.assertEqual(namespace['GROQ_MODEL'], expected)

    def test_pdf_uses_source_project(self):
        client = Mock()
        storage = client.storage.from_.return_value
        storage.list.return_value = [{'name': 'sample.pdf'}]
        storage.download.return_value = b'pdf'
        create = Mock(return_value=client)
        ns = dict(create_client=create, SUPABASE_SOURCE_URL='source-url',
                  SUPABASE_SOURCE_KEY='source-key', SUPABASE_BUCKET='source-bucket', os=os)
        self.assertEqual(function('fetch_pdf_from_storage', ns)(), ('sample', b'pdf'))
        create.assert_called_once_with('source-url', 'source-key')


if __name__ == '__main__':
    unittest.main()
