import http.client
import importlib.util
import json
from pathlib import Path
import subprocess
import shutil
import socket
import sys
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('vk_web', Path(__file__).resolve().parents[1] / 'vk_web.py')
web = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(web)


class WebPanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = Path(self.tmp.name) / 'secrets_web.json'
        web.write_auth(self.auth, 'operator', 'Long unique password 123')
        self.calls = []
        self.rpc_result = {'accounts': [], 'scheduler': {}, 'jobs': []}
        def rpc(payload):
            self.calls.append(payload)
            return self.rpc_result
        self.server = web.make_server('127.0.0.1', 0, self.auth, rpc, allowed_hosts=['127.0.0.1'])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.cookie = ''
        self.csrf = ''

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=4)
        current = {'Origin': f'http://127.0.0.1:{self.port}', 'Content-Type': 'application/json'}
        if self.cookie:
            current['Cookie'] = self.cookie
        if self.csrf:
            current['X-CSRF-Token'] = self.csrf
        current.update(headers or {})
        body = json.dumps(data).encode() if data is not None else None
        connection.request(method, path, body, current)
        response = connection.getresponse()
        content = response.read()
        result = response.status, dict(response.getheaders()), content
        connection.close()
        return result

    def login(self):
        status, headers, body = self.request('POST', '/api/login', {'username':'operator', 'password':'Long unique password 123'})
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.csrf = json.loads(body)['csrf']
        return headers

    def test_auth_file_stores_only_salted_hash(self):
        raw = self.auth.read_text()
        self.assertNotIn('Long unique', raw)
        credentials = json.loads(raw)
        self.assertTrue(web.verify_password(credentials, 'operator', 'Long unique password 123'))
        self.assertFalse(web.verify_password(credentials, 'operator', 'wrong'))
        web.write_auth(Path(self.tmp.name) / 'short_auth.json', 'operator', 'eight888')
        with self.assertRaises(FileExistsError):
            web.write_auth(self.auth, 'operator', 'different long password')

    def test_auth_required_and_no_rpc_before_login(self):
        self.assertEqual(self.request('POST', '/api/rpc', {'operation':'snapshot'})[0], 401)
        self.assertEqual(self.calls, [])

    def test_cookie_csrf_and_origin(self):
        headers = self.login()
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertEqual(self.request('POST', '/api/rpc', {'operation':'snapshot'})[0], 200)
        self.assertEqual(self.request('POST', '/api/rpc', {'operation':'add_account','name':'A'}, {'X-CSRF-Token':'bad'})[0], 403)
        self.assertEqual(self.request('POST', '/api/rpc', {'operation':'snapshot'}, {'Origin':'http://evil.example'})[0], 403)
        self.assertEqual(len(self.calls), 1)

    def test_host_and_body_limit(self):
        self.assertEqual(self.request('GET', '/', headers={'Host':'evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/login', {'password':'x' * 20000})[0], 413)

    def test_repeated_bad_origin_consumes_bounded_body_before_denial(self):
        self.login()
        payload = json.dumps({'operation':'snapshot'}).encode()
        for _ in range(5):
            # Separate headers from the body to expose a premature close. The
            # server must read the bounded body before sending its 403; no retry.
            connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=4)
            try:
                connection.putrequest('POST', '/api/rpc')
                connection.putheader('Origin', 'http://evil.example')
                connection.putheader('Content-Type', 'application/json')
                connection.putheader('Content-Length', str(len(payload)))
                connection.putheader('Cookie', self.cookie)
                connection.putheader('X-CSRF-Token', self.csrf)
                connection.endheaders()
                connection.sock.settimeout(.05)
                with self.assertRaises(socket.timeout):
                    connection.sock.recv(1, socket.MSG_PEEK)
                connection.sock.settimeout(4)
                connection.send(payload)
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                self.assertEqual(json.loads(response.read()), {'error':'invalid_origin'})
            finally:
                connection.close()
        self.assertEqual(self.calls, [])

    def test_login_throttle(self):
        for _ in range(5):
            self.assertEqual(self.request('POST', '/api/login', {'username':'operator','password':'wrong'})[0], 401)
        self.assertEqual(self.request('POST', '/api/login', {'username':'operator','password':'wrong'})[0], 429)

    def test_operations_validation_logout(self):
        self.login()
        for payload in ({'operation':'retry'}, {'operation': []}, {'operation':'configure_account','account':1,'enabled':True,'likes':999}, {'operation':'add_account','name':'<script>'}):
            self.assertEqual(self.request('POST', '/api/rpc', payload)[0], 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.request('POST', '/api/logout', {})[0], 200)
        self.assertFalse(json.loads(self.request('GET', '/api/session')[2])['authenticated'])

    def test_static_whitelist_and_security_headers(self):
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'VK Unified', body)
        self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
        self.assertEqual(self.request('GET', '/secrets_vk_web.json')[0], 404)
        self.assertEqual(self.request('GET', '/../vk_web.py')[0], 404)
        self.assertEqual(self.request('GET', '/api/connection.rdp')[0], 401)
        self.login()
        status, headers, body = self.request('GET', '/api/connection.rdp')
        self.assertEqual(status, 200)
        self.assertIn(b'full address:s:192.168.100.46', body)
        self.assertNotIn(b'username', body)

    def test_rpc_arguments_are_fixed_and_payload_is_stdin(self):
        remote = web.RemoteRPC('vk-bot@192.168.100.46', 'C:/Users/vk-bot/vk_bot', 'C:/keys/id_rsa')
        completed = subprocess.CompletedProcess([], 0, '{"ok":true}', '')
        with patch.object(web.subprocess, 'run', return_value=completed) as mocked:
            self.assertEqual(remote({'operation':'snapshot'}), {'ok':True})
        args, kwargs = mocked.call_args
        self.assertIsInstance(args[0], list)
        self.assertNotIn('snapshot', ' '.join(args[0]))
        self.assertEqual(json.loads(kwargs['input']), {'operation':'snapshot'})
        self.assertFalse(kwargs.get('shell', False))
        with self.assertRaises(ValueError):
            web.RemoteRPC('bad;command', 'C:/safe', 'key')
        with self.assertRaises(ValueError):
            web.RemoteRPC('user@host', "C:/bad'command", 'key')

    def test_remote_guard_errors_preserved(self):
        remote = web.RemoteRPC('user@host', 'C:/safe', 'key')
        with patch.object(web.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2, '{"error":"account_must_be_disabled_for_login"}', 'private details')):
            self.assertEqual(remote({'operation':'login_start','account':1}), {'error':'account_must_be_disabled_for_login'})

    def test_local_backend_uses_fixed_rpc_and_preserves_guard_without_ssh(self):
        calls = []
        def local_rpc(source, destination):
            calls.append(json.loads(source.read()))
            json.dump({'error':'account_guard'}, destination)
            return 2
        with patch.dict(sys.modules, {'vk_control':SimpleNamespace(rpc=local_rpc)}), patch.object(web.subprocess, 'run') as shell:
            local = web.LocalRPC()
            self.assertEqual(local({'operation':'snapshot'}), {'error':'account_guard'})
            self.assertEqual(local({'operation':'run_actions'}), {'error':'invalid_operation_or_fields'})
            shell.assert_not_called()
        self.assertEqual(calls, [{'operation':'snapshot'}])

    def test_posts_filters_validated(self):
        self.assertTrue(web.validate_payload({'operation':'posts','group_id':123,'limit':100}))
        self.assertFalse(web.validate_payload({'operation':'posts','group_id':True}))
        self.assertFalse(web.validate_payload({'operation':'posts','limit':101}))
        self.assertFalse(web.validate_payload({'operation':'posts','group_id':2**53}))
        self.assertTrue(web.validate_payload({'operation':'refresh_statistics'}))
        self.assertTrue(web.validate_payload({'operation':'refresh_statistics','group_id':123}))
        self.assertFalse(web.validate_payload({'operation':'refresh_statistics','group_id':0}))
        self.assertFalse(web.validate_payload({'operation':'refresh_statistics','group_id':True}))
        self.assertFalse(web.validate_payload({'operation':'refresh_statistics','limit':100}))
        self.assertTrue(web.validate_payload({'operation':'login_check','account':1}))
        self.assertFalse(web.validate_payload({'operation':'login_check','account':True}))
        self.assertFalse(web.validate_payload({'operation':'login_check','account':0}))

    def test_backend_errors_are_fixed_and_do_not_leak_details(self):
        self.login()
        self.rpc_result = {'error':'Traceback private secret', 'details':'private token'}
        status, _, body = self.request('POST','/api/rpc',{'operation':'snapshot'})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body), {'error':'backend_error'})
        self.rpc_result = {'error':'account_guard', 'details':'private token'}
        self.assertEqual(json.loads(self.request('POST','/api/rpc',{'operation':'snapshot'})[2]), {'error':'account_guard'})

    def test_configure_accepts_account_name_sent_by_add_any_form(self):
        self.login()
        payload={'operation':'configure_account','account':5,'name':'Аккаунт Техно',
                 'enabled':False,'likes':0,'personal_reposts':0,'community_reposts':0,'communities':[100,200]}
        status,_,_=self.request('POST','/api/rpc',payload)
        self.assertEqual(status,200)
        self.assertEqual(self.calls,[payload])
        for name in ('', 'x'*81, '<script>', 'bad\x01name'):
            with self.subTest(name=name):
                self.assertFalse(web.validate_payload({**payload,'name':name}))


class HeartbeatRenderingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js required for isolated frontend regression')
    def test_missing_community_destinations_preserves_form_without_request(self):
        script=r"""
const fs=require('node:fs'),vm=require('node:vm');
class Element {
 constructor(){this.children=[];this.value='';this.classList={toggle(){}};this.open=true;}
 append(...v){this.children.push(...v);} replaceChildren(...v){this.children=v;}
 setAttribute(){} add(){} showModal(){this.open=true;} close(){this.open=false;}
}
const elements=new Map(),document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement(){return new Element();},querySelectorAll(){return [];}};
const calls=[],fixture={account_flow:'add_any',accounts:[{account:5,name:'Техно',enabled:false,login:{state:'complete'},authentication:{state:'matched',fresh:true,expected_vk_id:555,observed_vk_id:555},policy:{}}]};
const context={document,Node:Element,Option:Element,fixture,calls,setInterval(){}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8').split('\n(async()=>')[0],context);
vm.runInContext("snapshot=fixture;csrf='fixture';rpc=async(...args)=>{calls.push(args);return {}};",context);
for(const [id,value] of Object.entries({'account-id':'5','account-name':'Техно',likes:'50','personal-reposts':'10','community-reposts':'20',communities:''}))document.getElementById(id).value=value;
document.getElementById('account-enabled').checked=true;
(async()=>{await document.getElementById('account-form').onsubmit({preventDefault(){},submitter:{}});console.log(JSON.stringify({calls,error:document.getElementById('form-error').textContent,quota:document.getElementById('community-reposts').value,open:document.getElementById('account-dialog').open}));})().catch(()=>process.exitCode=1);
"""
        result=subprocess.run([shutil.which('node'),'-e',script,str(web.ROOT/'web/app.js')],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads(result.stdout)
        self.assertEqual(data['calls'],[])
        self.assertIn('два',data['error'])
        self.assertEqual(data['quota'],'20')
        self.assertTrue(data['open'])

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for isolated frontend regression')
    def test_async_login_check_polls_status_only_and_preserves_unsaved_input(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm');
class Element {
 constructor(){this.ownText='';this.children=[];this.value='';this.classList={toggle(){}};}
 set textContent(value){this.ownText=String(value);this.children=[];}
 get textContent(){return this.ownText+this.children.map(child=>child.textContent||'').join(' ');}
 append(...children){this.children.push(...children);} replaceChildren(...children){this.ownText='';this.children=[...children];}
 add(child){this.children.push(child);} setAttribute(){} showModal(){this.open=true;} close(){this.open=false;}
}
const elements=new Map();
const document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement(){return new Element();},querySelectorAll(){return [];}};
const fixture={accounts:[{account:1,name:'Первый',vk_id:123,bound:true,enabled:false,login:{state:'idle'},authentication:{state:'matched',expected_vk_id:123,observed_vk_id:123,checked_at:'2026-10-23T05:59:00Z',fresh:true,can_finish:true},policy:{communities:[]}}]};
const calls=[];let statusReads=0;
const proof={state:'wrong_account',expected_vk_id:123,observed_vk_id:456,checked_at:'2026-10-23T06:00:00Z',fresh:true};
const fakeRPC=async(operation,fields)=>{
 calls.push({operation,...fields});
 if(operation==='login_check')return {state:'opening'};
 if(operation==='login_status')return ++statusReads===1?{state:'opening'}:{state:'checked',authentication:proof};
 throw new Error('unexpected operation '+operation);
};
const context={document,Node:Element,Option:Element,fixture,fakeRPC,setInterval(){}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8').split('\n(async()=>')[0],context);
vm.runInContext("snapshot=fixture;csrf='qa';rpc=fakeRPC;",context);
(async()=>{
 await vm.runInContext('requestAccountCheck(1)',context);
 const started=elements.get('notice').textContent;
 const immediate=vm.runInContext('({proof:{...snapshot.accounts[0].authentication},canEnable:accountAuthentication(snapshot.accounts[0]).canEnable})',context);
 document.getElementById('account-dialog').open=true;document.getElementById('likes').value='17';
 await vm.runInContext('pollLoginStatuses()',context);
 const duringDialog=calls.length;
 elements.get('account-dialog').open=false;
 vm.runInContext('busy=true;',context);await vm.runInContext('pollLoginStatuses()',context);
 const duringBusy=calls.length;
 vm.runInContext('busy=false;',context);
 await vm.runInContext('pollLoginStatuses()',context);
 await vm.runInContext('pollLoginStatuses()',context);
 await vm.runInContext('pollLoginStatuses()',context);
 console.log(JSON.stringify({started,immediate,calls,duringDialog,duringBusy,notice:elements.get('notice').textContent,accounts:elements.get('account-list').textContent,unsaved:elements.get('likes').value,authentication:fixture.accounts[0].authentication,enabled:fixture.accounts[0].enabled}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(Path(web.ROOT) / 'web' / 'app.js')], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn('Проверка запущена', data['started'])
        self.assertNotIn('Вход не проверен', data['started'])
        self.assertEqual(data['immediate']['proof']['state'], 'unverified')
        self.assertFalse(data['immediate']['proof']['fresh'])
        self.assertFalse(data['immediate']['canEnable'])
        self.assertEqual(data['duringDialog'], 1)
        self.assertEqual(data['duringBusy'], 1)
        self.assertEqual(data['calls'], [{'operation':'login_check','account':1}, {'operation':'login_status','account':1}, {'operation':'login_status','account':1}])
        self.assertIn('Открыт другой аккаунт', data['notice'])
        self.assertIn('Обнаружен VK ID: 456', data['accounts'])
        self.assertEqual(data['authentication']['state'], 'wrong_account')
        self.assertEqual(data['unsaved'], '17')
        self.assertFalse(data['enabled'])

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for isolated frontend regression')
    def test_authentication_and_partial_cached_group_statistics_rendered(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm');
class Element {
 constructor(){this.ownText='';this.children=[];this.value='';this.classList={toggle(){}};}
 set textContent(value){this.ownText=String(value);this.children=[];}
 get textContent(){return this.ownText+this.children.map(child=>child.textContent||'').join(' ');}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.ownText='';this.children=[...children];}
 add(child){this.children.push(child);} setAttribute(){} showModal(){this.open=true;} close(){this.open=false;}
}
const elements=new Map();
const document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement(){return new Element();},querySelectorAll(){return [];}};
const basic={policy:{likes:0,personal_reposts:0,community_reposts:0,communities:[]},enabled:false,bound:true,login:{state:'ready'}};
const fixture={now:'2026-10-23T06:00:00Z',scheduler:{},accounts:[
 {...basic,account:1,name:'Первый',vk_id:123,authentication:{state:'wrong_account',expected_vk_id:123,observed_vk_id:456,checked_at:'2026-10-23T06:00:00Z',fresh:true}},
 {...basic,account:2,name:'Legacy',vk_id:789,login:{state:'unverified',reason:'legacy_identity_not_compared'}},
 {...basic,account:3,name:'Проверенный',vk_id:333,login:{state:'complete',verification_version:2},authentication:{state:'matched',expected_vk_id:333,observed_vk_id:333,checked_at:'2026-10-23T06:00:00Z',fresh:true,can_finish:true}},
 {...basic,account:4,name:'Устаревший',vk_id:444,authentication:{state:'stale',expected_vk_id:444,observed_vk_id:444,checked_at:'2026-10-22T06:00:00Z',fresh:false}}
],jobs:[],posts:[],groups:[{id:100,name:'Группа',posts:3,published_from:1792720000,published_until:1792722000,
 observed_at:'2026-10-23T06:00:00Z',observed_until:'2026-10-23T06:02:00Z',
 like_count:null,like_count_known_sum:23,like_count_known_posts:2,
 comment_count:0,comment_count_known_sum:0,comment_count_known_posts:3,
 repost_count:null,repost_count_known_sum:0,repost_count_known_posts:1,
 view_count:null,view_count_known_sum:null,view_count_known_posts:0}],
 statistics_refresh:{state:'running',groups_done:2,total:57,requests:3,at:'2026-10-23T06:02:00Z'}};
const context={document,Node:Element,Option:Element,fixture,setInterval(){},fetch:async()=>({ok:true,json:async()=>({authenticated:false})})};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8').split('\n(async()=>')[0],context);
vm.runInContext('snapshot=fixture;render();editAccount(fixture.accounts[0]);',context);
const wrongBlocked=elements.get('account-enabled').disabled;
vm.runInContext('editAccount(fixture.accounts[2]);',context);
const matchedAllowed=!elements.get('account-enabled').disabled;
const result={accounts:elements.get('account-list').textContent,groups:elements.get('groups-table').textContent,
 refresh:elements.get('statistics-progress').textContent,wrongBlocked,matchedAllowed};
result.phaseGuards=vm.runInContext("Object.fromEntries(['complete','ready','checked','opening','awaiting_login','wrong_account'].map(state=>{const auth=accountAuthentication({...fixture.accounts[2],login:{state,verification_version:2}});return [state,{canEnable:auth.canEnable,canFinish:auth.canFinish}];}))",context);
result.legacyReady=vm.runInContext("accountAuthentication({...fixture.accounts[2],login:{state:'ready',verification_version:1}}).canFinish",context);
vm.runInContext("snapshot.statistics_refresh={state:'partial',groups_done:55,total:57,requests:57,failures:2,rejected_posts:0};renderGroups();",context);
result.partialRefresh=elements.get('statistics-progress').textContent;
result.partialGroups=elements.get('groups-table').textContent;
context.calls=[];
vm.runInContext("rpc=async(operation,fields={})=>{calls.push({operation,...fields});if(operation==='snapshot')return fixture;if(operation==='login_check')return {state:'checked',authentication:fixture.accounts[0].authentication};return {posts:[]};};$('post-group').value='100';",context);
const checkButton=elements.get('account-list').children[0].children.flatMap(child=>child.children).find(child=>child.textContent==='Проверить вход');
(async()=>{
 await vm.runInContext('loadPosts()',context);
 result.postCall=context.calls[0];
 await checkButton.onclick();
 result.loginCall=context.calls[1];
 result.notice=elements.get('notice').textContent;
 console.log(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(Path(web.ROOT) / 'web' / 'app.js')], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn('Открыт другой аккаунт', data['accounts'])
        self.assertIn('Ожидался VK ID: 123', data['accounts'])
        self.assertIn('Обнаружен VK ID: 456', data['accounts'])
        self.assertIn('Вход не проверен', data['accounts'])
        self.assertIn('Активное окно автоматически не закрывается', data['accounts'])
        self.assertIn('Проверка входа устарела', data['accounts'])
        self.assertIn('Вход подтверждён', data['accounts'])
        self.assertNotIn('Привязка подтверждена', data['accounts'])
        self.assertTrue(data['wrongBlocked'])
        self.assertTrue(data['matchedAllowed'])
        self.assertEqual(data['phaseGuards']['complete'], {'canEnable':True,'canFinish':False})
        self.assertEqual(data['phaseGuards']['checked'], {'canEnable':True,'canFinish':False})
        self.assertEqual(data['phaseGuards']['ready'], {'canEnable':False,'canFinish':True})
        for phase in ('opening','awaiting_login','wrong_account'):
            self.assertEqual(data['phaseGuards'][phase], {'canEnable':False,'canFinish':False})
        self.assertFalse(data['legacyReady'])
        self.assertIn('23 • 2/3', data['groups'])
        self.assertIn('0 • 1/3', data['groups'])
        self.assertIn('нет данных • 0/3', data['groups'])
        self.assertIn('2/57', data['refresh'])
        self.assertIn('3', data['refresh'])
        self.assertIn('Частичное обновление', data['partialRefresh'])
        self.assertIn('55/57', data['partialRefresh'])
        self.assertIn('ошибок: 2', data['partialRefresh'])
        self.assertIn('отклонено публикаций: 0', data['partialRefresh'])
        self.assertIn('23 • 2/3', data['partialGroups'])
        self.assertIn('нет данных • 0/3', data['partialGroups'])
        self.assertEqual(data['postCall'], {'operation':'posts','group_id':100,'limit':100})
        self.assertEqual(data['loginCall'], {'operation':'login_check','account':1})
        self.assertIn('Открыт другой аккаунт', data['notice'])

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for isolated frontend regression')
    def test_add_account_starts_login_once_and_preserves_created_row_on_error(self):
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const helper = source.match(/\/\/ BEGIN account creation helper([\s\S]*?)\/\/ END account creation helper/);
if (!helper) throw new Error('account creation helper missing');
async function run(fail) {
 const calls=[];
 const context={rpc:async(operation,fields)=>{
   calls.push({operation,...fields});
   if(operation==='add_account')return {account:3,name:fields.name,enabled:false};
   if(operation==='login_start') {
     if(fail)throw new Error('interactive_login_task_failed');
     return {state:'starting'};
   }
   throw new Error('unexpected operation');
 }};
 vm.createContext(context);vm.runInContext(helper[1],context);
 return {result:await context.createAccountAndLogin('Новый аккаунт'),calls};
}
(async()=>console.log(JSON.stringify({success:await run(false),failure:await run(true)})))().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(Path(web.ROOT) / 'web' / 'app.js')], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        expected_calls = [{'operation':'add_account','name':'Новый аккаунт'}, {'operation':'login_start','account':3}]
        self.assertEqual(data['success']['calls'], expected_calls)
        self.assertEqual(data['failure']['calls'], expected_calls)
        self.assertEqual(data['success']['result'], {'account':3,'loginStarted':True})
        self.assertEqual(data['failure']['result'], {'account':3,'loginStarted':False,'error':'interactive_login_task_failed'})

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for isolated frontend regression')
    def test_real_shaped_heartbeat_and_stale_are_distinct_from_stopped(self):
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const helper = source.match(/\/\/ BEGIN heartbeat helpers([\s\S]*?)\/\/ END heartbeat helpers/);
if (!helper) throw new Error('heartbeat renderer helper missing');
const fixture = {at:'2026-10-23T06:00:00+00:00', phase:'stopped', mode:'live',
 day:'2026-10-23',next_batch:'2026-10-23T12:40:00+05:00',pid:123,usage:{},remaining:{}};
const context = {fixture, now:Date.parse('2026-10-23T06:00:30Z')};
vm.createContext(context);
vm.runInContext(helper[1], context);
const render = (value, now) => context.workerHeartbeat(value, now);
const result = {
 stopped:render(fixture,context.now),
 oldStopped:render(fixture,context.now+3600000),
 stale:render({...fixture,phase:'waiting_batch'},context.now+3600000),
 fresh:render({...fixture,phase:'refreshing_counters'},context.now),
 missing:render(null,context.now),
 invalid:render({at:{bad:true},phase:'waiting_batch'},context.now)
};
// Exercise the actual render() integration with a minimal isolated DOM. This
// runs the shipped app source, without a browser, network, worker or live page.
class Element {
 constructor(){this.textContent='';this.value='';this.children=[];this.classList={toggle(){}};}
 append(){} replaceChildren(){} add(){} setAttribute(){} removeAttribute(){}
}
const elements = new Map();
const document = {
 getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
 createElement(){return new Element();}, querySelectorAll(){return [];}
};
const integration = {document, Node:Element, Option:Element, fixture,
 setInterval(){},fetch:async()=>({ok:true,json:async()=>({authenticated:false})})};
vm.createContext(integration);
vm.runInContext(source,integration);
vm.runInContext("snapshot={now:fixture.at,scheduler:{heartbeat:fixture},accounts:[],jobs:[],posts:[],groups:[]};render();",integration);
result.dom = Object.fromEntries(['heartbeat','worker-state','worker-phase','worker-mode'].map(id=>[id,elements.get(id).textContent]));
console.log(JSON.stringify(result));
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(Path(web.ROOT) / 'web' / 'app.js')], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = json.loads(result.stdout)
        self.assertEqual(rendered['stopped']['at'], '2026-10-23T06:00:00+00:00')
        self.assertEqual(rendered['stopped']['state'], 'stopped')
        self.assertEqual(rendered['oldStopped']['state'], 'stopped')
        self.assertIn('Остановлен', rendered['stopped']['stateText'])
        self.assertEqual(rendered['stale']['state'], 'stale')
        self.assertNotIn('Остановлен', rendered['stale']['stateText'])
        self.assertIn('не подтверждена', rendered['stale']['stateText'])
        self.assertEqual(rendered['fresh']['state'], 'fresh')
        self.assertEqual(rendered['fresh']['phaseText'], 'Обновление счётчиков')
        self.assertIn('реальные действия', rendered['fresh']['modeText'])
        self.assertEqual(rendered['missing']['state'], 'unknown')
        self.assertEqual(rendered['invalid']['at'], None)
        self.assertNotIn('[object Object]', result.stdout)
        self.assertIn('11:00:00', rendered['dom']['heartbeat'])
        self.assertIn('Остановлен', rendered['dom']['worker-state'])
        self.assertEqual(rendered['dom']['worker-phase'], 'Этап: Остановлен')
        self.assertEqual(rendered['dom']['worker-mode'], 'Режим: реальные действия (live)')


if __name__ == '__main__':
    unittest.main()
