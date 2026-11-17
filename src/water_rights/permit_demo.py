"""Self-contained local HTTP server for the hand-made permit sample site.

Standard library only::

    python -m water_rights.permit_demo --root fixtures/permit_site --snapshot G1

It implements the source contract used by the collector:

* every 200/304 response carries ``X-Permit-Snapshot`` and a non-empty ETag;
* ``If-None-Match`` yields 304 for unchanged bytes;
* ``X-Expected-Snapshot`` is checked and 412 is returned on a mismatch;
* ``/permits/p1003.html`` performs one same-origin redirect;
* every request line and selected headers are appended to a request log.

The ``--foreign`` option starts a second server on the next port; it records
hits but must never receive one when the collector rejects a cross-origin
redirect.
"""
import argparse
import hashlib
import http.server
import threading
import urllib.parse
from pathlib import Path

CONTENT_TYPES = {'.html': 'text/html; charset=utf-8',
                 '.csv': 'text/csv; charset=utf-8'}
REDIRECTS = {'/permits/p1003.html': '/permits/p1003-real.html'}


class PermitHandler(http.server.BaseHTTPRequestHandler):
    server_version = 'PermitDemo/1.0'

    def log_message(self, fmt, *args):
        record = '%s %s' % (self.address_string(), fmt % args)
        self.server.request_log.append(record)

    def _send(self, status, headers=(), body=b''):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body and self.command == 'GET':
            self.wfile.write(body)

    def do_HEAD(self):
        self._serve(head_only=True)

    def do_GET(self):
        self._serve(head_only=False)

    def _serve(self, head_only):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        self.server.request_log.append(
            '%s %s %s expected=%s inm=%s' % (
                self.command, self.path, 'FOREIGN' if self.server.foreign else 'LOCAL',
                self.headers.get('X-Expected-Snapshot', ''),
                self.headers.get('If-None-Match', '')))
        expected = self.headers.get('X-Expected-Snapshot')
        if expected is not None and expected != self.server.snapshot:
            self._send(412, (('X-Permit-Snapshot', self.server.snapshot),
                            ('ETag', '"snapshot-%s"' % self.server.snapshot)))
            return
        if self.server.foreign:
            body = b'foreign origin: collector must never reach this server'
            digest = hashlib.sha256(body).hexdigest()
            etag = '"foreign-%s"' % digest[:16]
            self._send(200, (('X-Permit-Snapshot', self.server.snapshot),
                            ('ETag', etag),
                            ('Content-Type', 'text/plain; charset=utf-8')),
                       b'' if head_only else body)
            return
        if path in self.server.redirects:
            target = self.server.redirects[path]
            if parsed.query:
                target += '?' + parsed.query
            self._send(302, (('Location', target),
                            ('X-Permit-Snapshot', self.server.snapshot),
                            ('ETag', '"redirect-%s"' % target)))
            return
        relative = path.lstrip('/')
        file_path = (Path(self.server.root) / relative).resolve()
        root = Path(self.server.root).resolve()
        if file_path.is_dir():
            file_path = (file_path / 'index.html').resolve()
        if not str(file_path).startswith(str(root)) or not file_path.is_file():
            self._send(404, (('X-Permit-Snapshot', self.server.snapshot),
                            ('ETag', '"missing"')))
            return
        body = file_path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        etag = '"%s"' % digest
        if self.headers.get('If-None-Match') == etag:
            self._send(304, (('X-Permit-Snapshot', self.server.snapshot),
                            ('ETag', etag)))
            return
        suffix = file_path.suffix
        self._send(200, (
            ('X-Permit-Snapshot', self.server.snapshot),
            ('ETag', etag),
            ('Content-Type', CONTENT_TYPES.get(suffix, 'application/octet-stream')),
        ), b'' if head_only else body)


class DemoServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root, snapshot, foreign=False):
        super().__init__(address, PermitHandler)
        self.root = root
        self.snapshot = snapshot
        self.foreign = foreign
        self.request_log = []
        self.redirects = dict(REDIRECTS)


def serve(root, snapshot='G1', port=8080, foreign_port=None):
    server = DemoServer(('127.0.0.1', port), root, snapshot)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    foreign = None
    if foreign_port is not None:
        foreign = DemoServer(('127.0.0.1', foreign_port), root, snapshot,
                             foreign=True)
        threading.Thread(target=foreign.serve_forever, daemon=True).start()
    return server, foreign


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local permit source demo server')
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--snapshot', default='G1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--foreign-port', type=int, default=None)
    args = parser.parse_args(argv)
    server, foreign = serve(args.root, args.snapshot, args.port,
                            args.foreign_port)
    print('serving %s on http://127.0.0.1:%d/announce/ snapshot=%s'
          % (args.root, server.server_port, args.snapshot))
    if foreign:
        print('foreign origin on http://127.0.0.1:%d (must receive zero requests)'
              % foreign.server_port)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        if foreign:
            foreign.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
