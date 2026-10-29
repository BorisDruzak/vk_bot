import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import vk_web as web


class POSTabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        auth = folder / 'auth.json'
        web.write_auth(auth, 'fixture', 'fictional-password-123')
        self.assets = folder / 'assets'
        self.assets.mkdir()
        (self.assets / 'index.html').write_text('<h1>POS fixture</h1>')
        (self.assets / 'app.js').write_text('/* fixture */')
        self.reads = 0
        self.rpc_calls = []
        self.failure = False
        def reader():
            self.reads += 1
            if self.failure:
                raise OSError('private/path/should-not-leak')
            return {'status': 'complete', 'total_verified': 15}
        self.server = web.make_server('127.0.0.1', 0, auth, lambda p: self.rpc_calls.append(p),
            pos_reader=reader, pos_assets=self.assets)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.port = self.server.server_address[1]
        self.cookie = self.csrf = ''

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=4)
        current = {'Origin': f'http://127.0.0.1:{self.port}', 'Content-Type': 'application/json',
                   'Cookie': self.cookie, 'X-CSRF-Token': self.csrf}
        current.update(headers or {})
        connection.request(method, path, None if data is None else json.dumps(data), current)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def login(self):
        status, headers, body = self.request('POST', '/api/login', {'username': 'fixture', 'password': 'fictional-password-123'})
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.csrf = json.loads(body)['csrf']

    def test_same_session_protects_assets_and_read_only_snapshot(self):
        for path in ('/pos/', '/pos/app.js', '/pos/api/session', '/pos/api/snapshot'):
            self.assertEqual(self.request('GET', path)[0], 401)
        self.assertEqual(self.reads, 0)
        self.login()
        self.assertEqual(self.request('GET', '/pos/')[0], 200)
        status, _, body = self.request('GET', '/pos/api/session')
        self.assertEqual((status, json.loads(body)['csrf']), (200, self.csrf))
        self.assertEqual(self.request('GET', '/pos/api/snapshot', headers={'X-CSRF-Token': 'bad'})[0], 403)
        self.assertEqual(self.reads, 0)
        self.assertEqual(json.loads(self.request('GET', '/pos/api/snapshot')[2])['total_verified'], 15)
        self.assertEqual(self.rpc_calls, [])
        self.assertEqual(self.request('POST', '/pos/api/run', {})[0], 404)
        self.request('POST', '/api/logout', {})
        self.assertEqual(self.request('GET', '/pos/api/snapshot')[0], 401)

    def test_only_embedded_html_can_be_framed_by_same_origin(self):
        self.login()
        _, headers, _ = self.request('GET', '/pos/')
        self.assertEqual(headers['X-Frame-Options'], 'SAMEORIGIN')
        self.assertIn("frame-ancestors 'self'", headers['Content-Security-Policy'])
        _, headers, _ = self.request('GET', '/')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertIn("frame-src 'self'", headers['Content-Security-Policy'])
        for path in ('/pos/../runtime/actors.json', '/pos/cases.json', '/pos/api/run'):
            self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(self.request('GET', '/pos/api/snapshot', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_failure_is_explicit_and_sanitized(self):
        self.login()
        self.failure = True
        status, _, body = self.request('GET', '/pos/api/snapshot')
        self.assertEqual((status, json.loads(body)), (503, {'error': 'pos_queue_unavailable'}))
