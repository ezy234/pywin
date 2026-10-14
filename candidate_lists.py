"""Strict, dependency-free reader for the checked-in admission CSV corpus."""
import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

COLUMNS = ['jamb_reg_no', 'fullname', 'department', 'faculty', 'academic_session',
           'list_name', 'source_pdf', 'source_page', 'source_row', 'queue_status']
STATUSES = {'ready', 'missing_jamb_number', 'invalid_jamb_number', 'missing_fullname',
            'missing_department', 'conflicting_duplicate'}
REG = re.compile(r'\d{8,14}[A-Z]{1,3}\Z')


@dataclass(frozen=True)
class Dataset:
    candidates: list
    fingerprint: str
    statistics: dict


def load_dataset(manifest_path='data/admission_lists/manifest.json', session='all'):
    manifest_path = Path(manifest_path).resolve()
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, ValueError) as error:
        raise ValueError(f'Cannot read admission manifest: {type(error).__name__}') from None
    if manifest.get('schema_version') != 1 or manifest.get('columns') != COLUMNS:
        raise ValueError('Unsupported admission manifest schema')
    lists = manifest.get('lists')
    if not isinstance(lists, list) or not lists:
        raise ValueError('Admission manifest has no PDF lists')
    if manifest.get('total_rows') != sum(item['rows'] for item in lists):
        raise ValueError('Manifest total row count is inconsistent')
    selected = [item for item in lists if session == 'all' or item['academic_session'] == session]
    if not selected:
        raise ValueError('No admission lists match the requested academic session')
    # Visit recent sessions first; keep filenames stable within a session.
    selected.sort(key=lambda item: item['path'])
    selected.sort(key=lambda item: item['academic_session'], reverse=True)
    candidates, seen = [], {}
    statistics = {'pdf_files': len(selected), 'source_rows': 0, 'excluded_rows': {}, 'duplicate_rows': 0}
    excluded = Counter()
    for item in selected:
        path = (manifest_path.parent / item['path']).resolve()
        if not path.is_relative_to(manifest_path.parent) or path.suffix != '.csv':
            raise ValueError('Manifest CSV path is outside the admission directory')
        try:
            content = path.read_bytes()
        except OSError as error:
            raise ValueError(f'Missing or unreadable CSV: {item["path"]} ({type(error).__name__})') from None
        if hashlib.sha256(content).hexdigest() != item['sha256']:
            raise ValueError(f'CSV checksum mismatch: {item["path"]}')
        reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig'), newline=''))
        if reader.fieldnames != COLUMNS:
            raise ValueError(f'Unexpected CSV labels: {item["path"]}')
        counts, count = Counter(), 0
        for row in reader:
            count += 1
            location = f'{item["path"]}, CSV row {count + 1}'
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f'Malformed CSV row: {location}')
            if any(row[key] != str(item[key]) for key in ['list_name', 'academic_session', 'source_pdf']):
                raise ValueError(f'CSV provenance differs from its manifest: {location}')
            if not row['source_page'].isdigit() or not 1 <= int(row['source_page']) <= item['pages']:
                raise ValueError(f'Invalid source PDF page: {location}')
            status = row['queue_status']
            if status not in STATUSES:
                raise ValueError(f'Unknown queue status: {location}')
            counts[status] += 1
            if status != 'ready':
                excluded[status] += 1
                continue
            if not REG.fullmatch(row['jamb_reg_no']) or not row['fullname'].strip() or not row['department'].strip():
                raise ValueError(f'Incomplete row marked ready: {location}')
            key = (row['list_name'], row['jamb_reg_no'])
            signature = (row['fullname'].upper(), row['department'], row['faculty'])
            if key in seen:
                if seen[key] != signature:
                    raise ValueError(f'Conflicting duplicate marked ready: {location}')
                statistics['duplicate_rows'] += 1
                continue
            seen[key] = signature
            candidates.append(row)
        if count != item['rows'] or dict(counts) != item['status_counts']:
            raise ValueError(f'CSV row counts differ from its manifest: {item["path"]}')
        statistics['source_rows'] += count
    statistics['excluded_rows'] = dict(excluded)
    statistics['queued_candidates'] = len(candidates)
    fingerprint = hashlib.sha256(raw + b'\0' + session.encode()).hexdigest()
    return Dataset(candidates, fingerprint, statistics)
