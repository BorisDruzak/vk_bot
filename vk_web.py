"""Small authenticated LAN control panel. The Windows VM owns all VK state/actions.

Initialize once with --init-auth --username operator (password via getpass).
Only explicitly listed RPC operations can cross the SSH boundary.
"""
from __future__ import annotations

import argparse
import base64
from collections import defaultdict, deque
import getpass
import hashlib
import hmac
import importlib.util
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit,parse_qs

ROOT = Path(__file__).resolve().parent
AUTH_FILE = ROOT / 'secrets_vk_web.json'  # covered by secrets*.json in .gitignore
BODY_LIMIT = 16_384
SESSION_SECONDS = 8 * 60 * 60
OPS = {'snapshot', 'posts', 'add_account', 'configure_account', 'login_start', 'login_status', 'login_check', 'login_finish', 'refresh_statistics'}


def _derive(password, salt):
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=16384, r=8, p=1, dklen=32)


def write_auth(path, username, password):
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', username):
        raise ValueError('invalid_username')
    if not 8 <= len(password) <= 1024:
        raise ValueError('password_requires_8_to_1024_characters')
    path = Path(path)
    salt = secrets.token_bytes(24)
    payload = {'username': username, 'salt': base64.b64encode(salt).decode(), 'hash': base64.b64encode(_derive(password, salt)).decode(), 'algorithm': 'scrypt-n16384-r8-p1'}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as file:
        json.dump(payload, file)
    if os.name == 'nt':
        # Current owner only; never loosen a pre-existing file's permissions.
        try:
            owner = subprocess.run(['whoami'], capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'{owner}:(F)'], capture_output=True, check=True)
        except (OSError, subprocess.SubprocessError):
            path.unlink(missing_ok=True)
            raise ValueError('auth_acl_failed') from None


def verify_password(credentials, username, password):
    if not isinstance(username, str) or not isinstance(password, str) or len(password) > 1024:
        return False
    try:
        actual = _derive(password, base64.b64decode(credentials['salt'], validate=True))
        expected = base64.b64decode(credentials['hash'], validate=True)
        return hmac.compare_digest(actual, expected) & hmac.compare_digest(username.encode(), credentials['username'].encode())
    except (KeyError, TypeError, ValueError):
        return False


class RemoteRPC:
    def __init__(self, host, remote_root, identity_file, python=None):
        remote_root = remote_root.replace('\\', '/')
        python = (python or remote_root + '/.venv/Scripts/python.exe').replace('\\', '/')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+', host):
            raise ValueError('invalid_remote_host')
        if any(not re.fullmatch(r'[A-Za-z]:/[A-Za-z0-9_ ./-]+', value) or '..' in value.split('/') for value in (remote_root, python)):
            raise ValueError('invalid_remote_path')
        command = f"powershell.exe -NoLogo -NoProfile -NonInteractive -Command \"& '{python}' '{remote_root}/vk_control.py' --rpc\""
        self.command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2', '-i', str(identity_file), host, command]

    def __call__(self, payload):
        try:
            result = subprocess.run(self.command, input=json.dumps(payload, ensure_ascii=True), text=True, encoding='utf-8', capture_output=True, timeout=120, shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if len(result.stdout) > 4_000_000:
                return {'error': 'backend_response_too_large'}
            if result.returncode and not result.stdout.strip():
                return {'error': 'backend_unavailable'}
            decoded = json.loads(result.stdout.lstrip('\ufeff'))
            if result.returncode and (not isinstance(decoded, dict) or 'error' not in decoded):
                return {'error': 'backend_unavailable'}
            return decoded if isinstance(decoded, dict) else {'error': 'backend_invalid_response'}
        except subprocess.TimeoutExpired:
            return {'error': 'backend_timeout_outcome_unknown'}
        except (OSError, UnicodeError, ValueError):
            return {'error': 'backend_invalid_response'}


def validate_payload(data):
    if not isinstance(data, dict) or not isinstance(data.get('operation'), str) or data['operation'] not in OPS:
        return False
    operation = data['operation']
    fields = {'operation'}
    if operation in {'posts', 'refresh_statistics'}:
        fields |= {'group_id'}
        if 'group_id' in data and (type(data['group_id']) is not int or not 1 <= data['group_id'] <= 2**53 - 1):
            return False
    if operation == 'posts':
        fields |= {'limit'}
        if 'limit' in data and (type(data['limit']) is not int or not 1 <= data['limit'] <= 100):
            return False
    if operation == 'add_account':
        fields |= {'name'}
        name = data.get('name')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(c in name for c in '<>\r\n\x00'):
            return False
    if operation in {'configure_account', 'login_start', 'login_status', 'login_check', 'login_finish'}:
        fields |= {'account'}
        if type(data.get('account')) is not int or not 1 <= data['account'] <= 10000:
            return False
    if operation == 'configure_account':
        fields |= {'name', 'enabled', 'likes', 'personal_reposts', 'community_reposts', 'communities'}
        if 'name' in data:
            name = data['name']
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c)<32 or c in '<>' for c in name):
                return False
        if 'enabled' in data and type(data['enabled']) is not bool:
            return False
        for key, limit in [('likes', 50), ('personal_reposts', 10), ('community_reposts', 20)]:
            if key in data and (type(data[key]) is not int or not 0 <= data[key] <= limit):
                return False
        if 'communities' in data:
            groups = data['communities']
            if not isinstance(groups, list) or len(groups) > 2 or len(set(map(str, groups))) != len(groups) or any(type(g) is not int or g <= 0 for g in groups):
                return False
    return not (set(data) - fields)


class LocalRPC:
    """Call the same fixed control boundary when the panel runs on its VM."""
    def __call__(self, payload):
        if not validate_payload(payload):
            return {'error':'invalid_operation_or_fields'}
        import vk_control
        source = StringIO(json.dumps(payload, ensure_ascii=True))
        destination = StringIO()
        vk_control.rpc(source=source, destination=destination)
        decoded = json.loads(destination.getvalue())
        return decoded if isinstance(decoded, dict) else {'error':'backend_invalid_response'}


def load_pos_reader(root):
    """Load the installed POS viewer at startup, never its executor or config."""
    root = Path(root).resolve()
    spec = importlib.util.spec_from_file_location('_vk_pos_queue_view', root / 'pos_queue.py')
    module = importlib.util.module_from_spec(spec)
    # pos_queue imports its sibling pos_core; restore the search path before
    # serving requests. These installed sources are not selected by HTTP input.
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(root))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module.ReadOnlyQueue(root / 'runtime' / 'queue.sqlite3').snapshot


def make_server(host, port, auth_file, rpc, allowed_hosts=None, *, pos_reader=None, pos_assets=None, analytics_reader=None):
    credentials = json.loads(Path(auth_file).read_text(encoding='utf-8'))
    if credentials.get('algorithm') != 'scrypt-n16384-r8-p1':
        raise ValueError('invalid_auth_file')
    valid_hosts = {'localhost', '127.0.0.1', socket.gethostname().lower()}
    if host != '0.0.0.0':
        valid_hosts.add(host.lower())
    try:
        valid_hosts.update(row[4][0].lower() for row in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    valid_hosts.update(value.lower() for value in (allowed_hosts or []))
    sessions, failures = {}, defaultdict(deque)
    state_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'VKUnified'
        sys_version = ''
        protocol_version = 'HTTP/1.0'

        def log_message(self, *_):
            pass  # Request bodies, private identifiers and login data never enter access logs.

        def reply(self, status, data, cookie=None, content_type='application/json; charset=utf-8', allow_frame=False):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'SAMEORIGIN' if allow_frame else 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            ancestors = "'self'" if allow_frame else "'none'"
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors " + ancestors + "; base-uri 'none'; form-action 'self'")
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(body)

        def guard(self, require_origin=False):
            host_header = self.headers.get('Host', '')
            # Exact local host/port allowlist, no forwarded headers or wildcard trust.
            expected_port = self.server.server_address[1]
            hosts = {f'{value}:{expected_port}' for value in valid_hosts}
            if expected_port == 80:
                hosts |= valid_hosts
            if host_header.lower() not in hosts:
                self.reply(403, {'error': 'invalid_host'})
                return False
            if require_origin and self.headers.get('Origin') != 'http://' + host_header:
                self.reply(403, {'error': 'invalid_origin'})
                return False
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.reply(403, {'error': 'cross_site_request'})
                return False
            return True

        def session(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
                token = cookie['vk_web'].value if 'vk_web' in cookie else ''
            except Exception:
                return None, None
            with state_lock:
                session = sessions.get(token)
                if session and session['expires'] > time.monotonic():
                    return token, session.copy()
                sessions.pop(token, None)
            return None, None

        def do_GET(self):
            if not self.guard():
                return
            if urlsplit(self.path).path == '/api/analytics':
                _, session = self.session()
                if not session:
                    self.reply(401, {'error':'login_required'})
                    return
                if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),session['csrf']):
                    self.reply(403, {'error':'invalid_csrf'})
                    return
                if self.headers.get('Origin') not in (None,'http://' + self.headers.get('Host','')):
                    self.reply(403, {'error':'invalid_origin'})
                    return
                try:
                    query=parse_qs(urlsplit(self.path).query,strict_parsing=True,max_num_fields=3,keep_blank_values=True)
                    if set(query)-{'run','weeks','reposts'} or any(len(v)!=1 for v in query.values()):raise ValueError()
                    rid=query.get('run',[None])[0];weeks=query.get('weeks',['calendar'])[0];reposts=query.get('reposts',['1'])[0]
                    if (rid is not None and not re.fullmatch('[0-9a-f]{32}',rid)) or weeks not in ('calendar','period') or reposts not in ('0','1'):raise ValueError()
                except ValueError:
                    self.reply(400, {'error':'invalid_analytics_query'})
                    return
                try:
                    if analytics_reader is None:raise OSError()
                    self.reply(200,analytics_reader(run_id=rid,week_mode=weeks,include_reposts=reposts=='1'))
                except Exception:
                    self.reply(503, {'error':'analytics_unavailable'})
                return
            if self.path.startswith('/pos/'):
                _, session = self.session()
                if not session:
                    self.reply(401, {'error':'login_required'})
                    return
                if self.path == '/pos/api/session':
                    self.reply(200, {'authenticated':True, 'username':credentials['username'], 'csrf':session['csrf']})
                    return
                if self.path == '/pos/api/snapshot':
                    if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
                        self.reply(403, {'error':'invalid_csrf'})
                        return
                    if self.headers.get('Origin') not in (None, 'http://' + self.headers.get('Host', '')):
                        self.reply(403, {'error':'invalid_origin'})
                        return
                    try:
                        if pos_reader is None:
                            raise ValueError('pos_unavailable')
                        self.reply(200, pos_reader())
                    except Exception:
                        self.reply(503, {'error':'pos_queue_unavailable'})
                    return
                assets = {'/pos/':('index.html', 'text/html; charset=utf-8'),
                          '/pos/app.js':('app.js', 'text/javascript; charset=utf-8'),
                          '/pos/style.css':('style.css', 'text/css; charset=utf-8'),
                          '/pos/favicon.svg':('favicon.svg', 'image/svg+xml')}
                if self.path not in assets:
                    self.reply(404, {'error':'not_found'})
                    return
                name, mime = assets[self.path]
                try:
                    body = (Path(pos_assets) / name).read_bytes() if pos_assets is not None else None
                    if body is None:
                        raise OSError('pos_assets_unavailable')
                    self.reply(200, body, content_type=mime, allow_frame=self.path == '/pos/')
                except OSError:
                    self.reply(503, {'error':'asset_unavailable'})
                return
            if self.path == '/api/session':
                _, session = self.session()
                self.reply(200, {'authenticated':True, 'username':credentials['username'], 'csrf':session['csrf']} if session else {'authenticated':False})
                return
            if self.path == '/api/connection.rdp':
                _, session = self.session()
                if not session:
                    self.reply(401, {'error':'login_required'})
                    return
                self.reply(200, b'full address:s:192.168.100.46\r\nprompt for credentials:i:1\r\nauthentication level:i:2\r\nscreen mode id:i:2\r\n', content_type='application/x-rdp')
                return
            assets = {'/':('index.html', 'text/html; charset=utf-8'), '/app.js':('app.js', 'text/javascript; charset=utf-8'), '/analytics.js':('analytics.js', 'text/javascript; charset=utf-8'), '/style.css':('style.css', 'text/css; charset=utf-8'), '/favicon.svg':('favicon.svg', 'image/svg+xml')}
            if self.path not in assets:
                self.reply(404, {'error':'not_found'})
                return
            name, mime = assets[self.path]
            try:
                self.reply(200, (ROOT / 'web' / name).read_bytes(), content_type=mime)
            except OSError:
                self.reply(503, {'error':'asset_unavailable'})

        def do_POST(self):
            if self.headers.get('Transfer-Encoding'):
                self.reply(415, {'error':'json_required'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                self.reply(400, {'error':'invalid_length'})
                return
            if length <= 0:
                self.reply(413, {'error':'request_too_large'})
                return
            self.connection.settimeout(10)
            if length > BODY_LIMIT:
                # A small rejected body is drained within a separate fixed cap.
                # Closing with unread bytes can reset the TCP stream on Windows
                # before the caller receives the HTTP rejection. Never drain an
                # unbounded attacker-controlled length.
                if length <= BODY_LIMIT * 4:
                    self.connection.settimeout(2)
                    try:
                        self.rfile.read(length)
                    except OSError:
                        pass
                self.reply(413, {'error':'request_too_large'})
                return
            try:
                raw = self.rfile.read(length)
            except OSError:
                self.reply(400, {'error':'invalid_body'})
                return
            if len(raw) != length:
                self.reply(400, {'error':'invalid_body'})
                return
            # Consume a bounded, framed body before any early 403/404/415 reply;
            # Host/Origin and authentication still precede parsing/dispatch.
            if not self.guard(require_origin=True):
                return
            if self.path not in {'/api/login', '/api/logout', '/api/rpc'}:
                self.reply(404, {'error':'not_found'})
                return
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self.reply(415, {'error':'json_required'})
                return
            try:
                data = json.loads(raw)
            except ValueError:
                self.reply(400, {'error':'invalid_json'})
                return
            if not isinstance(data, dict):
                self.reply(400, {'error':'invalid_json'})
                return
            if self.path == '/api/login':
                now, address = time.monotonic(), self.client_address[0]
                with state_lock:
                    attempts = failures[address]
                    while attempts and attempts[0] <= now - 600:
                        attempts.popleft()
                    if len(attempts) >= 5:
                        self.reply(429, {'error':'login_throttled'})
                        return
                    attempts.append(now)
                if not verify_password(credentials, data.get('username'), data.get('password')):
                    self.reply(401, {'error':'invalid_credentials'})
                    return
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                with state_lock:
                    failures.pop(address, None)
                    expired = [key for key, value in sessions.items() if value['expires'] <= now]
                    for key in expired:
                        sessions.pop(key, None)
                    if len(sessions) >= 128:
                        sessions.pop(next(iter(sessions)))
                    sessions[token] = {'csrf':csrf, 'expires':now + SESSION_SECONDS}
                self.reply(200, {'csrf':csrf, 'username':credentials['username']}, f'vk_web={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}')
                return
            token, session = self.session()
            if not session:
                self.reply(401, {'error':'login_required'})
                return
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
                self.reply(403, {'error':'invalid_csrf'})
                return
            if self.path == '/api/logout':
                with state_lock:
                    sessions.pop(token, None)
                self.reply(200, {'ok':True}, 'vk_web=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
                return
            if not validate_payload(data):
                self.reply(400, {'error':'invalid_operation_or_fields'})
                return
            try:
                result = rpc(data)
                if not isinstance(result, dict):
                    result = {'error':'backend_invalid_response'}
                # Error responses have only fixed identifiers, never raw stderr or exceptions.
                if 'error' in result:
                    code = result['error']
                    result = {'error':code if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,100}', code) else 'backend_error'}
                self.reply(502 if 'error' in result else 200, result)
            except Exception:
                self.reply(502, {'error':'backend_unavailable'})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--allowed-host', action='append', default=[])
    parser.add_argument('--remote-host', default='vk-bot@192.168.100.46')
    parser.add_argument('--remote-root', default='C:/Users/vk-bot/vk_bot')
    parser.add_argument('--remote-python')
    parser.add_argument('--local-backend', action='store_true', help='Run the fixed vk_control.rpc on the same Windows VM, without SSH')
    parser.add_argument('--pos-root', type=Path, default=ROOT.parent / 'pos_automation', help='Installed read-only POS queue and assets')
    parser.add_argument('--identity-file', default=str(Path.home() / '.ssh' / 'id_rsa_vk_bot'))
    parser.add_argument('--auth-file', type=Path, default=AUTH_FILE)
    parser.add_argument('--init-auth', action='store_true')
    parser.add_argument('--username', default='operator')
    args = parser.parse_args()
    if args.init_auth:
        password = getpass.getpass('Пароль панели (минимум 8 символов): ')
        if password != getpass.getpass('Повторите пароль: '):
            parser.error('passwords_do_not_match')
        try:
            write_auth(args.auth_file, args.username, password)
        except (ValueError, FileExistsError) as error:
            parser.error(str(error) if isinstance(error, ValueError) else 'auth_file_already_exists')
        print('Учётная запись создана. Файл содержит только соль и хеш пароля.')
        return
    if not args.auth_file.is_file():
        parser.error('Run --init-auth --username operator first')
    backend = LocalRPC() if args.local_backend else RemoteRPC(args.remote_host, args.remote_root, args.identity_file, args.remote_python)
    try:
        pos_reader = load_pos_reader(args.pos_root) if args.local_backend else None
    except Exception:
        pos_reader = None  # POS absence must not prevent the VK panel starting.
    from vk_analytics_web import read_analytics
    analytics_reader = (lambda **kwargs:read_analytics(ROOT,**kwargs)) if args.local_backend else None
    server = make_server(args.host, args.port, args.auth_file, backend, args.allowed_host,
                         pos_reader=pos_reader, pos_assets=args.pos_root / 'pos_web',analytics_reader=analytics_reader)
    print(f'VK Unified HTTP panel listening on {args.host}:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
