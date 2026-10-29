"""Error-handling regressions: no click on inaccessible pages, no false success."""
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright
from vk_batch import (
    POST_PAGE_ACCESS_JS, PostTemporaryError, PostUnavailable,
    StopRun, add_posts, audit_pending, db_open, get_tasks,
    get_audit_tasks, get_audited_missing_tasks, go_to_post,
    main, process_one, record_post_access, recheck_access_one, save_audit,
    status_write,
)

GROUP = 188206921


class AccessDbTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = db_open(Path(self.folder.name) / 'test.sqlite3')
        self.addCleanup(self.db.close)

    def test_unavailable_does_not_change_actions_or_rerun(self):
        add_posts(self.db, GROUP, [13368, 13369])
        status_write(self.db, GROUP, 13368, 'like', 'verified')
        record_post_access(self.db, GROUP, 13368, 'hidden', 'Пользователь скрыл страницу')
        self.assertEqual([r['post_id'] for r in get_tasks(self.db, GROUP, 0)], [13369])
        self.assertEqual([r['post_id'] for r in get_audit_tasks(self.db, GROUP, 0)], [13369])
        record = self.db.execute('SELECT like_status,repost_status FROM posts WHERE post_id=13368').fetchone()
        self.assertEqual(tuple(record), ('verified','pending'))

    def test_audited_queue_excludes_hidden(self):
        add_posts(self.db, GROUP, [13368, 13369])
        for pid in (13368, 13369):
            save_audit(self.db, GROUP, pid, False, False, 'inline')
        record_post_access(self.db, GROUP, 13368, 'hidden', 'VK: страница скрыта')
        self.assertEqual([r['post_id'] for r in get_audited_missing_tasks(self.db, GROUP, 0)], [13369])

    def test_recheck_access_only_clears_error_on_success(self):
        add_posts(self.db, GROUP, [13368])
        record_post_access(self.db, GROUP, 13368, 'hidden', 'Страница скрыта')
        with patch('vk_batch.go_to_post', side_effect=PostUnavailable('hidden', 'Всё ещё скрыта')):
            self.assertFalse(recheck_access_one(MagicMock(), self.db, GROUP, 13368))
        self.assertEqual(self.db.execute('SELECT state FROM post_access').fetchone()[0], 'hidden')
        with patch('vk_batch.go_to_post'):
            self.assertTrue(recheck_access_one(MagicMock(), self.db, GROUP, 13368))
        self.assertIsNone(self.db.execute('SELECT state FROM post_access').fetchone())
        self.assertEqual([r['post_id'] for r in get_tasks(self.db, GROUP, 0)], [13368])
        self.assertEqual(tuple(self.db.execute('SELECT like_status,repost_status FROM posts').fetchone()),
                         ('pending','pending'))

    def test_audit_skips_confirmed_hidden_and_keeps_next_post(self):
        add_posts(self.db, GROUP, [13368, 13369])
        def navigate(_page, _group_id, post_id):
            if post_id == 13368:
                raise PostUnavailable('hidden', 'Страница скрыта')
        with patch('vk_batch.go_to_post', side_effect=navigate), \
             patch('vk_batch.refresh_state', return_value={'liked':True,'reposted':True,'source':'inline'}), \
             patch('vk_batch.wait_post_footer_info', return_value={'liked':True}), \
             patch('vk_batch.ensure_ok'), patch('vk_batch.click_like') as like, \
             patch('vk_batch.click_share_then_publish') as share:
            stats = audit_pending(MagicMock(), self.db, GROUP, 2)
        self.assertEqual(stats['skipped_unavailable'], 1)
        self.assertEqual(stats['inspected'], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM post_access').fetchone()[0], 1)
        self.assertEqual(tuple(self.db.execute('SELECT like_status,repost_status FROM posts WHERE post_id=13369').fetchone()),
                         ('already','already'))
        self.assertEqual(tuple(self.db.execute('SELECT like_status,repost_status FROM posts WHERE post_id=13368').fetchone()),
                         ('pending','pending'))
        like.assert_not_called()
        share.assert_not_called()

    def test_post_action_unavailable_keeps_uncertain_and_fails_run(self):
        add_posts(self.db, GROUP, [13376])
        row = get_tasks(self.db, GROUP, 1)[0]
        pre = {'liked':False,'reposted':False,'source':'inline','canLike':True,'canRepost':True}
        with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state', return_value=pre), \
             patch('vk_batch.wait_post_footer_info', return_value={'liked':False}), \
             patch('vk_batch.ensure_ok'), patch('vk_batch.verified_like_button'), \
             patch('vk_batch.click_like'), patch('vk_batch.after_action_state',
                                                  side_effect=PostUnavailable('hidden', 'Страница скрыта')), \
             patch('vk_batch.time.sleep'):
            with self.assertRaisesRegex(StopRun, 'после клика') as cm:
                process_one(MagicMock(), self.db, GROUP, row, 0)
        self.assertNotIsInstance(cm.exception, PostUnavailable)
        self.assertEqual(self.db.execute('SELECT like_status FROM posts').fetchone()[0], 'uncertain')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM post_access').fetchone()[0], 0)


class NavigationTests(unittest.TestCase):
    def make_page(self, status):
        page = MagicMock()
        page.url = 'https://vk.com/wall-188206921_13368'
        page.wait_for_function.return_value.json_value.return_value = {'kind': status}
        return page

    def test_hidden_classified_without_28_second_wait_or_click(self):
        page = self.make_page('hidden')
        with patch('vk_batch.ensure_ok'):
            with self.assertRaises(PostUnavailable) as cm:
                go_to_post(page, GROUP, 13368)
        self.assertEqual(cm.exception.kind, 'hidden')
        self.assertEqual(page.goto.call_count, 1)
        page.locator.assert_not_called()
        self.assertEqual(page.wait_for_function.call_args.kwargs['timeout'], 16000)

    def test_temporary_retry_once_then_skip(self):
        page = self.make_page('temporary')
        with patch('vk_batch.ensure_ok'):
            with self.assertRaises(PostTemporaryError):
                go_to_post(page, GROUP, 13368)
        self.assertEqual(page.goto.call_count, 2)
        self.assertEqual(page.wait_for_timeout.call_count, 1)

    def test_protection_still_stops_before_skipping(self):
        page = self.make_page('hidden')
        with patch('vk_batch.ensure_ok', side_effect=StopRun('CAPTCHA')):
            with self.assertRaisesRegex(StopRun, 'CAPTCHA') as cm:
                go_to_post(page, GROUP, 13368)
        self.assertNotIsInstance(cm.exception, PostUnavailable)


class BatchContinuationTests(unittest.TestCase):
    def test_execute_skips_hidden_and_continues_another_post(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.sqlite3'
            db = db_open(path)
            add_posts(db, GROUP, [13368, 13369])
            db.close()
            playwright = MagicMock()
            context = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            context.pages = [MagicMock()]
            visited = []
            def process(_page, _db, _group, row, _pause, *, safety=None):
                visited.append(row['post_id'])
                if row['post_id'] == 13369:
                    raise PostUnavailable('hidden','VK сообщает, что страница скрыта')
                return True
            with patch.object(sys,'argv',['vk_batch.py','--execute','--skip-scan','--max-posts','2']), \
                 patch('vk_batch.PROFILE',Path(folder)/'profile'), \
                 patch('vk_batch.DB_PATH',path), \
                 patch('vk_batch.sync_playwright',playwright), \
                 patch('vk_batch.process_one',side_effect=process):
                self.assertEqual(main(),0)
            self.assertEqual(visited,[13369,13368])
            db = db_open(path)
            try:
                self.assertEqual(db.execute('SELECT state FROM post_access WHERE post_id=13369').fetchone()[0],
                                 'hidden')
                self.assertEqual([r['post_id'] for r in get_tasks(db,GROUP,0)],[13368])
                self.assertEqual(tuple(db.execute('SELECT like_status,repost_status FROM posts WHERE post_id=13369').fetchone()),
                                 ('pending','pending'))
            finally:
                db.close()

    def test_consecutive_temporary_failures_stop_and_preserve_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.sqlite3'
            db = db_open(path)
            add_posts(db, GROUP, [13368,13369,13370])
            db.close()
            playwright = MagicMock()
            context = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            context.pages = [MagicMock()]
            with patch.object(sys,'argv',['vk_batch.py','--execute','--skip-scan','--max-posts','3']), \
                 patch('vk_batch.PROFILE',Path(folder)/'profile'), \
                 patch('vk_batch.DB_PATH',path), \
                 patch('vk_batch.sync_playwright',playwright), \
                 patch('vk_batch.process_one',side_effect=PostTemporaryError('Сбой загрузки')) as process:
                self.assertEqual(main(),2)
                self.assertEqual(process.call_count,2)
            db = db_open(path)
            try:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM post_access').fetchone()[0],2)
                self.assertEqual(tuple(db.execute('SELECT like_status,repost_status FROM posts WHERE post_id=13370').fetchone()),
                                 ('pending','pending'))
            finally:
                db.close()


class BrowserProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True,
            args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_vk_hidden_error_text_recognized(self):
        page = self.browser.new_page()
        try:
            page.set_content('<main role="main"><h2>Ошибка</h2>'
                             '<p>Пользователь предпочёл скрыть эту страницу.</p></main>')
            result = page.evaluate(POST_PAGE_ACCESS_JS, {'groupId':GROUP,'postId':13368})
            self.assertEqual(result, {'kind':'hidden'})
        finally:
            page.close()

    def test_post_text_must_not_be_interpreted_as_hidden_error(self):
        page = self.browser.new_page()
        try:
            page.set_content('<main role="main"><div data-testid="post" '
                             'data-post-id="-188206921_13368" '
                             'style="width:400px;height:200px">'
                             '<p>Пользователь предпочёл скрыть эту страницу.</p></div></main>')
            result = page.evaluate(POST_PAGE_ACCESS_JS, {'groupId':GROUP,'postId':13368})
            self.assertEqual(result, {'kind':'ready'})
        finally:
            page.close()

    def test_other_post_cannot_satisfy_ready_state(self):
        page = self.browser.new_page()
        try:
            page.set_content('<main role="main"><div data-testid="post" '
                             'data-post-id="-188206921_13369" '
                             'style="width:400px;height:200px">другой пост</div></main>')
            self.assertIsNone(page.evaluate(POST_PAGE_ACCESS_JS, {'groupId':GROUP,'postId':13368}))
        finally:
            page.close()

if __name__ == '__main__':
    unittest.main()
