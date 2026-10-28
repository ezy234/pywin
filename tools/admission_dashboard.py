"""Serve the dedicated local dashboard; credentials stay on this PC."""
import csv
import io
import json
import os
from pathlib import Path
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CONFIG = ROOT / 'state' / 'admission-dashboard-config.json'
TOKEN = secrets.token_urlsafe(32)
ORIGIN = 'http://127.0.0.1:8876'


def api(path, method='GET', data=None, github=False):
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    key = config['github_pat'] if github else config['supabase_key']
    base = 'https://api.github.com/repos/ezy234/pywin' if github else config['supabase_url'] + '/rest/v1'
    headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'User-Agent': 'admission-dashboard'}
    if not github:
        headers.update(apikey=key, Prefer='return=representation')
    request = urllib.request.Request(base + path, headers=headers, method=method,
        data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else None


def all_rows(table, query=''):
    rows = []
    while True:
        batch = api(f'/{table}?select=*&order=id&limit=1000&offset={len(rows)}' + query)
        rows.extend(batch)
        if len(batch) < 1000:
            return rows


def vescape(value):
    return str(value or '').replace('\\', '\\\\').replace('\r', '').replace('\n', r'\n').replace(';', r'\;').replace(',', r'\,')


def vcard(rows):
    lines = []
    for row in rows:
        lines.extend(['BEGIN:VCARD', 'VERSION:3.0', 'FN:' + vescape(row['fullname']),
            'N:;' + vescape(row['fullname']) + ';;;', 'ORG:' + vescape('University of Nigeria') + ';' + vescape(row.get('faculty')),
            'TITLE:' + vescape(row.get('course'))])
        if row.get('email'):
            lines.append('EMAIL;TYPE=INTERNET:' + vescape(row['email']))
        if row.get('mobile_phone'):
            lines.append('TEL;TYPE=CELL:' + vescape(row['mobile_phone']))
        lines.append('END:VCARD')
    # RFC line folding, keeping each UTF-8 physical line at most 75 bytes.
    folded = []
    for line in lines:
        current = ''
        for char in line:
            if len((current + char).encode('utf-8')) > 75:
                folded.append(current)
                current = ' '
            current += char
        folded.append(current)
    return ('\r\n'.join(folded) + '\r\n').encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, mime='application/json'):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def valid(self):
        return self.headers.get('Host') == '127.0.0.1:8876' and self.headers.get('X-Dashboard-Token') == TOKEN

    def do_GET(self):
        if self.headers.get('Host') != '127.0.0.1:8876':
            return self.send(403, {'error': 'Local access only'})
        if self.path == '/':
            html = (ROOT / 'dashboard' / 'admission_2026.html').read_text(encoding='utf-8').replace('__LOCAL_TOKEN__', TOKEN)
            return self.send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
        if not self.valid():
            return self.send(403, {'error': 'Invalid local token'})
        try:
            if self.path == '/api/state':
                runs = api('/admission_2026_runs?select=*&order=created_at.desc&limit=20')
                queue = all_rows('admission_2026_queue', '&run_id=eq.' + runs[0]['id']) if runs else []
                # Do not return bulky nested source candidates; expose useful queue fields.
                for row in queue:
                    source = row.pop('candidate')
                    row.update(fullname=source['fullname'], department=source['department'], faculty=source['faculty'])
                self.send(200, {'runs': runs, 'queue': queue, 'people': all_rows('admission_2026_candidates')})
            else:
                self.send(404, {'error': 'Unknown path'})
        except Exception as error:
            self.send(502, {'error': 'Request failed: ' + type(error).__name__})

    def do_POST(self):
        if not self.valid() or self.headers.get('Origin') != ORIGIN:
            return self.send(403, {'error': 'Invalid local request'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length < 10000000:
                raise ValueError('Invalid request size')
            data = json.loads(self.rfile.read(length))
            if self.path == '/api/control':
                action = data['action']
                if action == 'start':
                    workers = int(data.get('workers', 12))
                    if not 1 <= workers <= 20:
                        raise ValueError('Worker count must be 1 to 20')
                    active = api('/admission_2026_runs?select=id&status=eq.running')
                    if active:
                        raise ValueError('A swarm is already running')
                    api('/actions/workflows/admission_2026_swarm.yml/dispatches', 'POST',
                        {'ref': 'main', 'inputs': {'workers': str(workers)}}, github=True)
                else:
                    import uuid
                    run = str(uuid.UUID(data['run_id']))
                    rows = api('/admission_2026_runs?select=*&id=eq.' + run)
                    if len(rows) != 1:
                        raise ValueError('Unknown run')
                    if action == 'pause':
                        api('/admission_2026_runs?id=eq.' + run + '&status=eq.running', 'PATCH', {'status': 'paused'})
                    elif action == 'resume':
                        from datetime import datetime, timezone
                        if datetime.now(timezone.utc) >= datetime.fromisoformat(rows[0]['deadline'].replace('Z', '+00:00')):
                            raise ValueError('24-hour deadline passed; create a new run to retry remaining candidates')
                        api('/admission_2026_runs?id=eq.' + run, 'PATCH', {'status': 'running'})
                        api('/actions/workflows/admission_2026_swarm.yml/dispatches', 'POST',
                            {'ref': 'main', 'inputs': {'run_id': run, 'workers': str(rows[0]['workers'])}}, github=True)
                    elif action == 'retry':
                        api('/admission_2026_queue?run_id=eq.' + run + '&status=eq.failed', 'PATCH',
                            {'status': 'pending', 'attempts': 0, 'reason': None, 'finished_at': None})
                    else:
                        raise ValueError('Unknown action')
                self.send(200, {'message': 'Request accepted. Refresh to check progress. Pause lets in-flight candidates finish.'})
            elif self.path == '/api/export':
                people = all_rows('admission_2026_candidates')
                rows = [r for r in people if (not data.get('faculty') or r.get('faculty') == data['faculty'])
                    and (not data.get('department') or r.get('course') == data['department'])]
                if data['format'] == 'vcf':
                    self.send(200, vcard(rows), 'text/vcard; charset=utf-8')
                elif data['format'] == 'csv':
                    fields = ['fullname','reg_no','email','mobile_phone','course','faculty','academic_session','gender','state_of_origin','local_government','address']
                    buffer = io.StringIO(newline='')
                    writer = csv.DictWriter(buffer, fields, extrasaction='ignore')
                    writer.writeheader()
                    for row in rows:
                        writer.writerow({k: ("'" + str(row.get(k)) if str(row.get(k) or '').startswith(('=', '+', '-', '@', '\t', '\r')) else row.get(k)) for k in fields})
                    self.send(200, buffer.getvalue().encode('utf-8-sig'), 'text/csv; charset=utf-8')
                else:
                    raise ValueError('Unknown export format')
            else:
                self.send(404, {'error': 'Unknown path'})
        except ValueError as error:
            self.send(400, {'error': str(error)})
        except Exception as error:
            self.send(502, {'error': 'Request failed: ' + type(error).__name__})


if __name__ == '__main__':
    if not CONFIG.exists():
        raise SystemExit('Missing ignored state/admission-dashboard-config.json; configure local credentials first.')
    server = ThreadingHTTPServer(('127.0.0.1', 8876), Handler)
    print('Admission dashboard: ' + ORIGIN, flush=True)
    webbrowser.open(ORIGIN)
    server.serve_forever()
