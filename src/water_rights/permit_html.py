"""Strict, script-free HTML extraction for permit announcements and details.

Only the Python standard library is used.  The source contract declares
correctly nested UTF-8 HTML; this parser builds a small element tree while
rejecting malformed nesting, duplicate attributes, ``script``/``base``/
``style`` elements and nested tables.  Entities are decoded by the parser,
nested text is concatenated and Unicode whitespace is folded.
"""
from html.parser import HTMLParser
import re

from .common import ident

LEAF_PERMIT = '许可证号'
LEAF_HOLDER = '持有人'
LEAF_DETAIL = '详情'
LEAF_TITLES = (LEAF_PERMIT, LEAF_HOLDER, LEAF_DETAIL)
MAX_HEADER_ROWS = 5
MAX_SPAN = 50

_VOID = frozenset((
    'area', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
    'param', 'source', 'track', 'wbr'))
_FORBIDDEN = frozenset(('script', 'base', 'style'))


class HtmlContractError(ValueError):
    pass


class _Node:
    __slots__ = ('tag', 'attrs', 'children')

    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = attrs
        self.children = []

    def get(self, name, default=None):
        return self.attrs.get(name, default)


def fold_text(text):
    """Decode already-unescaped text, fold Unicode whitespace runs, trim."""
    return re.sub(r'\s+', ' ', text).strip()


def _text_of(node):
    parts = []

    def walk(current):
        for child in current.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                walk(child)

    walk(node)
    return fold_text(''.join(parts))


def _iter_descendants(node):
    for child in node.children:
        if not isinstance(child, str):
            yield child
            yield from _iter_descendants(child)


def _find_all(node, tag):
    return [n for n in _iter_descendants(node) if n.tag == tag]


def _find_id(node, element_id):
    return [n for n in _iter_descendants(node)
            if n.attrs.get('id') == element_id]


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node(None, {})
        self.stack = [self.root]

    def _attributes(self, attrs):
        parsed = {}
        for name, value in attrs:
            if name in parsed:
                raise HtmlContractError('duplicate attribute: ' + name)
            parsed[name] = value if value is not None else ''
        return parsed

    def handle_starttag(self, tag, attrs):
        if tag in _FORBIDDEN:
            raise HtmlContractError('forbidden element: ' + tag)
        node = _Node(tag, self._attributes(attrs))
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        if tag in _FORBIDDEN:
            raise HtmlContractError('forbidden element: ' + tag)
        self.stack[-1].children.append(_Node(tag, self._attributes(attrs)))

    def handle_endtag(self, tag):
        if tag in _VOID:
            raise HtmlContractError('void element with end tag: ' + tag)
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                if depth != len(self.stack) - 1:
                    raise HtmlContractError('improperly nested element: ' + tag)
                self.stack.pop()
                return
        raise HtmlContractError('stray end tag: ' + tag)

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_document(raw):
    """Decode strict UTF-8 and build the element tree."""
    if not isinstance(raw, (bytes, bytearray)):
        raise HtmlContractError('HTML input must be raw bytes')
    try:
        text = bytes(raw).decode('utf-8', errors='strict')
    except UnicodeDecodeError as error:
        raise HtmlContractError('HTML is not strict UTF-8') from error
    builder = _TreeBuilder()
    try:
        builder.feed(text)
        builder.close()
    except HtmlContractError:
        raise
    except Exception as error:  # HTMLParser internal errors are contract violations
        raise HtmlContractError('malformed HTML: ' + str(error)) from error
    if len(builder.stack) != 1:
        raise HtmlContractError('unclosed element: ' + builder.stack[-1].tag)
    return builder.root


def _span(cell, attr, allow):
    if attr == 'colspan' and not allow and cell.get(attr) is not None:
        raise HtmlContractError('td must not declare colspan')
    raw = cell.get(attr)
    if raw is None:
        return 1
    if not re.fullmatch(r'[0-9]+', raw):
        raise HtmlContractError('span must be an integer: ' + attr)
    value = int(raw)
    if not 1 <= value <= MAX_SPAN:
        raise HtmlContractError('span out of range 1..50: ' + attr)
    return value


def _build_grid(physical_rows, allow_colspan):
    """Expand rowspan/colspan into a logical rectangle; reject holes/overlaps."""
    occupied = set()
    grid = {}
    width = 0
    for r, cells in enumerate(physical_rows):
        c = 0
        for cell in cells:
            while (r, c) in occupied:
                c += 1
            rs = _span(cell, 'rowspan', allow_colspan)
            cs = _span(cell, 'colspan', allow_colspan)
            for rr in range(r, r + rs):
                for cc in range(c, c + cs):
                    point = (rr, cc)
                    if point in occupied:
                        raise HtmlContractError('overlapping table cells')
                    occupied.add(point)
                    grid[point] = cell
                    width = max(width, cc + 1)
            c += cs
    height = len(physical_rows)
    for (r, c) in occupied:
        if r >= height:
            raise HtmlContractError('cell span runs past the table end')
    for r in range(height):
        for c in range(width):
            if (r, c) not in grid:
                raise HtmlContractError('ragged table has a hole')
    return grid, height, width


def _direct_children(node, tag):
    return [c for c in node.children if not isinstance(c, str) and c.tag == tag]


def _only_permit_table(root):
    tables = [n for n in _find_all(root, 'table') if n.get('id') == 'permits']
    if len(tables) != 1:
        raise HtmlContractError('page must contain exactly one table#permits')
    table = tables[0]
    if _find_all(table, 'table'):
        raise HtmlContractError('nested tables are forbidden')
    return table


def _header_grid(table):
    heads = [c for c in table.children
             if not isinstance(c, str) and c.tag == 'thead']
    bodies = [c for c in table.children
              if not isinstance(c, str) and c.tag == 'tbody']
    if len(heads) != 1 or len(bodies) != 1:
        raise HtmlContractError('table#permits needs one thead and one tbody')
    head, body = heads[0], bodies[0]
    if [c for c in table.children if not isinstance(c, str)
            and c.tag not in ('thead', 'tbody')]:
        raise HtmlContractError('unexpected section inside table#permits')
    header_rows = []
    for tr in _direct_children(head, 'tr'):
        cells = _direct_children(tr, 'th')
        if not cells or [c for c in tr.children
                         if not isinstance(c, str) and c.tag != 'th']:
            raise HtmlContractError('thead rows may only contain th')
        header_rows.append(cells)
    if not 1 <= len(header_rows) <= MAX_HEADER_ROWS:
        raise HtmlContractError('thead must contain 1..5 rows')
    grid, _, width = _build_grid(header_rows, allow_colspan=True)
    leaf_titles = []
    last_header_row = len(header_rows) - 1
    for c in range(width):
        bottom_row = max(r for r in range(len(header_rows)) if (r, c) in grid)
        leaf = grid[(bottom_row, c)]
        if bottom_row == last_header_row and _span(leaf, 'colspan', True) > 1:
            raise HtmlContractError('leaf header must not span required columns')
        leaf_titles.append(_text_of(leaf))
    if sorted(leaf_titles) != sorted(LEAF_TITLES) or len(set(leaf_titles)) != 3:
        raise HtmlContractError('leaf titles must be exactly the three columns')
    if width != 3:
        raise HtmlContractError('logical table width must be three columns')
    body_rows = _direct_children(body, 'tr')
    for tr in body_rows:
        cells = _direct_children(tr, 'td')
        if not cells or [c for c in tr.children
                         if not isinstance(c, str) and c.tag != 'td']:
            raise HtmlContractError('tbody rows may only contain td')
    return head, body, {title: c for c, title in enumerate(leaf_titles)}, body_rows


def _anchor_href(cell):
    anchors = [a for a in _iter_descendants(cell) if a.tag == 'a']
    links = [a for a in anchors if a.get('href') is not None]
    if len(links) != 1:
        raise HtmlContractError('detail cell must contain exactly one a[href]')
    href = links[0].get('href')
    if not href:
        raise HtmlContractError('detail link href is empty')
    return href


def parse_announcement(raw):
    """Return (next_href_or_None, logical rows) for one announcement page.

    Logical row numbers start at 1 for the first tbody row.  Values come from
    the restored logical rectangle, never from fixed physical columns.
    """
    root = parse_document(raw)
    next_links = _find_id(root, 'next')
    if len(next_links) > 1:
        raise HtmlContractError('at most one a#next per page')
    next_href = None
    if next_links:
        anchor = next_links[0]
        if anchor.tag != 'a':
            raise HtmlContractError('#next must be an anchor element')
        if anchor.get('href') is None:
            raise HtmlContractError('a#next must carry href')
        next_href = anchor.get('href')
    table = _only_permit_table(root)
    _, body, columns, body_rows = _header_grid(table)
    body_cell_rows = [_direct_children(tr, 'td') for tr in body_rows]
    data_grid, height, data_width = _build_grid(body_cell_rows, allow_colspan=False)
    if body_cell_rows and data_width != 3:
        raise HtmlContractError(
            'tbody logical width must match the three header columns')
    rows = []
    for r in range(height):
        permit = fold_text(_text_of(data_grid[(r, columns[LEAF_PERMIT])]))
        holder = fold_text(_text_of(data_grid[(r, columns[LEAF_HOLDER])]))
        detail_cell = data_grid[(r, columns[LEAF_DETAIL])]
        ident(permit)
        ident(holder)
        rows.append({
            'logical_row': r + 1,
            'permit_id': permit,
            'holder': holder,
            'detail_href': _anchor_href(detail_cell),
        })
    return next_href, rows


def parse_detail(raw):
    """Return the four declared fields plus the quota link and digest."""
    root = parse_document(raw)
    dls = [n for n in _find_all(root, 'dl') if n.get('id') == 'permit']
    if len(dls) != 1:
        raise HtmlContractError('detail page must contain exactly one dl#permit')
    dl = dls[0]
    if _find_all(dl, 'dl'):
        raise HtmlContractError('nested dl is forbidden')
    fields = {}
    dds = _find_all(dl, 'dd')
    if len(dds) != 4:
        raise HtmlContractError('dl#permit must contain exactly four dd values')
    for dd in dds:
        name = dd.get('data-field')
        if name not in ('permit_id', 'holder', 'outlet_id', 'annual_m3'):
            raise HtmlContractError('dd must carry one of the four data-field names')
        if name in fields:
            raise HtmlContractError('duplicate detail field: ' + name)
        fields[name] = fold_text(_text_of(dd))
    if set(fields) != {'permit_id', 'holder', 'outlet_id', 'annual_m3'}:
        raise HtmlContractError('dl#permit must declare the four fields')
    for name in ('permit_id', 'holder', 'outlet_id'):
        ident(fields[name])
    quota_links = _find_id(root, 'quota')
    if len(quota_links) != 1:
        raise HtmlContractError('detail page must contain exactly one a#quota')
    quota = quota_links[0]
    if quota.tag != 'a' or quota.get('href') is None:
        raise HtmlContractError('a#quota must carry href')
    digest = quota.get('data-sha256', '')
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise HtmlContractError('quota digest must be 64 lowercase hex chars')
    return {
        'permit_id': fields['permit_id'],
        'holder': fields['holder'],
        'outlet_id': fields['outlet_id'],
        'annual_text': fields['annual_m3'],
        'quota_href': quota.get('href'),
        'quota_sha256': digest,
    }
