"""In-memory programmable source site for collector scheduling tests.

Standard library ThreadingHTTPServer only.  Each path maps to a body built
for the currently selected snapshot; the test harness can switch snapshots,
mutate bodies, inject redirects (same- or cross-origin) and inspect the full
request log including conditional headers.
"""
import hashlib
import http.server
import threading
import urllib.parse

from water_rights.permit_demo import DemoServer


def quota_csv(outlet_id, monthly):
    lines = ['outlet_id,month,licensed_m3']
    for month in range(1, 13):
        lines.append('%s,%d,%s' % (outlet_id, month, monthly[month]))
    return ('\n'.join(lines) + '\n').encode('utf-8')


def detail_html(permit_id, holder, outlet_id, annual, quota_path, digest):
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
        '<dl id="permit">'
        '<dd data-field="permit_id">%s</dd>'
        '<dd data-field="holder">%s</dd>'
        '<dd data-field="outlet_id">%s</dd>'
        '<dd data-field="annual_m3">%s</dd></dl>'
        '<a id="quota" href="%s" data-sha256="%s">q</a>'
        '</body></html>' % (permit_id, holder, outlet_id, annual,
                            quota_path, digest)).encode('utf-8')


def announcement_html(rows, next_href=None):
    head = ('<table id="permits"><thead><tr>'
            '<th>许可证号</th><th>持有人</th><th>详情</th></tr></thead><tbody>')
    body = ''.join(
        '<tr><td>%s</td><td>%s</td><td><a href="%s">d</a></td></tr>'
        % (pid, holder, href) for pid, holder, href in rows)
    tail = '</tbody></table>'
    if next_href:
        tail += '<a id="next" href="%s">n</a>' % next_href
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
            + head + body + tail + '</body></html>').encode('utf-8')


def make_site(spec):
    """Build snapshot bodies from a simple declarative spec.

    spec = {'snapshots': {G: {'/path': bytes, ...}, ...}, 'index': path,
            'redirects': {path: target_url}}
    """
    bodies = {}
    for snapshot, paths in spec['snapshots'].items():
        for path, raw in paths.items():
            bodies[(snapshot, path)] = raw
    return bodies


class ScriptedHandler(http.server.BaseHTTPRequestHandler):
    server_version = 'ScriptedPermit/1.0'

    def log_message(self, fmt, *args):
        self.server.log.append(fmt % args)

    def _write(self, status, headers=(), body=b''):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body and self.command == 'GET':
            self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        entry = {
            'method': 'GET', 'path': self.path,
            'expected': self.headers.get('X-Expected-Snapshot'),
            'if_none_match': self.headers.get('If-None-Match'),
        }
        self.server.requests.append(entry)
        snapshot = self.server.snapshot
        etags = self.server.etags
        # Forced statuses (e.g. simulate 409/412/500 on a given path).
        if path in self.server.force_status:
            status = self.server.force_status[path]
            self._write(status, (('X-Permit-Snapshot', snapshot),
                                 ('ETag', etags.get((snapshot, path),
                                                    '"forced"'))))
            return
        if path in self.server.redirects:
            target = self.server.redirects[path]
            self._write(302, (('Location', target),
                             ('X-Permit-Snapshot', snapshot),
                             ('ETag', '"redirect"')))
            return
        body = self.server.bodies.get((snapshot, path))
        if body is None:
            self._write(404, (('X-Permit-Snapshot', snapshot),
                             ('ETag', '"missing"')))
            return
        # ETags are scoped to (snapshot, path): the same URL in a new
        # snapshot always has a different ETag, so stale conditional
        # requests cannot be answered 304.
        etag = self.server.etags.setdefault(
            (snapshot, path),
            '"%s-%s"' % (snapshot, hashlib.sha256(body).hexdigest()[:16]))
        expected = self.headers.get('X-Expected-Snapshot')
        if expected is not None and expected != snapshot:
            self._write(412, (('X-Permit-Snapshot', snapshot), ('ETag', etag)))
            return
        if self.headers.get('If-None-Match') == etag:
            self._write(304, (('X-Permit-Snapshot', snapshot), ('ETag', etag)))
            return
        self._write(200, (('X-Permit-Snapshot', snapshot), ('ETag', etag),
                         ('Content-Type', 'text/html; charset=utf-8')), body)


class ScriptedServer(DemoServer):
    def __init__(self, address, bodies, snapshot):
        http.server.ThreadingHTTPServer.__init__(self, address, ScriptedHandler)
        self.bodies = dict(bodies)
        self.snapshot = snapshot
        self.foreign = False
        self.root = ''
        self.request_log = []
        self.log = []
        self.requests = []
        self.etags = {}
        self.redirects = {}
        self.force_status = {}

    def shutdown(self):
        super().shutdown()
        self.server_close()


def start(bodies, snapshot='G1'):
    wrapped = {(snapshot, path): raw for path, raw in bodies.items()}
    server = ScriptedServer(('127.0.0.1', 0), wrapped, snapshot)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def origin_of(server):
    return 'http://127.0.0.1:%d' % server.server_port
