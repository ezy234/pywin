"""Portal outcomes shared by the browser and CSV runner."""
import re
import time

CLOSED_MESSAGE = re.compile(r'this\s+application\s+is\s+currently\s+closed', re.I)


class ApplicationClosed(str):
    # Empty text remains compatible with legacy callers that skip empty slips.
    reason = 'application_closed'

    def __new__(cls):
        return super().__new__(cls, '')


def wait_for_dashboard(page, print_button, timeout_ms, clock=time.monotonic):
    closed = page.get_by_text(CLOSED_MESSAGE).first
    deadline = clock() + timeout_ms / 1000
    while True:
        if closed.is_visible():
            return 'application_closed'
        if print_button.is_visible():
            return 'ready'
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError('Dashboard readiness timeout')
        page.wait_for_timeout(min(500, remaining * 1000))
