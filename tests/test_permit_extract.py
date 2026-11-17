"""Acceptance tests for strict HTML table restoration and RFC4180 quotas."""
import unittest

from water_rights.permit_csv import (parse_quota_csv, parse_decimal_litres)
from water_rights.permit_html import (HtmlContractError, parse_announcement,
                                      parse_detail)


def page(thead, body_rows, next_href=None):
    nav = '<nav>广告与站外导航</nav>'
    link = '<p><a id="next" href="%s">next</a></p>' % next_href if next_href else ''
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
            + nav + '<table id="permits"><thead>' + thead
            + '</thead><tbody>' + ''.join(body_rows)
            + '</tbody></table>' + link + '<table id="other"></table></body></html>'
            ).encode('utf-8')


HEADER_TWO_LEVEL = (
    '<tr><th rowspan="2">许可证号</th><th rowspan="2">持有人</th>'
    '<th colspan="1">许可详情</th></tr>'
    '<tr><th>详情</th></tr>')
HEADER_SIMPLE = '<tr><th>许可证号</th><th>持有人</th><th>详情</th></tr>'


class AnnouncementExtractionCase(unittest.TestCase):
    def test_two_level_colspan_rowspan_headers_and_data_rowspan(self):
        rows = [
            '<tr><td rowspan="2">P-1</td><td rowspan="2">甲&#160;方</td>'
            '<td><a href="/d1">看</a></td></tr>',
            '<tr><td><a href="/d2">看</a></td></tr>',
            '<tr><td>P-2</td><td>乙 方</td><td><a href="/d3">看</a></td></tr>',
        ]
        next_href, logical = parse_announcement(page(HEADER_TWO_LEVEL, rows))
        self.assertIsNone(next_href)
        self.assertEqual(
            [(r['logical_row'], r['permit_id'], r['holder'], r['detail_href'])
             for r in logical],
            [(1, 'P-1', '甲 方', '/d1'), (2, 'P-1', '甲 方', '/d2'),
             (3, 'P-2', '乙 方', '/d3')])
        # Values came from the restored column, not physical column index.
        self.assertEqual(logical[1]['permit_id'], 'P-1')

    def test_nested_text_entities_and_unicode_whitespace_fold(self):
        rows = ['<tr><td><b>P&#45;9</b></td><td>东　<i>河</i> 水厂</td>'
                '<td><a href="/d9">查\t看</a></td></tr>']
        _, logical = parse_announcement(page(HEADER_SIMPLE, rows))
        self.assertEqual(logical[0]['permit_id'], 'P-9')
        self.assertEqual(logical[0]['holder'], '东 河 水厂')

    def test_next_link_and_fragment_and_query_preserved(self):
        next_href, logical = parse_announcement(
            page(HEADER_SIMPLE, [], next_href='/p/2.html?x=1#frag'))
        self.assertEqual(next_href, '/p/2.html?x=1#frag')
        self.assertEqual(logical, [])

    def test_empty_table_is_a_valid_empty_dataset(self):
        _, logical = parse_announcement(page(HEADER_SIMPLE, []))
        self.assertEqual(logical, [])

    def test_reject_ambiguous_leaf_titles(self):
        bad = ('<tr><th>许可证号</th><th>持有人</th><th>持有人</th></tr>')
        with self.assertRaises(HtmlContractError):
            parse_announcement(page(bad, []))

    def test_reject_leaf_header_spanning_required_columns(self):
        bad = ('<tr><th>许可证号</th><th colspan="2">详情</th></tr>')
        with self.assertRaises(HtmlContractError):
            parse_announcement(page(bad, []))

    def test_reject_td_colspan(self):
        rows = ['<tr><td colspan="2">P-1</td><td><a href="/d">x</a></td></tr>']
        with self.assertRaises(HtmlContractError):
            parse_announcement(page(HEADER_SIMPLE, rows))

    def test_reject_hole_and_overlap(self):
        # rowspan=3 in a two-row table runs past the table end.
        rows = ['<tr><td rowspan="3">P-1</td><td>甲</td>'
                '<td><a href="/d">x</a></td></tr>',
                '<tr><td>乙</td><td><a href="/d2">x</a></td></tr>']
        with self.assertRaises(HtmlContractError):
            parse_announcement(page(HEADER_SIMPLE, rows))

    def test_reject_missing_detail_anchor(self):
        rows = ['<tr><td>P-1</td><td>甲</td><td>无链接</td></tr>']
        with self.assertRaises(HtmlContractError):
            parse_announcement(page(HEADER_SIMPLE, rows))

    def test_reject_two_detail_anchors(self):
        rows = ['<tr><td>P-1</td><td>甲</td>'
                '<td><a href="/d1">x</a><a href="/d2">y</a></td></tr>']
        with self.assertRaises(HtmlContractError):
            parse_announcement(page(HEADER_SIMPLE, rows))

    def test_reject_two_permit_tables(self):
        html = ('<table id="permits"><thead>%s</thead><tbody></tbody></table>'
                '<table id="permits"><thead>%s</thead><tbody></tbody></table>'
                % (HEADER_SIMPLE, HEADER_SIMPLE)).encode('utf-8')
        with self.assertRaises(HtmlContractError):
            parse_announcement(html)

    def test_reject_nested_table_and_script_and_base(self):
        nested = page(HEADER_SIMPLE,
                      ['<tr><td>P</td><td>h</td><td><a href="/d">x</a>'
                       '<table></table></td></tr>'])
        with self.assertRaises(HtmlContractError):
            parse_announcement(nested)
        with self.assertRaises(HtmlContractError):
            parse_announcement(b'<html><head><base href="/x/"></head></html>')
        with self.assertRaises(HtmlContractError):
            parse_announcement(b'<html><script>alert(1)</script></html>')

    def test_reject_non_utf8_and_bad_nesting(self):
        with self.assertRaises(HtmlContractError):
            parse_announcement('<html>许可'.encode('gbk'))
        with self.assertRaises(HtmlContractError):
            parse_announcement(
                b'<table id="permits"><thead>%s</thead><tbody>'
                b'<tr><td>P</td></tr></table>' % HEADER_SIMPLE.encode())


class DetailExtractionCase(unittest.TestCase):
    DETAIL = (
        '<html><body><dl id="permit">'
        '<dt>号</dt><dd data-field="permit_id">P-1</dd>'
        '<dt>人</dt><dd data-field="holder">甲 方</dd>'
        '<dt>口</dt><dd data-field="outlet_id">OUT-1</dd>'
        '<dt>年</dt><dd data-field="annual_m3">12.5</dd>'
        '</dl><p><a id="quota" href="/q.csv" data-sha256="%s">csv</a></p>'
        '</body></html>')

    def test_detail_fields_and_quota_link(self):
        digest = 'a' * 64
        detail = parse_detail((self.DETAIL % digest).encode('utf-8'))
        self.assertEqual(detail['permit_id'], 'P-1')
        self.assertEqual(detail['outlet_id'], 'OUT-1')
        self.assertEqual(detail['annual_text'], '12.5')
        self.assertEqual(detail['quota_href'], '/q.csv')
        self.assertEqual(detail['quota_sha256'], digest)

    def test_reject_bad_digest_and_missing_field(self):
        with self.assertRaises(HtmlContractError):
            parse_detail((self.DETAIL % 'A' * 64).encode('utf-8'))
        broken = self.DETAIL.replace(
            '<dd data-field="outlet_id">OUT-1</dd>', '')
        with self.assertRaises(HtmlContractError):
            parse_detail((broken % ('a' * 64)).encode('utf-8'))


class DecimalAndCsvCase(unittest.TestCase):
    def test_exact_decimal_to_litres_no_float(self):
        self.assertEqual(parse_decimal_litres('0'), 0)
        self.assertEqual(parse_decimal_litres('12000.5'), 12000500)
        self.assertEqual(parse_decimal_litres('360.250'), 360250)
        self.assertEqual(parse_decimal_litres('1000000000'), 10 ** 12)

    def test_reject_decimal_forms(self):
        for bad in ('00', '01', '1.', '.5', '1.1234', '-1', '1e3',
                    '1,000', '', ' 1', '1.0001', '0.0', '0.000'):
            with self.assertRaises(HtmlContractError, msg=bad):
                parse_decimal_litres(bad)

    def _csv(self, lines, bom=False):
        data = ('outlet_id,month,licensed_m3\n' + '\n'.join(lines) + '\n')
        raw = data.encode('utf-8')
        return b'\xef\xbb\xbf' + raw if bom else raw

    def _twelve(self, value='10'):
        return ['OUT-1,%d,%s' % (m, value) for m in range(1, 13)]

    def test_quoted_newline_does_not_add_a_record_number(self):
        # Count logical RFC4180 records directly: the quoted block with an
        # embedded newline must count as exactly one record.
        import csv
        import io
        raw = 'a,b\n1,"x\ny"\n2,z\n'
        records = list(csv.reader(io.StringIO(raw), strict=True))
        self.assertEqual(len(records), 3)
        self.assertEqual(records[1], ['1', 'x\ny'])
        lines = ['OUT-1,%d,10' % m for m in range(1, 6)]
        lines.append('OUT-1,6,"10\nnote"')
        lines += ['OUT-1,%d,10' % m for m in range(7, 13)]
        data = ('outlet_id,month,licensed_m3\n' + '\n'.join(lines)
                ).encode('utf-8')
        with self.assertRaises(HtmlContractError):
            parse_quota_csv(data, 'OUT-1')

    def test_bom_and_record_numbers_are_one_based_after_header(self):
        parsed = parse_quota_csv(self._csv(self._twelve(), bom=True), 'OUT-1')
        self.assertEqual(parsed['annual_litres'], 120000)
        self.assertEqual([r for _m, _l, r in parsed['months']],
                         list(range(1, 13)))
        self.assertEqual(parsed['months'][0], (1, 10000, 1))
        self.assertEqual(parsed['months'][-1], (12, 10000, 12))

    def test_missing_and_duplicate_month_rejected(self):
        lines = self._twelve()
        del lines[3]
        with self.assertRaises(HtmlContractError):
            parse_quota_csv(self._csv(lines), 'OUT-1')
        lines = self._twelve()
        lines[3] = 'OUT-1,5,10'
        with self.assertRaises(HtmlContractError):
            parse_quota_csv(self._csv(lines), 'OUT-1')

    def test_annual_conservation_with_fractions(self):
        lines = ['OUT-1,%d,%s' % (m, '30.025' if m < 12 else '30.225')
                 for m in range(1, 13)]
        parsed = parse_quota_csv(self._csv(lines), 'OUT-1')
        # 11 * 30025 + 30225 = 360500 millilitres-units of 0.001 m3
        self.assertEqual(parsed['annual_litres'], 360500)

    def test_reject_wrong_outlet_extra_column_repeated_header_empty_record(self):
        with self.assertRaises(HtmlContractError):
            parse_quota_csv(self._csv(['OUT-2,1,1'] + self._twelve()[1:]),
                            'OUT-1')
        with self.assertRaises(HtmlContractError):
            parse_quota_csv(self._csv(['OUT-1,1,1,9'] + self._twelve()[1:]),
                            'OUT-1')
        with self.assertRaises(HtmlContractError):
            parse_quota_csv(
                self._csv(self._twelve() +
                          ['outlet_id,month,licensed_m3']), 'OUT-1')
        with self.assertRaises(HtmlContractError):
            parse_quota_csv(self._csv(self._twelve() + ['']), 'OUT-1')

    def test_month_must_have_no_leading_zero(self):
        lines = self._twelve()
        lines[0] = 'OUT-1,01,10'
        with self.assertRaises(HtmlContractError):
            parse_quota_csv(self._csv(lines), 'OUT-1')


if __name__ == '__main__':
    unittest.main()
