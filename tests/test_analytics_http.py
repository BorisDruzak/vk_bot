import http.client,json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock
import vk_web as web


class AnalyticsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();auth=Path(self.tmp.name)/'auth.json'
        web.write_auth(auth,'fixture','fictional-analytics-password')
        self.reader=Mock(return_value=dict(groups=[],runs=[]));self.rpc=Mock()
        self.server=web.make_server('127.0.0.1',0,auth,self.rpc,analytics_reader=self.reader)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.port=self.server.server_address[1];self.cookie='';self.csrf=''

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()

    def request(self,method,path,data=None,**headers):
        h={'Origin':f'http://127.0.0.1:{self.port}','Cookie':self.cookie,'X-CSRF-Token':self.csrf,'Content-Type':'application/json'};h.update(headers)
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=4)
        c.request(method,path,None if data is None else json.dumps(data),h)
        r=c.getresponse();result=r.status,dict(r.getheaders()),r.read();c.close();return result

    def login(self):
        s,h,b=self.request('POST','/api/login',dict(username='fixture',password='fictional-analytics-password'))
        self.assertEqual(s,200);self.cookie=h['Set-Cookie'].split(';')[0];self.csrf=json.loads(b)['csrf']

    def test_session_csrf_and_read_only_endpoint(self):
        self.assertEqual(self.request('GET','/api/analytics')[0],401);self.reader.assert_not_called()
        self.login()
        self.assertEqual(self.request('GET','/api/analytics',**{'X-CSRF-Token':'bad'})[0],403)
        self.assertEqual(self.request('GET','/api/analytics')[0],200)
        self.reader.assert_called_once_with(run_id=None,week_mode='calendar',include_reposts=True)
        self.assertNotEqual(self.request('POST','/api/analytics',{})[0],200);self.rpc.assert_not_called()

    def test_valid_filters_and_invalid_query_allowlist(self):
        self.login();rid='a'*32
        self.assertEqual(self.request('GET',f'/api/analytics?run={rid}&weeks=period&reposts=0')[0],200)
        self.reader.assert_called_once_with(run_id=rid,week_mode='period',include_reposts=False)
        for query in ('run=../file','weeks=other','reposts=bad','extra=1','weeks=period&weeks=calendar'):
            self.assertEqual(self.request('GET','/api/analytics?'+query)[0],400)
        self.assertEqual(self.reader.call_count,1)

    def test_fixed_error_no_private_path_leak(self):
        self.login();self.reader.side_effect=OSError('SECRET/private/path')
        s,_,body=self.request('GET','/api/analytics');self.assertEqual(s,503)
        self.assertEqual(json.loads(body),dict(error='analytics_unavailable'))


if __name__=='__main__':unittest.main()
