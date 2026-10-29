"""Disposable processes/fake errors/local loopback; no VK, profiles or actor state."""
import errno
import io
import json
import socket
import ssl
import subprocess
import threading
import time
import unittest
import urllib.error
import tempfile
from pathlib import Path
from contextlib import closing
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch

import vk_api_read as api
import vk_diagnostics as diagnostics


class ReadDiagnosticsTests(unittest.TestCase):
    def callback(self,events):
        return lambda event,**kw:events.append(diagnostics.make_record(event,**kw))

    def test_timeout_captures_last_tls_phase_and_never_forwards_stderr_or_input(self):
        script="import sys,time,json;sys.stderr.write('VKREAD '+json.dumps(dict(version=1,phase='tls_handshake',state='started',tick_ns=time.monotonic_ns(),secret='PRIVATE'))+'\\n');sys.stderr.write('PRIVATE traceback https://private/?key=PRIVATE\\n');sys.stderr.flush();time.sleep(10)"
        events=[]
        with patch.object(api,'_TRANSPORT_SCRIPT',script),self.assertRaises(api.VkApiReadDeadline) as e:
            api.bounded_transport({'owner_id':-101,'access_token':'PRIVATE'},timeout=.5,diagnostic=self.callback(events))
        detail=e.exception.read_diagnostics
        self.assertEqual(detail['read_phase'],'tls_handshake')
        self.assertEqual(detail['read_error'],'helper_deadline')
        self.assertEqual(events[-1]['event'],'public_read_failed')
        self.assertEqual(events[-1]['source_group'],101)
        self.assertNotIn('PRIVATE',json.dumps(events)+json.dumps(detail)+str(e.exception))
        self.assertNotIn('https://',json.dumps(events))

    def test_timeout_before_boot_has_explicit_unknown_startup_phase(self):
        with patch.object(api,'_TRANSPORT_SCRIPT','import time;time.sleep(10)'),self.assertRaises(api.VkApiReadDeadline) as e:
            api.bounded_transport({},timeout=.3)
        self.assertEqual(e.exception.read_diagnostics['read_phase'],'helper_start')

    def test_import_timeout_is_separate_from_network_timeout(self):
        script="import sys,time,json;print('VKREAD '+json.dumps(dict(version=1,phase='helper_imports',state='started',tick_ns=time.monotonic_ns())),file=sys.stderr,flush=True);time.sleep(10)"
        with patch.object(api,'_TRANSPORT_SCRIPT',script),self.assertRaises(api.VkApiReadDeadline) as e:
            api.bounded_transport({},timeout=.5)
        self.assertEqual(e.exception.read_diagnostics['read_phase'],'helper_imports')

    def test_dns_access_certificate_and_timeout_classifications_keep_only_codes(self):
        import vk_read_trace as trace
        cases=[(urllib.error.URLError(socket.gaierror(11001,'PRIVATE')),'dns_error'),
               (PermissionError(errno.EACCES,'PRIVATE'),'access_denied'),
               (ConnectionRefusedError(errno.ECONNREFUSED,'PRIVATE'),'connection_refused'),
               (ssl.SSLCertVerificationError(1,'PRIVATE'),'tls_certificate_error'),
               (TimeoutError('PRIVATE'),'timeout')]
        cases.extend((OSError(code,'PRIVATE'),kind) for code,kind in [(10013,'access_denied'),(10060,'timeout'),(10051,'network_unreachable')])
        for error,kind in cases:
            with self.subTest(kind=kind):
                detail=trace.classify_error(error)
                self.assertEqual(detail['read_error'],kind)
                self.assertNotIn('PRIVATE',json.dumps(detail))

    def test_child_failure_and_rate_limit_preserve_original_exception_policy(self):
        for expression,expected,kind in [
            ("__import__('urllib.error',fromlist=['URLError']).URLError(__import__('socket').gaierror(11001,'PRIVATE'))",api.VkApiFailure,'dns_error'),
            ("a.VkApiLimited('PRIVATE',code=6)",api.VkApiLimited,'vk_rate_limited')]:
            script=f"import vk_api_read as a;a._default_transport=lambda p:(_ for _ in ()).throw({expression});a._transport_stdio()"
            events=[]
            with patch.object(api,'_TRANSPORT_SCRIPT',script),self.assertRaises(expected) as e:
                api.bounded_transport({'access_token':'PRIVATE'},diagnostic=self.callback(events))
            self.assertEqual(e.exception.read_diagnostics['read_error'],kind)
            if expected is api.VkApiLimited:self.assertEqual(e.exception.code,6)
            self.assertNotIn('PRIVATE',json.dumps(events))

    def test_callback_failure_does_not_change_success_or_retry(self):
        script="import vk_api_read as a;a._default_transport=lambda p:(200,{'response':{'count':0,'items':[]}});a._transport_stdio()"
        with patch.object(api,'_TRANSPORT_SCRIPT',script),patch.object(subprocess,'run',wraps=subprocess.run) as run:
            result=api.bounded_transport({},diagnostic=lambda *a,**k:(_ for _ in ()).throw(ValueError('PRIVATE')))
        self.assertEqual(result[0],200);self.assertEqual(run.call_count,1)

    def test_callbacks_never_delay_start_of_reserved_http_read(self):
        events=[];run=subprocess.run
        script="import vk_api_read as a;a._default_transport=lambda p:(200,{'response':{'items':[]}});a._transport_stdio()"
        def invoke(*a,**kw):
            self.assertEqual(events,[],'A diagnostic DB write must not delay HTTP after its deadline guard')
            return run(*a,**kw)
        with patch.object(api,'_TRANSPORT_SCRIPT',script),patch.object(subprocess,'run',side_effect=invoke):
            api.bounded_transport({},diagnostic=self.callback(events))

    def test_http_429_keeps_http_code_after_limit_translation(self):
        script="import vk_api_read as a;import urllib.error;a.urllib.request.urlopen=lambda *x,**k:(_ for _ in ()).throw(urllib.error.HTTPError('https://PRIVATE',429,'PRIVATE',{},None));a._transport_stdio()"
        events=[]
        with patch.object(api,'_TRANSPORT_SCRIPT',script),self.assertRaises(api.VkApiLimited) as e:
            api.bounded_transport({},diagnostic=self.callback(events))
        self.assertEqual(e.exception.read_diagnostics['http_status'],429)
        self.assertEqual(e.exception.read_diagnostics['read_error'],'http_rate_limited')
        self.assertEqual(events[-1]['http_status'],429)
        self.assertNotIn('PRIVATE',json.dumps(events))

    def test_body_timeout_retains_status_already_received(self):
        script="""import vk_api_read as a
class Response:
 def __enter__(self):return self
 def __exit__(self,*args):pass
 def getcode(self):return 200
 def read(self,n):raise TimeoutError('PRIVATE')
a.urllib.request.urlopen=lambda *x,**k:Response()
a._transport_stdio()
"""
        with patch.object(api,'_TRANSPORT_SCRIPT',script),self.assertRaises(api.VkApiTransientRead) as e:
            api.bounded_transport({})
        self.assertEqual(e.exception.read_diagnostics['http_status'],200)
        self.assertEqual(e.exception.read_diagnostics['read_phase'],'response_body')
        self.assertEqual(e.exception.read_diagnostics['read_error'],'timeout')

    def test_oversized_response_has_specific_diagnostic_reason(self):
        script="""import vk_api_read as a
class Response:
 def __enter__(self):return self
 def __exit__(self,*args):pass
 def getcode(self):return 200
 def read(self,n):return b'x'*8000001
a.urllib.request.urlopen=lambda *x,**k:Response()
a._transport_stdio()
"""
        with patch.object(api,'_TRANSPORT_SCRIPT',script),self.assertRaises(api.VkApiFailure) as e:
            api.bounded_transport({})
        self.assertEqual(e.exception.read_diagnostics['read_error'],'response_too_large')

    def test_vk_client_access_and_rate_errors_keep_read_correlation_and_guards(self):
        import vk_batch as engine
        for code,expected in [(5,api.VkApiAuthFailure),(7,api.VkApiAuthFailure),(6,api.VkApiLimited)]:
            script=f"import vk_api_read as a;a._default_transport=lambda p:(200,{{'error':{{'error_code':{code},'error_msg':'PRIVATE'}}}});a._transport_stdio()"
            with tempfile.TemporaryDirectory() as td,closing(engine.db_open(Path(td)/'read.sqlite3')) as db:
                client=api.VKReadClient(db,'PRIVATE',transport=api.bounded_transport,shared_path=None)
                with patch.object(api,'_TRANSPORT_SCRIPT',script),self.assertRaises(expected) as e:client.wall_get(101)
                self.assertEqual(e.exception.code,code)
                self.assertEqual(len(e.exception.read_diagnostics['read_id']),32)
                self.assertEqual(e.exception.read_diagnostics['vk_error_code'],code)
                self.assertEqual(e.exception.read_diagnostics['read_error'],'vk_rate_limited' if code==6 else 'vk_access_denied')
                self.assertEqual(db.execute('SELECT count(*) FROM api_read_attempts').fetchone()[0],1)
                if code==6:
                    with self.assertRaises(engine.StopRun):engine.require_platform_unlocked(db)

    def test_result_preserves_two_value_protocol_without_payload_metadata_keys(self):
        payload={'response':{'items':[]}}
        script=f"import vk_api_read as a;a._default_transport=lambda p:(200,{payload!r});a._transport_stdio()"
        with patch.object(api,'_TRANSPORT_SCRIPT',script):result=api.bounded_transport({})
        self.assertEqual(len(result),2);self.assertEqual(result,(200,payload))
        status,body=result;self.assertEqual(status,200);self.assertEqual(body,payload)
        self.assertEqual(set(body),{'response'})

    def test_bootstrap_import_timing_and_original_stdout_protocol(self):
        script=api._TRANSPORT_SCRIPT.replace('import vk_api_read;',"import vk_api_read;vk_api_read._default_transport=lambda p:(200,{'response':{'items':[]}});")
        events=[]
        with patch.object(api,'_TRANSPORT_SCRIPT',script):api.bounded_transport({},diagnostic=self.callback(events))
        self.assertIn('helper_imports',events[-1]['read_timings_ms'])

    def test_http_and_vk_access_codes_are_visible_without_response_body(self):
        script="import vk_api_read as a;a._default_transport=lambda p:(200,{'error':{'error_code':5,'error_msg':'PRIVATE'}});a._transport_stdio()"
        events=[]
        with patch.object(api,'_TRANSPORT_SCRIPT',script):
            result=api.bounded_transport({},diagnostic=self.callback(events))
        self.assertEqual(result[1]['error']['error_code'],5)
        self.assertEqual(events[-1]['vk_error_code'],5)
        self.assertNotIn('PRIVATE',json.dumps(events))

    def test_loopback_read_has_dns_tcp_headers_body_and_json_timings(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length',0)))
                body=b'{"response":{"count":0,"items":[]}}'
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*a):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        script=f"import vk_api_read as a;a.API_URL='http://localhost:{server.server_port}/';a._transport_stdio()"
        events=[]
        with patch.object(api,'_TRANSPORT_SCRIPT',script):
            result=api.bounded_transport({'access_token':'PRIVATE'},diagnostic=self.callback(events))
        self.assertEqual(result[0],200)
        phases=events[-1]['read_timings_ms']
        self.assertTrue({'dns','tcp_connect','response_headers','response_body','json_parse'}<=set(phases))
        self.assertEqual(events[-1]['http_status'],200)
        self.assertNotIn('PRIVATE',json.dumps(events))

    def test_network_observers_restore_original_functions_after_failure(self):
        import vk_read_trace as trace
        original=socket.getaddrinfo
        observed=trace.ReadTrace(io.StringIO())
        with self.assertRaises(ValueError):
            with observed.observe_network():
                self.assertIsNot(socket.getaddrinfo,original)
                raise ValueError('PRIVATE')
        self.assertIs(socket.getaddrinfo,original)

    def test_diagnostic_summary_rejects_arbitrary_values(self):
        record=diagnostics.make_record('public_read_failed',read_id='a'*32,read_phase='dns',read_error='dns_error',
            errno=-2,winerror=11001,read_timings_ms={'dns':2,'PRIVATE':3},url='PRIVATE',read_message='PRIVATE')
        self.assertEqual(record['read_phase'],'dns');self.assertEqual(record['errno'],-2)
        self.assertEqual(record['read_timings_ms'],{'dns':2})
        self.assertNotIn('PRIVATE',json.dumps(record))
        import vk_read_trace as trace
        self.assertEqual(trace.sanitize_summary({'read_phase':[],'read_error':{}}),{})

    def test_read_summary_survives_scheduler_error_chain_redaction(self):
        error=api.VkApiReadDeadline('PRIVATE')
        error.read_diagnostics={'read_id':'b'*32,'read_phase':'dns','read_error':'helper_deadline',
                                'elapsed_ms':20005,'errno':11001,'message':'PRIVATE','url':'https://PRIVATE'}
        record=diagnostics.make_record('scheduler_failed',error=error)
        self.assertEqual(record['error_chain'][0]['public_read']['read_phase'],'dns')
        self.assertNotIn('PRIVATE',json.dumps(record))


from tests import test_batch_engagement as refresh_fixtures
class ReadDiagnosticIntegrationTests(unittest.TestCase):
    setUp=refresh_fixtures.RefreshTests.setUp
    payload=refresh_fixtures.RefreshTests.payload

    def test_real_refresh_persists_safe_read_summary_without_jobs_or_attempts(self):
        import vk_batch_engagement as engagement
        from contextlib import closing,nullcontext,redirect_stdout
        import vk_batch as engine
        payload=self.payload()[1]
        script=f"import vk_api_read as a;a._default_transport=lambda p:(200,{payload!r});a._transport_stdio()"
        with patch.object(api,'_TRANSPORT_SCRIPT',script),patch.object(api,'configured_token',return_value='PRIVATE'),\
             patch.object(engagement.app,'exclusive_run',return_value=nullcontext()),redirect_stdout(io.StringIO()):
            report=engagement.refresh(self.state,self.paths[1],self.groups,
                {'daily_requests':5000,'pages_per_group':10,'pause_seconds':5},clock=lambda:self.now,
                sleeper=lambda _:None,diagnostic=lambda event,**kw:self.state.log_event(event,self.now,slot=0,**kw))
        self.assertEqual(report['state'],'ready')
        rows=self.state.db.execute("SELECT snapshot FROM scheduler_diagnostics WHERE event='public_read_finished'").fetchall()
        self.assertEqual(len(rows),1)
        record=json.loads(rows[0][0]);self.assertEqual(record['slot'],0);self.assertEqual(record['http_status'],200)
        self.assertNotIn('PRIVATE',rows[0][0])
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM scheduler_jobs').fetchone()[0],0)
        with self.state.account_db(1) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM action_attempts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM api_read_attempts').fetchone()[0],1)


if __name__=='__main__':unittest.main()
