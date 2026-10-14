"""Convert local admission PDFs using text coordinates only; never calls an AI."""
import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

FIELDS = ['jamb_reg_no', 'fullname', 'department', 'faculty', 'academic_session',
          'list_name', 'source_pdf', 'source_page', 'source_row', 'queue_status']
REG = re.compile(r'\d{8,14}[A-Z]{1,3}\Z')


def clean(text):
    return re.sub(r'\s+', ' ', text).strip()


def lines(words):
    result = []
    for word in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        center = (word[1] + word[3]) / 2
        if not result or abs(center - result[-1][0]) > 2.3:
            result.append((center, [word]))
        else:
            result[-1][1].append(word)
    return [(y, sorted(ws, key=lambda w: w[0])) for y, ws in result]


def join_words(words):
    return clean(' '.join(w[4] for _, ws in lines(words) for w in ws))


def headers(words):
    """Locate schemas again whenever a table restarts or changes columns."""
    result = []
    for word in words:
        if word[4].upper().rstrip('.') not in {'JAMB', 'REG'}:
            continue
        center = (word[1] + word[3]) / 2
        nearby = [w for w in words if abs((w[1] + w[3]) / 2 - center) <= 13]
        names = [w for w in nearby if w[4].upper() in {'FULLNAME', 'SURNAME', 'FULL', 'NAME', 'NAMES'} and w[0] > word[0]]
        courses = [w for w in nearby if w[4].upper() in {'COURSE', 'DEPARTMENT', 'PROGRAMME'} and w[0] > word[0]]
        if not names or not courses:
            continue
        name = min(names, key=lambda w: w[0])
        course = max(courses, key=lambda w: w[0])
        faculty = [w for w in nearby if w[4].upper() == 'FACULTY' and name[0] < w[0] < course[0]]
        year = [w for w in nearby if w[4].upper() == 'YEAR' and w[0] > course[0]]
        name_columns = sorted(w[0] - 6 for w in nearby if w[4].upper() in {'FIRSTNAME', 'MIDDLENAME', 'MIDDLE'} and name[0] < w[0] < course[0]) if name[4].upper() == 'SURNAME' else []
        serial = [w for w in nearby if re.fullmatch(r'S/?N[O0]?\.?', w[4], re.I)]
        labels = {'JAMB', 'REG', 'NO', 'NUMBER', 'FULLNAME', 'SURNAME', 'FIRSTNAME', 'MIDDLENAME', 'MIDDLE', 'FULL', 'NAME', 'NAMES', 'FACULTY', 'COURSE', 'ADMITTED', 'DEPARTMENT', 'YEAR', 'OF', 'PLACEMENT', 'CANDIDATE'}
        header_words = [w for w in nearby if w[4].upper().rstrip('.') in labels]
        schema = {'y': center, 'top': min(w[1] for w in header_words),
                  'bottom': max(w[3] for w in header_words),
                  'reg_x': word[0], 'name_x': name[0], 'course_x': course[0] - 6,
                  'faculty_x': faculty[0][0] - 6 if faculty else None,
                  'end_x': year[0][0] - 6 if year else float('inf'),
                  'serial_x': min((w[0] for w in serial), default=word[0] - 25), 'name_columns': name_columns}
        if not any(abs(h['y'] - center) < 15 for h in result):
            result.append(schema)
    return sorted(result, key=lambda h: h['y'])


def page_rows(words, previous_schema=None, previous_faculty=''):
    raw_lines = defaultdict(list)
    for word in words:
        raw_lines[(word[5], word[6])].append(word)
    discarded = set()
    for key, ws in raw_lines.items():
        text = clean(' '.join(w[4] for w in ws)).upper()
        if re.match(r'^(?:UNIVERSITY OF NIGERIA|OFFICE OF THE REGISTRAR|20\d{2}\s*/\s*20\d{2})', text) or re.fullmatch(r'(?:\(ADMISSIONS\)|(?:ADMISSION\s+)?LIST(?:\s+[IVX]+)?|JUPEB ADMISSION)', text):
            discarded.add(key)
    words = [w for w in words if (w[5], w[6]) not in discarded]
    schemas = headers(words)
    first_reg_y = min(((w[1] + w[3]) / 2 for w in words if REG.fullmatch(w[4].upper())), default=float('inf'))
    if schemas and previous_schema and first_reg_y < schemas[0]['top']:
        schemas.insert(0, {**previous_schema, 'y': 0, 'top': 0, 'bottom': 0})
    if not schemas and previous_schema:
        schemas = [{**previous_schema, 'y': 0, 'top': 0, 'bottom': 0}]
    if not schemas:
        raise ValueError('No recognizable table headers')
    sections = []
    physical_lines = lines(words)
    for line_index, (y, ws) in enumerate(physical_lines):
        text = join_words(ws)
        if re.match(r'FACULTY\s+OF\b', text, re.I) and ws[0][0] < schemas[0]['name_x'] - 10:
            faculty = re.sub(r'^FACULTY\s+OF\s+(?:THE\s+)?', '', text, flags=re.I).upper()
            if line_index + 1 < len(physical_lines):
                next_y, next_words = physical_lines[line_index + 1]
                continuation = join_words(next_words).upper()
                if next_y - y < 20 and continuation in {'EDUCATION', 'SCIENCES', 'STUDIES', 'TECHNOLOGY', 'AND TECHNOLOGY'}:
                    faculty += ' ' + continuation
            sections.append((y, faculty))
    result = []
    for index, schema in enumerate(schemas):
        bottom = schemas[index + 1]['top'] if index + 1 < len(schemas) else float('inf')
        content = [w for w in words if schema['bottom'] < (w[1] + w[3]) / 2 < bottom]
        # Header labels can be centered over left-aligned data (notably COURSE).
        # PDF text lines retain each cell's actual left edge, even without borders.
        cell_lines = defaultdict(list)
        for word in content:
            cell_lines[(word[5], word[6])].append(word)
        origins = [min(ws, key=lambda w: w[0])[0] for ws in cell_lines.values()]
        previous_x = schema['faculty_x'] if schema['faculty_x'] is not None else max([schema['name_x']] + schema['name_columns'])
        course_origins = [x for x in origins if (previous_x + schema['course_x']) / 2 < x < min(schema['end_x'], schema['course_x'] + 55)]
        schema = dict(schema)
        if course_origins:
            schema['course_x'] = min(course_origins) - 2
        reg_words = [w for w in content if re.fullmatch(r'[0-9A-Za-z]{8,20}', w[4]) and sum(c.isdigit() for c in w[4]) >= 7]
        reg_left = min((w[0] for w in reg_words), default=schema['reg_x'])
        serials = [w for w in content if re.fullmatch(r'\d+\.?', w[4]) and w[0] < reg_left - 5 and abs(w[0] - schema['serial_x']) < 25]
        anchors = list(reg_words)
        for serial in serials:
            serial_center = (serial[1] + serial[3]) / 2
            if not any(abs((w[1] + w[3]) / 2 - serial_center) < 10 for w in anchors):
                anchors.append(serial)  # Preserve source rows with blank registration cells.
        anchors.sort(key=lambda w: (w[1] + w[3]) / 2)
        centers = [(w[1] + w[3]) / 2 for w in anchors]
        for row_index, (anchor, center) in enumerate(zip(anchors, centers)):
            first_margin = min(25, (centers[1] - center) / 2) if row_index == 0 and len(centers) > 1 else 25
            lo = (centers[row_index - 1] + center) / 2 if row_index else max(schema['bottom'], center - first_margin)
            hi = (center + centers[row_index + 1]) / 2 if row_index + 1 < len(centers) else center + 40
            preceding_sections = [(y, f) for y, f in sections if y < center]
            following_sections = [y for y, _ in sections if y > center]
            if preceding_sections:
                lo = max(lo, preceding_sections[-1][0] + 4)
            if following_sections:
                hi = min(hi, following_sections[0] - 4)
            band = [w for w in content if lo <= (w[1] + w[3]) / 2 < hi]
            row_regs = [w for w in reg_words if lo <= (w[1] + w[3]) / 2 < hi]
            if len(row_regs) > 1:
                raise ValueError('More than one registration number in a row')
            reg = row_regs[0] if row_regs else None
            # Infer the name's left edge from the data, since some headers are centered.
            name_left = reg[2] + 1 if reg else schema['reg_x'] + 45
            name_end = schema['faculty_x'] if schema['faculty_x'] is not None else schema['course_x']
            is_text = lambda w: not re.fullmatch(r'\d+\.?', w[4])
            name_words = [w for w in band if name_left <= w[0] < name_end and is_text(w)]
            department_words = [w for w in band if schema['course_x'] <= w[0] < schema['end_x'] and is_text(w)]
            faculty_words = [w for w in band if schema['faculty_x'] is not None and schema['faculty_x'] <= w[0] < schema['course_x'] and is_text(w)]
            if schema['name_columns']:
                boundaries = [name_left] + schema['name_columns'] + [name_end]
                name = clean(' '.join(join_words([w for w in name_words if left <= w[0] < right]) for left, right in zip(boundaries, boundaries[1:])))
            else:
                name = join_words(name_words)
            department = join_words(department_words).upper()
            faculty = join_words(faculty_words).upper() if faculty_words else preceding_sections[-1][1] if preceding_sections else previous_faculty
            if not name and not department:
                continue  # A page number outside the actual table.
            jamb = reg[4].upper() if reg else ''
            status = 'ready' if REG.fullmatch(jamb) else 'missing_jamb_number' if not jamb else 'invalid_jamb_number'
            if status == 'ready' and not name:
                status = 'missing_fullname'
            if status == 'ready' and not department:
                status = 'missing_department'
            nearest_serial = min(serials, key=lambda w: abs((w[1] + w[3]) / 2 - center), default=None)
            serial_text = nearest_serial[4].rstrip('.') if nearest_serial and abs((nearest_serial[1] + nearest_serial[3]) / 2 - center) < 10 else ''
            result.append({'jamb_reg_no': jamb, 'fullname': name, 'department': department,
                           'faculty': faculty, 'source_row': serial_text, 'queue_status': status})
    return result, schemas[-1], sections[-1][1] if sections else previous_faculty


def convert(source, destination):
    import fitz
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    index_path = source / 'sources.json'
    sources = json.loads(index_path.read_text(encoding='utf-8-sig')) if index_path.exists() else []
    urls = {(s['session'], s['filename']): s['url'] for s in sources}
    manifest = {'schema_version': 1, 'columns': FIELDS, 'lists': []}
    for path in sorted(source.rglob('*.pdf')):
        relative = path.relative_to(source)
        list_name = relative.with_suffix('').as_posix()
        rows, schema, faculty, expected_regs = [], None, '', Counter()
        with fitz.open(path) as document:
            opening = document[0].get_text()
            sessions = re.findall(r'\b(20\d{2})\s*/\s*(20\d{2})\b', opening)
            session = '-'.join(sessions[0]) if sessions else relative.parts[0]
            for number, page in enumerate(document, 1):
                words = page.get_text('words')
                expected_regs.update(w[4].upper() for w in words if REG.fullmatch(w[4].upper()))
                try:
                    parsed, schema, faculty = page_rows(words, schema, faculty)
                except ValueError as error:
                    raise ValueError(f'{relative}, page {number}: {error}') from error
                for row in parsed:
                    row.update(academic_session=session, list_name=list_name, source_pdf=relative.as_posix(), source_page=number)
                rows.extend(parsed)
            pages = len(document)
        actual_regs = Counter(row['jamb_reg_no'] for row in rows if REG.fullmatch(row['jamb_reg_no']))
        if expected_regs != actual_regs:
            missing = sum((expected_regs - actual_regs).values())
            extra = sum((actual_regs - expected_regs).values())
            raise ValueError(f'{relative}: registration coverage mismatch (missing={missing}, extra={extra})')
        grouped = defaultdict(list)
        for row in rows:
            if row['jamb_reg_no']:
                grouped[row['jamb_reg_no']].append(row)
        conflicts = 0
        for group in grouped.values():
            signatures = {(r['fullname'].upper(), r['department'], r['faculty']) for r in group}
            if len(signatures) > 1:
                conflicts += 1
                for row in group:
                    row['queue_status'] = 'conflicting_duplicate'
        output = destination / relative.with_suffix('.csv')
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, FIELDS, lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
        item = {'path': output.relative_to(destination).as_posix(), 'list_name': list_name,
                'academic_session': session, 'source_pdf': relative.as_posix(), 'source_url': urls.get((relative.parts[0], path.name), ''),
                'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'pages': pages,
                'rows': len(rows), 'unique_jamb_numbers': len(grouped), 'conflicting_jamb_numbers': conflicts,
                'status_counts': dict(Counter(r['queue_status'] for r in rows))}
        manifest['lists'].append(item)
        print(json.dumps({'file': relative.as_posix(), 'rows': len(rows), 'status_counts': item['status_counts']}), flush=True)
    manifest['total_rows'] = sum(item['rows'] for item in manifest['lists'])
    manifest['missing_downloads'] = [s for s in sources if not (source / s['session'] / s['filename']).is_file()]
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    write_report(destination)
    print(f"Converted {len(manifest['lists'])} PDFs, {manifest['total_rows']} rows.", flush=True)


def write_report(destination):
    destination = Path(destination)
    manifest = json.loads((destination / 'manifest.json').read_text(encoding='utf-8'))
    statuses, identities = Counter(), set()
    for item in manifest['lists']:
        statuses.update(item['status_counts'])
        with (destination / item['path']).open(encoding='utf-8', newline='') as handle:
            for row in csv.DictReader(handle):
                if row['queue_status'] == 'ready':
                    identities.add((row['list_name'], row['jamb_reg_no']))
    text = ['# Admission CSV conversion report', '',
            f"Converted **{len(manifest['lists'])} PDFs**, preserving **{manifest['total_rows']:,} source rows**.", '',
            f"The action queues **{len(identities):,} distinct (list, JAMB number) candidates**. "
            f"It collapses {statuses['ready'] - len(identities):,} identical ready-row duplicates.", '',
            'No AI or OCR was used. Conversion reads native PDF word coordinates, identifies table headers, '
            'and checks the count of every recognized registration token against the output. '
            'Faculty section headings are carried across pages; running titles are excluded. '
            'Source spelling and missing values are preserved rather than guessed.', '',
            '| Queue status | Source rows |', '| --- | ---: |']
    text += [f'| {status} | {count:,} |' for status, count in sorted(statuses.items())]
    text += ['', 'Only `ready` rows enter the portal queue. Conflicting duplicates are rows in the same PDF '
             'with the same JAMB number but different names, departments, or faculties. '
             'Those rows remain available in the CSV for review. Blank or malformed registration numbers '
             'and missing names are also retained and excluded. The Public Administration/Local Government '
             'PDF has blank registration cells, so its 16 rows cannot be submitted to the JAMB-based portal.', '',
             '| CSV | Source rows | Ready rows | Excluded rows |', '| --- | ---: | ---: | ---: |']
    for item in manifest['lists']:
        ready = item['status_counts'].get('ready', 0)
        text.append(f"| [{item['path']}]({item['path']}) | {item['rows']:,} | {ready:,} | {item['rows'] - ready:,} |")
    text += ['', 'The manifest records each CSV and PDF SHA-256, source URL, original PDF path, page count, '
             'session, and status counts. Each CSV row records its source page and printed serial number when present.', '',
             f"## Indexed PDFs absent from the downloaded folder ({len(manifest['missing_downloads'])})", '',
             'These files were present in the supplied source index but were not downloaded. They were not fabricated or converted.', '']
    text += [f"- [{item['session']}/{item['filename']}]({item['url']})" for item in manifest['missing_downloads']]
    (destination / 'REPORT.md').write_text('\n'.join(text) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    convert(args.source, args.destination)
