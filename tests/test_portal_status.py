import unittest
from unittest.mock import Mock
from portal_status import CLOSED_MESSAGE, wait_for_dashboard


class ReadinessTests(unittest.TestCase):
    def test_closed_toast_returns_immediately_without_print_button(self):
        page, button = Mock(), Mock()
        page.get_by_text.return_value.first.is_visible.return_value = True
        self.assertEqual(wait_for_dashboard(page, button, 180000), 'application_closed')
        button.is_visible.assert_not_called()
        page.wait_for_timeout.assert_not_called()
        self.assertIsNotNone(CLOSED_MESSAGE.search('This application is currently closed'))

    def test_ready_button_does_not_wait_out_timeout(self):
        page, button = Mock(), Mock()
        page.get_by_text.return_value.first.is_visible.return_value = False
        button.is_visible.return_value = True
        self.assertEqual(wait_for_dashboard(page, button, 180000), 'ready')
        page.wait_for_timeout.assert_not_called()

    def test_waits_for_later_toast_and_has_bounded_timeout(self):
        page, button = Mock(), Mock()
        page.get_by_text.return_value.first.is_visible.side_effect = [False, True]
        button.is_visible.return_value = False
        self.assertEqual(wait_for_dashboard(page, button, 180000), 'application_closed')
        page.wait_for_timeout.assert_called_once()
        page.get_by_text.return_value.first.is_visible.side_effect = None
        page.get_by_text.return_value.first.is_visible.return_value = False
        with self.assertRaises(TimeoutError):
            wait_for_dashboard(page, button, 1000, clock=Mock(side_effect=[0, 2]))
