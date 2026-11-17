"""HTTP fetch layer enforcing the permit source snapshot contract.

Only the standard library is used.  Redirects are handled manually so that
every hop can be checked against the entry origin *before* a request is
sent: same scheme, host (case-insensitive) and effective default port, and
no userinfo.  At most three redirect hops are allowed.
"""
import hashlib
import http.client
import socket
import urllib.error
import urllib.parse
import urllib.request

from .common import ident

TIMEOUT_SECONDS = 15
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_REDIRECT_HOPS = 3
REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))
SNAPSHOT_HEADER = 'X-Permit-Snapshot'
EXPECTED_HEADER = 'X-Expected-Snapshot'


class FetchError(ValueError):
    """Any transport, protocol or contract failure aborts the round."""


class SnapshotConflict(FetchError):
    """The source switched snapshots (HTTP 409/412 or mismatched header)."""


def _split(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https'):
        raise FetchError('entry must be a complete http/https URL: ' + url)
    if not parsed.hostname:
        raise FetchError('URL has no host: ' + url)
    if parsed.username is not None or parsed.password is not None:
        raise FetchError('userinfo is not allowed in URL: ' + url)
    return parsed


def origin(url):
    """Normalised (scheme, host, effective port) origin tuple."""
    parsed = _split(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == 'https' else 80
    return parsed.scheme, parsed.hostname.lower(), port


def defragment(url):
    """Remove the fragment; path and query string are kept untouched."""
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))


def resolve(base_url, href):
    """Resolve a relative link against the actual final response URL."""
    joined = urllib.parse.urljoin(base_url, href)
    resolved = defragment(joined)
    _split(resolved)
    return resolved


def _opaque(value, label):
    # ident gives non-empty, trimmed, <=128, no control characters; the source
    # contract additionally restricts snapshot/etag to printable ASCII.
    ident(value)
    if any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise FetchError(label + ' must be printable ASCII')
    return value


class Resource:
    __slots__ = ('request_url', 'final_url', 'status', 'snapshot', 'etag',
                 'sha256', 'kind', 'raw')

    def __init__(self, request_url, final_url, status, snapshot, etag,
                 sha256, kind, raw):
        self.request_url = request_url
        self.final_url = final_url
        self.status = status
        self.snapshot = snapshot
        self.etag = etag
        self.sha256 = sha256
        self.kind = kind
        self.raw = raw


class _ManualRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Signal the outer loop instead of following transparently.
        raise _Redirect(headers.get('Location'))


class _Redirect(Exception):
    def __init__(self, location):
        super().__init__('redirect')
        self.location = location


class HttpFetcher:
    """One fetcher serves one round; all requests expect one snapshot G."""

    def __init__(self, entry_url, timeout=TIMEOUT_SECONDS):
        self.entry_url = defragment(entry_url)
        _split(self.entry_url)
        self.entry_origin = origin(self.entry_url)
        self.timeout = timeout
        self.total_bytes = 0
        opener = urllib.request.OpenerDirector()
        opener.add_handler(urllib.request.HTTPSHandler())
        opener.add_handler(_ManualRedirectHandler())
        opener.add_handler(urllib.request.HTTPHandler())
        self._opener = opener

    def _check_origin(self, url):
        if origin(url) != self.entry_origin:
            raise FetchError('cross-origin redirect rejected: ' + url)

    def _read_limited(self, response):
        chunks = []
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise FetchError('response exceeds the 2 MB per-document limit')
            chunks.append(chunk)
        return b''.join(chunks)

    def _one_request(self, url, expected_snapshot, etag=None):
        headers = {'Accept-Encoding': 'identity'}
        if expected_snapshot is not None:
            headers[EXPECTED_HEADER] = expected_snapshot
        if etag is not None:
            headers['If-None-Match'] = etag
        request = urllib.request.Request(url, headers=headers, method='GET')
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except _Redirect:
            raise
        except urllib.error.HTTPError as error:
            if error.code in (409, 412):
                raise SnapshotConflict('source rejected the snapshot request: %d'
                                       % error.code) from error
            raise FetchError('HTTP error %d for %s' % (error.code, url)) from error
        except (urllib.error.URLError, socket.timeout, http.client.HTTPException,
                OSError) as error:
            raise FetchError('request failed for %s: %s' % (url, error)) from error
        with response:
            status = response.status
            if status in REDIRECT_STATUSES:
                raise _Redirect(response.headers.get('Location'))
            if status not in (200, 304):
                raise FetchError('unexpected HTTP status %d for %s' % (status, url))
            etag_value = response.headers.get('ETag')
            snapshot_value = response.headers.get(SNAPSHOT_HEADER)
            if not etag_value or not snapshot_value:
                raise FetchError('response missing ETag or snapshot header: ' + url)
            _opaque(etag_value, 'ETag')
            _opaque(snapshot_value, SNAPSHOT_HEADER)
            if expected_snapshot is not None and snapshot_value != expected_snapshot:
                raise SnapshotConflict(
                    'snapshot mismatch at %s: expected %s got %s'
                    % (url, expected_snapshot, snapshot_value))
            raw = b'' if status == 304 else self._read_limited(response)
        return status, etag_value, snapshot_value, raw

    def get(self, request_url, expected_snapshot=None, conditional_etag=None):
        """Fetch one URL, honouring redirect, snapshot and size contracts.

        ``expected_snapshot=None`` is only valid for the initial index
        revalidation probe; every later request must pin a concrete G.
        """
        url = defragment(request_url)
        self._check_origin(url)
        etag = conditional_etag
        hops = 0
        while True:
            try:
                status, etag_value, snapshot_value, raw = self._one_request(
                    url, expected_snapshot, etag)
            except _Redirect as turn:
                hops += 1
                if hops > MAX_REDIRECT_HOPS:
                    raise FetchError('more than three redirect hops: ' + request_url)
                if not turn.location:
                    raise FetchError('redirect without Location header')
                url = resolve(url, turn.location)
                # Origin check happens before the next request is sent.
                self._check_origin(url)
                etag = None
                continue
            return Resource(request_url, url, status, snapshot_value,
                            etag_value,
                            hashlib.sha256(raw).hexdigest() if raw else None,
                            None, raw)
