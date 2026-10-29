"""Own-profile alias resolution shares read guards without wall cache projection."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

import vk_api_read as api
import vk_batch as engine


class IdentityApiReadTests(unittest.TestCase):
    def setUp(self):
        root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup)
        self.db=engine.db_open(Path(root.name)/'catalog.sqlite3');self.addCleanup(self.db.close)
        self.now=1800000000.;self.calls=[]
        self.reply=(200,{'response':[{'id':123,'screen_name':'alias'}]})
        def transport(params):self.calls.append(params);return self.reply
        self.reader=api.VKReadClient(self.db,'PRIVATE-TOKEN',pause_seconds=5,per_run=25,
            per_24h=5000,transport=transport,clock=lambda:self.now,sleeper=self.sleep)
        self.sleeps=[]

    def sleep(self,seconds):self.sleeps.append(seconds);self.now+=seconds

    def used(self):return self.db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0]

    def test_user_resolution_reserves_group_zero_and_never_projects_wall_data(self):
        self.reply=(200,{'response':[{'id':123,'screen_name':'Own.ALIAS_7'}]})
        self.reader.pages=Mock()
        with patch.object(api,'project_public_page') as project:
            self.assertEqual(self.reader.resolve_screen_name('own.Alias_7'),123)
            self.assertEqual(self.reader.resolve_screen_name('own.Alias_7'),123)
        self.assertEqual(self.used(),2);self.assertEqual(self.sleeps,[5])
        self.assertEqual([r[0] for r in self.db.execute('SELECT group_id FROM api_read_attempts')],[0,0])
        self.assertEqual(self.calls[0],{'_read_method':'users.get',
            'user_ids':'own.Alias_7','fields':'screen_name','access_token':'PRIVATE-TOKEN','v':api.API_VERSION})
        self.reader.pages.get.assert_not_called();project.assert_not_called()
        self.assertNotIn('PRIVATE-TOKEN','\n'.join(self.db.iterdump()))
        self.assertNotIn('own.Alias_7','\n'.join(self.db.iterdump()))

    def test_bad_alias_never_reserves_or_calls_http(self):
        for alias in (None,True,12,'',' space','two words','https://vk.com/name','name/x',
                      'name?token=x','name\n','аlias','a'*65):
            with self.subTest(alias=alias),self.assertRaises(ValueError):
                self.reader.resolve_screen_name(alias)
        self.assertEqual(self.used(),0);self.assertEqual(self.calls,[])

    def test_nonuser_or_malformed_response_never_becomes_identity(self):
        for response in ({'type':'user','object_id':123},{},[],None,
                         [{'id':123,'screen_name':'alias'},{'id':124,'screen_name':'alias'}],
                         [{'type':'group','id':123,'screen_name':'alias'}],
                         [{'type':'application','id':123,'screen_name':'alias'}],
                         [{'id':True,'screen_name':'alias'}],[{'id':0,'screen_name':'alias'}],
                         [{'id':-1,'screen_name':'alias'}],[{'id':'123','screen_name':'alias'}],
                         [{'id':2**53,'screen_name':'alias'}],[{'id':123}],
                         [{'id':123,'screen_name':'other'}],[{'id':123,'screen_name':True}],
                         [{'id':123,'screen_name':'alİas'}],
                         [{'id':123,'screen_name':'alias','deactivated':'deleted'}],
                         [{'id':123,'screen_name':'alias','deactivated':'banned'}]):
            with self.subTest(response=response):
                self.reply=(200,{'response':response})
                with self.assertRaises(api.VkApiFailure):self.reader.resolve_screen_name('alias')
        self.assertEqual(len(self.calls),18)

    def test_permission_1051_is_auth_failure_with_one_request_and_no_fallback(self):
        self.reply=(200,{'error':{'error_code':1051,'error_msg':'PRIVATE'}})
        with self.assertRaises(api.VkApiAuthFailure) as caught:self.reader.resolve_screen_name('alias')
        self.assertEqual(caught.exception.code,1051)
        self.assertNotIn('PRIVATE',str(caught.exception))
        self.assertEqual(len(self.calls),1);self.assertEqual(self.used(),1)
        self.assertEqual(self.calls[0]['_read_method'],'users.get')
        self.assertIsNone(engine.safety_lock_state(self.db,0))

    def test_daily_and_per_run_budgets_block_before_http(self):
        self.reader.per_run=1;self.reader.resolve_screen_name('alias')
        with self.assertRaises(api.VkApiFailure):self.reader.resolve_screen_name('alias')
        self.assertEqual((len(self.calls),self.used()),(1,1))
        self.reader.per_run=10;self.reader.per_24h=1
        with self.assertRaises(api.VkApiFailure):self.reader.resolve_screen_name('alias')
        self.assertEqual((len(self.calls),self.used()),(1,1))

    def test_post_pacing_deadline_callback_runs_before_reservation(self):
        self.reader.resolve_screen_name('alias');before=self.used()
        def guard():
            self.assertEqual(self.sleeps,[5]);self.assertEqual(self.used(),before)
            raise api.VkApiReadDeadline('closed')
        self.reader.before_request=guard
        with self.assertRaises(api.VkApiReadDeadline):self.reader.resolve_screen_name('alias')
        self.assertEqual((self.used(),len(self.calls)),(1,1))

    def test_rate_and_protection_errors_latch_and_block_later_reads(self):
        for status,code,kind in ((429,None,api.VkApiLimited),(200,6,api.VkApiLimited),
                                (200,9,api.VkApiLimited),(200,29,api.VkApiLimited),
                                (200,14,api.VkApiProtection),(200,17,api.VkApiProtection)):
            with self.subTest(code=code,status=status),tempfile.TemporaryDirectory() as directory:
                with closing(engine.db_open(Path(directory)/'catalog.sqlite3')) as db:
                    call=Mock(return_value=(status,{'error':{'error_code':code,'error_msg':'PRIVATE'}}))
                    reader=api.VKReadClient(db,'token',transport=call)
                    with self.assertRaises(kind):reader.resolve_screen_name('alias')
                    self.assertIsNotNone(engine.safety_lock_state(db,0))
                    with self.assertRaises(engine.StopRun):reader.wall_get(101)
                    with self.assertRaises(engine.StopRun):reader.resolve_screen_name('alias')
                    self.assertEqual(call.call_count,1)
                    self.assertNotIn('PRIVATE','\n'.join(db.iterdump()))

    def test_auth_other_http_and_invalid_errors_fail_without_raw_payload(self):
        for reply,kind in (((200,{'error':{'error_code':5,'error_msg':'PRIVATE'}}),api.VkApiAuthFailure),
                           ((200,{'error':{'error_code':100,'error_msg':'PRIVATE'}}),api.VkApiFailure),
                           ((200,{'error':{'error_code':True}}),api.VkApiFailure),
                           ((503,{}),api.VkApiFailure),((200,[]),api.VkApiFailure)):
            self.reply=reply
            with self.subTest(reply=reply),self.assertRaises(kind) as error:
                self.reader.resolve_screen_name('alias')
            self.assertNotIn('PRIVATE',str(error.exception))

    def test_transport_limit_latches_and_preserves_sanitized_diagnostics(self):
        error=api.VkApiLimited('Public API limit',code=6)
        error.read_diagnostics={'read_error':'vk_rate_limited','vk_error_code':6}
        self.reader.transport=Mock(side_effect=error)
        with self.assertRaises(api.VkApiLimited) as caught:self.reader.resolve_screen_name('alias')
        self.assertEqual(caught.exception.read_diagnostics,error.read_diagnostics)
        self.assertIsNotNone(engine.safety_lock_state(self.db,0))


class IdentityTransportTests(unittest.TestCase):
    def test_default_transport_allowlists_method_and_strips_internal_selector(self):
        response=Mock();response.getcode.return_value=200
        response.read.return_value=b'{"response":[{"id":123,"screen_name":"alias"}]}'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        params={'_read_method':'users.get','user_ids':'alias','fields':'screen_name','access_token':'secret','v':api.API_VERSION}
        before=dict(params)
        with patch.object(api.urllib.request,'urlopen',return_value=response) as opened:
            self.assertEqual(api._default_transport(params)[0],200)
        request=opened.call_args.args[0]
        self.assertEqual(request.full_url,'https://api.vk.com/method/users.get')
        self.assertEqual(parse_qs(request.data.decode()),{'user_ids':['alias'],'fields':['screen_name'],'access_token':['secret'],'v':[api.API_VERSION]})
        self.assertEqual(params,before)
        with patch.object(api.urllib.request,'urlopen') as opened:
            for method in ('utils.resolveScreenName','https://evil.invalid','wall.post',None,True,[],{}):
                with self.subTest(method=method),self.assertRaises(ValueError):
                    api._default_transport({'_read_method':method})
            opened.assert_not_called()

    def test_wall_endpoint_default_stays_unchanged(self):
        response=Mock();response.getcode.return_value=200;response.read.return_value=b'{"response":{"items":[]}}'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        with patch.object(api.urllib.request,'urlopen',return_value=response) as opened:
            api._default_transport({'owner_id':-101})
        self.assertEqual(opened.call_args.args[0].full_url,api.API_URL)

    def test_existing_wall_transport_test_endpoint_override_remains_supported(self):
        response=Mock();response.getcode.return_value=200;response.read.return_value=b'{"response":{"items":[]}}'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        with patch.object(api,'API_URL','http://localhost:1234/'),patch.object(api.urllib.request,'urlopen',return_value=response) as opened:
            api._default_transport({'owner_id':-101})
        self.assertEqual(opened.call_args.args[0].full_url,'http://localhost:1234/')

    def test_bounded_child_preserves_only_allowed_identity_method(self):
        script=("import json,urllib.request,vk_api_read as a;"
            "exec(\"class R:\\n def __enter__(self):return self\\n def __exit__(self,*args):pass\\n def getcode(self):return 200\\n def read(self,n):return b'{\\\"response\\\":[{\\\"id\\\":123,\\\"screen_name\\\":\\\"alias\\\"}]}'\");"
            "urllib.request.urlopen=lambda request,timeout:R() if request.full_url=='https://api.vk.com/method/users.get' and b'_read_method' not in request.data else None;"
            "a._transport_stdio()")
        with patch.object(api,'_TRANSPORT_SCRIPT',script):
            result=api.bounded_transport({'_read_method':'users.get','user_ids':'alias','fields':'screen_name','access_token':'PRIVATE'},timeout=20)
            self.assertEqual(result,(200,{'response':[{'id':123,'screen_name':'alias'}]}))
            with self.assertRaises(api.VkApiFailure):
                api.bounded_transport({'_read_method':'wall.post','screen_name':'alias'},timeout=20)


if __name__=='__main__':unittest.main()
