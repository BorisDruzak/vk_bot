"""Regression tests for platform throttling, safe stop and scan progress.

Offline tests only: no authenticated VK, no real requests or reactions.
"""
import io
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import vk_batch as engine
import vk_groups as multi
from playwright.sync_api import sync_playwright


class UiRateLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            headless=True, args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def test_vk_visible_error_is_rate_limit_and_not_post_missing(self):
        self.page.set_content('''<div id="page-single-post">
             <h2>Не удалось загрузить посты</h2>
             <p>Too many requests per second</p><button>Попробовать снова</button>
             </div>''')
        self.assertTrue(self.page.evaluate(engine.PROTECTION_JS).startswith('VK_RATE_LIMITED:'))
        with self.assertRaises(engine.PlatformRateLimited):
            engine.ensure_ok(self.page)
        self.assertEqual(self.page.evaluate(engine.POST_PAGE_ACCESS_JS, {'groupId':203298456,'postId':1711}),
                         {'kind':'rate_limited'})

    def test_term_inside_normal_post_does_not_trigger_false_positive(self):
        self.page.set_content('''<article><div data-testid="post" data-post-id="-188_1">
        <p>Lecture about the error Too many requests per second and its causes.</p>
        </div></article>''')
        self.assertIsNone(self.page.evaluate(engine.PROTECTION_JS))
        engine.ensure_ok(self.page)

    def test_http429_observer_triggers_stop_even_without_banner(self):
        # Simulate on_response directly; no real network traffic.
        mock_page = MagicMock()
        mock_page.evaluate.return_value = None
        engine.watch_rate_limit_responses(mock_page)
        callback = mock_page.on.call_args.args[1]
        callback(MagicMock(status=200,url='https://vk.com/al_feed.php'))
        engine.ensure_ok(mock_page)
        callback(MagicMock(status=429,url='https://other.example.test/any'))
        engine.ensure_ok(mock_page)
        callback(MagicMock(status=429,url='https://vk.ru/al_wall.php'))
        with self.assertRaisesRegex(engine.PlatformRateLimited, '429'):
            engine.ensure_ok(mock_page)

    def test_platform_rate_limit_never_retries_a_post(self):
        page=MagicMock()
        page.url='https://vk.ru/wall-188206921_13376'
        page.wait_for_function.return_value.json_value.return_value={'kind':'rate_limited'}
        with patch.object(engine,'ensure_ok'):
            with self.assertRaises(engine.PlatformRateLimited):
                engine.go_to_post(page,188206921,13376)
        self.assertEqual(page.goto.call_count,1)
        page.locator.assert_not_called()


class PersistentRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.sqlite3'
        with closing(engine.db_open(self.path)) as db:
            multi.init_dates(db)
            engine.add_posts(db,188206921,[13412])
            multi.save_date(db,188206921,13412,int(datetime.now(timezone.utc).timestamp()),'mock-exact',True)
        self.selected='188206921'

    def _run(self, *args, readonly_failure=False, playwright=None):
        p=playwright or MagicMock()
        ctx=p.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
        ctx.pages=[MagicMock()]
        with patch.object(engine,'DB_PATH',self.path),patch.object(engine,'PROFILE',Path(self.temp.name)/'profile'),\
             patch.object(multi,'sync_playwright',p), redirect_stdout(io.StringIO()) as output:
            if readonly_failure:
                with patch.object(multi,'inspect_one',side_effect=engine.PlatformRateLimited('VK: Too many requests per second')):
                    code=multi.main(list(args))
            else:
                code=multi.main(list(args))
        return code,output.getvalue(),p

    def test_readonly_audit_rate_limit_latches_and_preserves_action_statuses(self):
        code,text,ptr=self._run('--audit','--only',self.selected,'--max-posts','1',readonly_failure=True)
        self.assertEqual(code,2)
        self.assertIn('[RATE LIMIT]',text)
        with closing(engine.db_open(self.path)) as db:
            self.assertIsNotNone(engine.safety_lock_state(db,0))
            rec=db.execute('SELECT like_status,repost_status FROM posts').fetchone()
            self.assertEqual(tuple(rec),('pending','pending'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM post_access').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
        code, text,ptr=self._run('--audit','--only',self.selected,'--max-posts','1')
        self.assertEqual(code,2)
        self.assertIn('блокировка',text)
        ptr.return_value.__enter__.return_value.chromium.launch_persistent_context.assert_not_called()
        # Local report remains available without any VK requests.
        code, report, _ = self._run('--report','--only',self.selected)
        self.assertEqual(code,0)
        self.assertIn('ЗАБЛОКИРОВАНО',report)

    def test_manual_mark_warning_does_not_open_browser(self):
        with patch.object(engine,'DB_PATH',self.path), patch.object(sys,'argv',['vk_batch.py','--mark-rate-limit']), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(engine.main(),0)
        self.assertIn('[RATE LIMIT]',out.getvalue())
        with closing(engine.db_open(self.path)) as db:
            self.assertIn('Too many requests',engine.safety_lock_state(db,0)['reason'])
        code,text,ptr=self._run('--audit','--only',self.selected,'--max-posts','1')
        self.assertEqual(code,2)
        ptr.return_value.__enter__.return_value.chromium.launch_persistent_context.assert_not_called()
        with patch.object(engine,'DB_PATH',self.path), patch.object(sys,'argv',['vk_batch.py','--ack-warning']), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(engine.main(),0)
        with closing(engine.db_open(self.path)) as db:
            self.assertIsNone(engine.safety_lock_state(db,0))

    def test_scan_lock_is_account_wide(self):
        with closing(engine.db_open(self.path)) as db:
            engine.lock_on_platform_warning(db,217724847,'VK throttled requests')
            with self.assertRaises(engine.StopRun):
                engine.require_platform_unlocked(db)
        code,text,p=self._run('--scan','--only',self.selected)
        self.assertEqual(code,2)
        p.return_value.__enter__.return_value.chromium.launch_persistent_context.assert_not_called()
        self.assertIn('блокировка',text)


class ScanRotationTests(unittest.TestCase):
    def test_scans_three_new_groups_then_the_next_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            with closing(engine.db_open(Path(tmp)/'state.sqlite3')) as db:
                multi.init_scan_progress(db)
                groups=[{'id':n} for n in range(1,8)]
                first=multi.choose_scan_groups(db,groups,3)
                self.assertEqual([g['id'] for g in first],[1,2,3])
                for g in first:
                    multi.mark_group_scanned(db,g['id'],5)
                second=multi.choose_scan_groups(db,groups,3)
                self.assertEqual([g['id'] for g in second],[4,5,6])
                for g in second:
                    multi.mark_group_scanned(db,g['id'],2)
                self.assertEqual([g['id'] for g in multi.choose_scan_groups(db,groups,3)][:1],[7])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM group_scan_progress').fetchone()[0],6)

    def test_config_has_conservative_scan_ceiling(self):
        cfg=multi.load_config()
        self.assertEqual(cfg['scan_groups_per_run'],3)
        self.assertEqual(len(cfg['groups']),64)

    def test_old_config_upgrade_keeps_full_group_list(self):
        import json
        cfg=multi.load_config()
        cfg.pop('scan_groups_per_run')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'old_groups.json'
            path.write_text(json.dumps(cfg,ensure_ascii=False),encoding='utf-8')
            loaded=multi.load_config(path)
            self.assertEqual(loaded['scan_groups_per_run'],3)
            self.assertEqual(len(loaded['groups']),64)
            self.assertNotIn('scan_groups_per_run',json.loads(path.read_text(encoding='utf-8')))


if __name__=='__main__':
    unittest.main()
