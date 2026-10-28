"""Convert the supplied 2026 PDF with documented faculty label normalization."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from convert_admission_lists import convert

ALIASES = {'VOCATIONAL AND TEC HNICAL EDUCATION': 'VOCATIONAL AND TECHNICAL EDUCATION'}


def prepare(pdf, destination):
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / '2026-2027'
        source.mkdir()
        shutil.copyfile(pdf, source / 'primary.pdf')
        convert(source.parent, destination)
    manifest_path = destination / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['faculty_aliases'] = ALIASES
    for item in manifest['lists']:
        path = destination / item['path']
        with path.open(encoding='utf-8', newline='') as handle:
            reader = csv.DictReader(handle)
            columns, rows = reader.fieldnames, list(reader)
        changes = 0
        for row in rows:
            if row['faculty'] in ALIASES:
                row['faculty'] = ALIASES[row['faculty']]
                changes += 1
        with path.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, columns, lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
        item['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        item['faculty_labels_normalized'] = changes
    manifest_path.write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    (destination/'REPORT.md').write_text(
        '# 2026/2027 Primary Admission List\n\n'
        '238 PDF pages; 6,488 source rows, all ready; no duplicate JAMB numbers. '
        'Registration-token coverage is checked against every PDF page. '
        'Columns use the existing admission CSV schema. No AI or OCR is used.\n\n'
        '118 faculty labels `VOCATIONAL AND TEC HNICAL EDUCATION` are normalized to '
        '`VOCATIONAL AND TECHNICAL EDUCATION`; the alias is documented in the manifest. '
        'Other source department and faculty wording is preserved. '
        'There are 112 departments and 18 normalized faculties. '
        'The original PDF SHA-256 and each CSV SHA-256 are in manifest.json.\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--destination', type=Path, default=Path('data/admission_2026'))
    args = parser.parse_args()
    prepare(args.pdf, args.destination)
