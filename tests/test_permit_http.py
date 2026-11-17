"""Unit tests for URL/origin rules and the manual redirect contract."""
import unittest

from water_rights.permit_http import (FetchError, HttpFetcher, defragment,
                                      origin, resolve)
from tests.permit_site_helper import start


class UrlContractCase(unittest.TestCase):
    def test_default_port_and_host_case_origin_equivalence(self):
        self.assertEqual(origin('http://h.example:80/a'),
                         origin('http://H.EXAMPLE/a'))
        self.assertEqual(origin('https://h.example:443/'),
                         origin('https://h.example/'))
        self.assertNotEqual(origin('http://h.example:8080/'),
                            origin('http://h.example/'))

    def test_userinfo_and_scheme_are_rejected(self):
        for url in ('http://user:pass@h.example/a',
                    'http://h@h.example/a',
                    'ftp://h.example/a',
                    'not-a-url'):
            with self.assertRaises(FetchError, msg=url):
                origin(url)

    def test_fragment_removed_query_kept_no_redecoding(self):
        self.assertEqual(defragment('http://h/a?b=1#frag'),
                         'http://h/a?b=1')
        self.assertEqual(resolve('http://h/x/y.html', 'z.csv?q=1#f'),
                         'http://h/x/z.csv?q=1')

    def test_fetcher_rejects_bad_entry(self):
        with self.assertRaises(FetchError):
            HttpFetcher('ftp://h.example/a')


class RedirectCase(unittest.TestCase):
    def _server(self, redirects):
        from tests.permit_site_helper import announcement_html
        page = announcement_html([])
        server = start({'/index': page, '/final': page})
        server.redirects = redirects
        self.addCleanup(server.shutdown)
        return server

    def test_at_most_three_redirect_hops(self):
        server = self._server({
            '/index': '/r1', '/r1': '/r2', '/r2': '/r3',
            '/r3': '/r4', '/r4': '/final'})
        fetcher = HttpFetcher('http://127.0.0.1:%d/index' % server.server_port)
        with self.assertRaises(FetchError):
            fetcher.get(fetcher.entry_url)

    def test_three_hops_allowed(self):
        server = self._server({
            '/index': '/r1', '/r1': '/r2', '/r2': '/final'})
        fetcher = HttpFetcher('http://127.0.0.1:%d/index' % server.server_port)
        resource = fetcher.get(fetcher.entry_url)
        self.assertTrue(resource.final_url.endswith('/final'))
        self.assertTrue(resource.request_url.endswith('/index'))
        self.assertEqual(resource.status, 200)
        self.assertTrue(resource.etag and resource.snapshot)


if __name__ == '__main__':
    unittest.main()
