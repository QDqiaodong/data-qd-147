"""RFC4180 quota CSV parsing with exact decimal-to-integer-litre conversion.

No floating point arithmetic is used anywhere.  Volumes may carry 0..3
fractional digits; the common unit is litres (1 m3 = 1000 L).
"""
import csv
import io
import re

from .permit_html import HtmlContractError

HEADER = ('outlet_id', 'month', 'licensed_m3')
MAX_VOLUME_M3 = 10 ** 9
_MONTH_RE = re.compile(r'(?:1[0-2]|[1-9])')
_DECIMAL_RE = re.compile(r'0|[1-9][0-9]*')
_FRACTION_RE = re.compile(r'[0-9]{1,3}')


def parse_decimal_litres(text):
    """Return the exact integer litre value of an m3 decimal text.

    The only legal texts are the single token ``0`` or a non-zero-leading
    integer optionally followed by 1..3 decimal digits.
    """
    if not isinstance(text, str) or not text:
        raise HtmlContractError('quota amount must be non-empty text')
    if text == '0':
        return 0
    if '.' in text:
        whole, fraction = text.split('.', 1)
        if not _FRACTION_RE.fullmatch(fraction):
            raise HtmlContractError('quota allows 1..3 decimal digits')
    else:
        whole, fraction = text, ''
    if not _DECIMAL_RE.fullmatch(whole) or whole == '0':
        raise HtmlContractError(
            'quota must be 0 or a non-zero-leading integer amount')
    litres = int(whole) * 1000
    if fraction:
        litres += int(fraction.ljust(3, '0'))
    if litres > MAX_VOLUME_M3 * 1000:
        raise HtmlContractError('quota exceeds 10^9 m3')
    return litres


def _parse_volume(text):
    return parse_decimal_litres(text)


def parse_quota_csv(raw, expected_outlet_id):
    """Validate one quota attachment against the detail's outlet identity.

    Returns ``{'outlet_id', 'annual_litres', 'months': [(month, litres, record_no)]}``.
    Record numbers start at 1 for the first logical record after the header;
    newlines inside quoted fields do not get separate numbers.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise HtmlContractError('CSV input must be raw bytes')
    data = bytes(raw)
    if data.startswith(b'\xef\xbb\xbf'):
        data = data[3:]
    try:
        text = data.decode('utf-8', errors='strict')
    except UnicodeDecodeError as error:
        raise HtmlContractError('CSV is not strict UTF-8') from error
    reader = csv.reader(io.StringIO(text), strict=True, skipinitialspace=False)
    try:
        records = list(reader)
    except csv.Error as error:
        raise HtmlContractError('CSV violates RFC4180: ' + str(error)) from error
    if not records:
        raise HtmlContractError('CSV must contain a header record')
    header = records[0]
    if tuple(header) != HEADER:
        raise HtmlContractError('CSV header must be exactly outlet_id,month,licensed_m3')
    months = {}
    outlet_ids = set()
    for record_no, record in enumerate(records[1:], start=1):
        if not record or all(field == '' for field in record):
            raise HtmlContractError('CSV contains an empty record')
        if tuple(record) == HEADER:
            raise HtmlContractError('CSV repeats the header')
        if len(record) != 3:
            raise HtmlContractError('CSV record %d has the wrong column count' % record_no)
        outlet_id, month_text, amount_text = record
        outlet_ids.add(outlet_id)
        if not _MONTH_RE.fullmatch(month_text):
            raise HtmlContractError('CSV record %d month must be 1..12 without leading zero'
                                    % record_no)
        month = int(month_text)
        if month in months:
            raise HtmlContractError('CSV record %d repeats month %d' % (record_no, month))
        months[month] = (_parse_volume(amount_text), record_no)
    if not months:
        raise HtmlContractError('CSV has no quota records')
    if outlet_ids != {expected_outlet_id}:
        raise HtmlContractError('CSV outlet_id must match the detail outlet')
    if set(months) != set(range(1, 13)):
        missing = sorted(set(range(1, 13)) - set(months))
        raise HtmlContractError('CSV is missing months: ' + ','.join(map(str, missing)))
    ordered = [(month, months[month][0], months[month][1]) for month in range(1, 13)]
    return {
        'outlet_id': expected_outlet_id,
        'annual_litres': sum(litres for _, litres, _ in ordered),
        'months': ordered,
    }
