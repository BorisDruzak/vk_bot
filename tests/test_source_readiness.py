"""Exact source readiness uses local HTML only, never VK or account profiles."""
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright
import vk_batch as engine
import vk_scheduled_action as scheduled


class SourceReadinessBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()

    def setUp(self):
        self.page=self.browser.new_page();self.addCleanup(self.page.close)

    def fixture(self,dates,delay=0):
        entries=[{'method':'wall.getById','response':{'items':[{'owner_id':-101,'id':11,'date':d} for d in dates]}}]
        import json
        self.page.set_content('''<article data-testid="post" data-post-id="-101_11"></article>
            <script>window.cur={apiPrefetchCache:[]};window.fixtureClicks=0;
            document.addEventListener('click',()=>window.fixtureClicks++);</script>''')
        self.page.evaluate('([entries,delay])=>setTimeout(()=>window.cur.apiPrefetchCache=entries,delay)',[entries,delay])

    def test_root_before_json_date_waits_without_renavigation_or_clicks(self):
        self.fixture([1792644694],600)
        with patch.object(engine,'go_to_post') as navigate:
            scheduled.source_ready(self.page,101,11,1792644694,lambda:None)
        navigate.assert_called_once()
        self.assertEqual(self.page.evaluate('window.fixtureClicks'),0)

    def test_missing_main_footer_stops_before_attempt_and_preserves_pending(self):
        import tempfile
        from pathlib import Path
        from contextlib import closing
        from types import SimpleNamespace
        self.page.set_content('''<article><div data-testid="post" data-post-id="-101_11"
          style="width:500px;height:100px">Post without action toolbar</div></article>
          <script>window.fixtureClicks=0;document.addEventListener('click',()=>window.fixtureClicks++);
          window.cur={apiPrefetchCache:[{method:'wall.getById',response:{items:[{
            owner_id:-101,id:11,date:1792644694,likes:{user_likes:0},reposts:{user_reposted:0}
          }]}}]};</script>''')
        with tempfile.TemporaryDirectory() as tmp,closing(engine.db_open(Path(tmp)/'actor.sqlite3')) as db:
            engine.add_posts(db,101,[11])
            row=db.execute('SELECT * FROM posts WHERE group_id=101 AND post_id=11').fetchone()
            safety=SimpleNamespace(reserve=lambda *args:self.fail('No reservation is allowed without the exact footer'))
            with self.assertRaises(engine.StopRun):
                engine.process_one(self.page,db,101,row,0,safety=safety,actions={'like'},already_open=True)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT like_status,repost_status FROM posts').fetchone()[:],('pending','pending'))
        self.assertEqual(self.page.evaluate('window.fixtureClicks'),0)

    def test_exact_mismatch_and_conflict_never_wait_for_a_preferred_date(self):
        for dates,code in (([1792644693],'source_date_mismatch'),([1792644694,1792644693],'source_date_conflict')):
            with self.subTest(code=code):
                self.fixture(dates);self.page.wait_for_timeout(20)
                with patch.object(engine,'go_to_post'),self.assertRaises(scheduled.ScheduledActionStopped) as exc:
                    scheduled.source_ready(self.page,101,11,1792644694,lambda:None)
                self.assertEqual(exc.exception.reason_code,'action_stopped_'+code)

    def test_platform_warning_during_date_wait_stops_immediately(self):
        self.fixture([]);checks=[]
        def guard():
            checks.append(1)
            if len(checks)==2:raise engine.PlatformRateLimited('offline 429')
        with patch.object(engine,'go_to_post') as navigate,self.assertRaises(engine.PlatformRateLimited):
            scheduled.source_ready(self.page,101,11,1792644694,guard)
        navigate.assert_called_once();self.assertEqual(len(checks),2)
        self.assertEqual(self.page.evaluate('window.fixtureClicks'),0)

    def test_after_like_waits_for_real_footer_in_local_dom_without_click(self):
        self.page.set_content('''<div id="page-single-post"><article>
            <div data-testid="post" data-post-id="-101_11" data-post-nesting-lvl="0">
              <div data-testid="post_footer_action_like" data-user-likes="false" style="width:40px;height:30px">Like</div>
            </div></article></div><script>window.fixtureClicks=0;
            document.addEventListener('click',()=>window.fixtureClicks++);
            window.cur={apiPrefetchCache:[{method:'wall.getById',response:{items:[
              {owner_id:-101,id:11,likes:{user_likes:1},reposts:{user_reposted:0}}]}}]};
            setTimeout(()=>document.querySelector('[data-user-likes]').setAttribute('data-user-likes','true'),600);
            </script>''')
        with patch.object(engine,'go_to_post') as navigate:
            result=engine.after_action_state(self.page,101,11,action='like')
        self.assertTrue(result['liked']);self.assertTrue(result['main_post_liked'])
        navigate.assert_called_once();self.assertEqual(self.page.evaluate('window.fixtureClicks'),0)


if __name__=='__main__':unittest.main()
