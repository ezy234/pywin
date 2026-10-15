"""Inspect one portal login in visible Chrome; never write to the database."""
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / 'state' / 'portal-diagnostic'


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    events = []

    def report(message, **details):
        events.append({'elapsed_seconds': round(time.monotonic() - started, 1), 'message': message, **details})
        (OUTPUT / 'report.json').write_text(json.dumps(events, indent=2), encoding='utf-8')
        print(message, flush=True)

    started = time.monotonic()
    report('Starting one-candidate diagnostic; no database writes.')
    reg = os.environ.get('DIAGNOSTIC_REG_NO', '').strip().upper()
    if not re.fullmatch(r'\d{8,14}[A-Z]{2}', reg):
        report('Invalid registration number format.')
        return 1
    required = ['PORTAL_URL', 'REG_INPUT_SELECTOR', 'LOGIN_BUTTON_SELECTOR', 'PRINT_BUTTON_SELECTOR']
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        report('Missing settings.', names=missing)
        return 1
    import bot
    from portal_status import CLOSED_MESSAGE
    import pyautogui
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=False)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        page = context.new_page()

        def capture(stage):
            # Desktop captures include native Chrome print preview; page captures
            # include the full document and every popup known to Playwright.
            try:
                pyautogui.screenshot().save(OUTPUT / f'{stage}-desktop.png')
            except Exception as error:
                report('Desktop screenshot unavailable.', error_type=type(error).__name__)
            for index, current in enumerate(context.pages):
                try:
                    current.screenshot(path=str(OUTPUT / f'{stage}-tab-{index}.png'), full_page=True, timeout=5000)
                except Exception as error:
                    report('Page screenshot unavailable.', tab=index, error_type=type(error).__name__)

        try:
            page.goto(os.environ['PORTAL_URL'], wait_until='domcontentloaded', timeout=180000)
            capture('01-login')
            page.locator(os.environ['REG_INPUT_SELECTOR']).fill(reg, timeout=180000)
            capture('02-registration-entered')
            page.locator(os.environ['LOGIN_BUTTON_SELECTOR']).click(timeout=180000)
            report('Login clicked; watching configured selector and exact Print PUTME Slip control.')
            configured = page.locator(os.environ['PRINT_BUTTON_SELECTOR'])
            exact = page.get_by_role('button', name='Print PUTME Slip', exact=True)
            deadline = time.monotonic() + 180
            button = None
            tick = 0
            while time.monotonic() < deadline:
                capture(f'03-dashboard-{tick:02d}')
                if page.get_by_text(CLOSED_MESSAGE).first.is_visible():
                    report('Portal reports application_closed; no retry or Print click needed.')
                    capture('03-application-closed')
                    return 0
                configured_visible = configured.count() == 1 and configured.is_visible()
                exact_visible = exact.count() == 1 and exact.is_visible()
                report('Dashboard readiness check.', configured_visible=configured_visible, exact_visible=exact_visible)
                if configured_visible or exact_visible:
                    button = exact if exact_visible else configured
                    break
                tick += 1
                page.wait_for_timeout(10000)
            if button is None:
                report('No print control detected within 180 seconds.')
                return 1
            # Save only control metadata, not form values or raw candidate text.
            controls = page.locator('button,input[type=submit],a').evaluate_all(
                "els => els.map(e => ({tag:e.tagName,id:e.id,text:(e.innerText||'').trim().slice(0,120)}))")
            report('Print control ready.', controls=controls)
            capture('04-before-print')
            button.click(timeout=180000)
            report('Print clicked.')
            matched = False
            for tick in range(18):
                page.wait_for_timeout(10000)
                capture(f'05-after-print-{tick:02d}')
                for current in context.pages:
                    try:
                        raw = current.locator('body').inner_text(timeout=3000)
                        if bot._looks_like_slip(raw) and bot._slip_matches_reg(raw, reg):
                            report('Matching slip detected in page text.', tab_count=len(context.pages))
                            matched = True
                            break
                    except Exception:
                        pass
                if matched:
                    break
            report('Testing production Ctrl+A / Ctrl+C clipboard capture.')
            bot.clear_clipboard()
            bot.pywinauto_copy_handler()
            raw = bot.get_clipboard_text() or ''
            clipboard_match = bot._looks_like_slip(raw) and bot._slip_matches_reg(raw, reg)
            report('Clipboard result.', character_count=len(raw), matching_slip=clipboard_match,
                   window_title_configured=bool(os.environ.get('WINDOW_TITLE_RE')))
            capture('06-after-clipboard')
            report('Diagnostic complete.', page_match=matched, clipboard_match=clipboard_match)
            return 0 if clipboard_match else 1
        except Exception as error:
            # Exception messages can contain submitted form values or URLs.
            report('Diagnostic failed.', error_type=type(error).__name__)
            capture('99-error')
            return 1
        finally:
            browser.close()


if __name__ == '__main__':
    raise SystemExit(main())
