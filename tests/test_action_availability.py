"""VK permission evidence, with all browser traffic fulfilled locally."""
import json
import unittest
from playwright.sync_api import sync_playwright
import vk_batch as engine


class AvailabilityBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()

    def setUp(self):
        self.page=self.browser.new_page();self.addCleanup(self.page.close)
        self.page.route('**/*',lambda route:route.fulfill(content_type='text/html',body=self.html))
        self.stamp=1792644694;self.checks=0

    def fixture(self,likes,reposts=None,*,extra=None,owner=-101,date=None,root=True):
        post={'owner_id':owner,'id':11,'date':self.stamp if date is None else date,'likes':likes,'reposts':reposts or {'user_reposted':0}}
        entries=[{'method':'wall.getById','response':{'items':[post]}}]
        if extra:entries+=extra
        body='<article><div data-testid="post" data-post-id="-101_11" data-post-nesting-lvl="0" style="width:500px;height:100px">Post</div></article>' if root else ''
        self.html=body+'<script>window.clicks=0;document.addEventListener("click",()=>window.clicks++);window.cur={apiPrefetchCache:'+json.dumps(entries)+'};</script>'
        self.page.goto('https://vk.com/wall-101_11')

    def guard(self):self.checks+=1

    def probe(self,kind='like'):
        import vk_action_availability as availability
        return availability.confirmed_denial(self.page,101,11,self.stamp,kind,self.guard)

    def test_explicit_like_denial_is_read_twice_without_click_or_navigation(self):
        self.fixture({'user_likes':0,'can_like':0})
        value=self.probe()
        self.assertEqual(value['reason'],'vk_like_denied')
        self.assertEqual(value['observations'],2)
        self.assertEqual(value['permission'],'likes.can_like')
        self.assertGreaterEqual(self.checks,3)
        self.assertEqual(self.page.evaluate('window.clicks'),0)

    def test_repost_denial_uses_documented_likes_can_publish_or_repost_disabled(self):
        for kind in ('personal','community'):
            for flag,value in [('can_publish',0),('repost_disabled',True)]:
                with self.subTest(kind=kind,flag=flag):
                    self.fixture({'user_likes':0,flag:value})
                    result=self.probe(kind)
                    self.assertEqual(result['reason'],'vk_repost_denied')
                    self.assertEqual(result['permission'],'likes.'+flag)

    def test_missing_footer_and_unknown_permissions_do_not_prove_vk_denial(self):
        self.fixture({'user_likes':0})
        self.assertIsNone(self.probe())

    def test_inline_json_permission_survives_cleared_live_cache(self):
        self.fixture({'user_likes':0,'can_like':0})
        entries=[{'method':'wall.getById','response':{'items':[{'owner_id':-101,'id':11,'date':self.stamp,
            'likes':{'user_likes':0,'can_like':0},'reposts':{'user_reposted':0}}]}}]
        self.html='''<article data-testid="post" data-post-id="-101_11" style="width:500px;height:100px">Post</article>
          <script>window.cur={};window.apiPrefetchReadyResolve=()=>{};window.clicks=0;
          window.cur.apiPrefetchCache='''+json.dumps(entries)+''';window.apiPrefetchReadyResolve();
          window.cur.apiPrefetchCache=[];</script>'''
        self.page.reload()
        self.assertEqual(self.probe()['permission'],'likes.can_like')
        self.assertEqual(self.page.evaluate('window.clicks'),0)

    def test_own_existing_like_and_allowed_action_are_never_classified_as_denied(self):
        for likes in ({'user_likes':1,'can_like':0},{'user_likes':0,'can_like':1}):
            self.fixture(likes);self.assertIsNone(self.probe())

    def test_wrong_source_date_missing_root_and_malformed_flags_are_not_denials(self):
        self.fixture({'user_likes':0,'can_like':0},owner=-999)
        self.assertIsNone(self.probe())
        for kwargs in ({'date':self.stamp+1},{'root':False}):
            self.fixture({'user_likes':0,'can_like':0},**kwargs)
            with self.assertRaises(engine.StopRun):self.probe()
        for value in (None,'0',[],2):
            self.fixture({'user_likes':0,'can_like':value})
            self.assertIsNone(self.probe())

    def test_conflicting_exact_vk_objects_do_not_allow_skip(self):
        extra=[{'method':'wall.getById','response':{'items':[{'owner_id':-101,'id':11,'date':self.stamp,
            'likes':{'user_likes':0,'can_like':1},'reposts':{'user_reposted':0}}]}}]
        self.fixture({'user_likes':0,'can_like':0},extra=extra)
        with self.assertRaises(engine.StopRun):self.probe()

    def test_permission_change_between_reads_cannot_turn_into_a_skip(self):
        self.fixture({'user_likes':0,'can_like':0})
        def change():
            self.checks+=1
            if self.checks==2:self.page.evaluate('window.cur.apiPrefetchCache[0].response.items[0].likes.can_like=1')
        import vk_action_availability as availability
        with self.assertRaises(engine.StopRun):availability.confirmed_denial(self.page,101,11,self.stamp,'like',change)

    def test_platform_warning_or_identity_failure_propagates_without_skip(self):
        self.fixture({'user_likes':0,'can_like':0})
        import vk_action_availability as availability
        for error in (engine.PlatformRateLimited('429'),engine.StopRun('identity mismatch')):
            def guard():raise error
            with self.assertRaises(type(error)):availability.confirmed_denial(self.page,101,11,self.stamp,'like',guard)


if __name__=='__main__':unittest.main()
