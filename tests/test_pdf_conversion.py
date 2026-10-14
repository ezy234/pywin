import unittest
from tools.convert_admission_lists import page_rows


def word(text, x, y, block, line=0):
    return (x, y, x + len(text) * 5, y + 10, text, block, line, 0)


def header(y=10, split=False, course=400):
    labels = [('SN', 70), ('JAMB', 100), ('SURNAME' if split else 'FULLNAME', 200), ('COURSE', course)]
    if split:
        labels += [('FIRSTNAME', 280), ('MIDDLENAME', 350)]
    return [word(text, x, y, 0, i) for i, (text, x) in enumerate(labels)]


def row(number, y, department_x=350, include_serial=True, reg=True):
    words = [word('TEST PERSON', 200, y, number, 2), word('CROP SCIENCE', department_x, y, number, 3)]
    if include_serial:
        words.append(word(str(number), 70, y, number, 0))
    if reg:
        words.append(word(f'20250000{number:04d}AA', 100, y, number, 1))
    return words


class ConversionTests(unittest.TestCase):
    def test_centered_course_header_uses_data_cell_edge(self):
        rows, _, _ = page_rows(header() + row(1, 30) + row(2, 55))
        self.assertEqual(rows[0]['fullname'], 'TEST PERSON')
        self.assertEqual(rows[0]['department'], 'CROP SCIENCE')
        self.assertEqual(len(rows), 2)

    def test_continuation_rows_before_new_header_are_preserved(self):
        _, previous, _ = page_rows(header() + row(1, 30))
        words = row(2, 10) + header(65) + row(3, 90)
        rows, _, _ = page_rows(words, previous, 'AGRICULTURE')
        self.assertEqual([r['jamb_reg_no'] for r in rows], ['202500000002AA', '202500000003AA'])

    def test_missing_serial_does_not_drop_candidate(self):
        rows, _, _ = page_rows(header() + row(1, 30, include_serial=False) + row(2, 55))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['source_row'], '')

    def test_blank_registration_is_preserved_and_flagged(self):
        rows, _, _ = page_rows(header() + row(1, 30, reg=False) + row(2, 55))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['queue_status'], 'missing_jamb_number')

    def test_split_name_columns_keep_wrapped_firstname_in_order(self):
        words = header(split=True, course=450)
        words += [word('1', 70, 35, 1, 0), word('202500000001AA', 100, 35, 1, 1),
                  word('SURNAMEVALUE', 200, 35, 1, 2), word('FIRST', 280, 29, 1, 3),
                  word('CONTINUED', 280, 41, 1, 4), word('MIDDLE', 350, 35, 1, 5),
                  word('MUSIC', 450, 35, 1, 6)]
        rows, _, _ = page_rows(words)
        self.assertEqual(rows[0]['fullname'], 'SURNAMEVALUE FIRST CONTINUED MIDDLE')


if __name__ == '__main__':
    unittest.main()
