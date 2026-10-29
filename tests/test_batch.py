import sqlite3
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright
from vk_batch import (
    POST_STATE_JS, DISCOVER_JS, PROTECTION_JS, StopRun,
    add_posts, click_share_then_publish, click_like, db_open, get_tasks,
    process_one, status_write, validate_group_url, login_only, main,
    POST_ROOT_READY_JS, POST_DIAG_JS, go_to_post, inspect_post_only,
)


class BatchDataTests(unittest.TestCase):
    def test_url_rejects_non_vk(self):
        for url in ('http://vk.ru/group', 'https://example.com/group', 'https://vk.com/', 'https://vk.com/feed'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_group_url(url)
        self.assertEqual(validate_group_url('https://vk.ru/official_smr174'),
                         'https://vk.com/official_smr174')

    def test_state_db_resume_and_no_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder) / 'test.db')
            try:
                self.assertEqual(add_posts(db, 188206921, [13390, 13390, 13391]), 2)
                self.assertEqual(add_posts(db, 188206921, [13391]), 0)
                status_write(db, 188206921, 13390, 'like', 'already')
                status_write(db, 188206921, 13390, 'repost', 'verified')
                rows = get_tasks(db, 188206921, 0)
                self.assertEqual([r['post_id'] for r in rows], [13391])
                self.assertEqual(len(get_tasks(db, 188206921, 1)), 1)
            finally:
                db.close()

    def test_skip_existing_likes_and_reposts_without_clicks(self):
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder) / 'test.db')
            try:
                add_posts(db, 188206921, [13390])
                row = get_tasks(db, 188206921, 1)[0]
                fake = MagicMock()
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state', return_value={
                    'liked': True, 'reposted': True, 'source': 'wall.getById',
                    'canLike': True, 'canRepost': True,
                }), patch('vk_batch.wait_post_footer_info', return_value={'ok':True,'liked':True}), \
                     patch('vk_batch.click_like') as like, patch('vk_batch.click_share_then_publish') as share:
                    self.assertTrue(process_one(fake, db, 188206921, row, 0))
                    like.assert_not_called()
                    share.assert_not_called()
                stored = db.execute('SELECT like_status, repost_status FROM posts').fetchone()
                self.assertEqual(tuple(stored), ('already', 'already'))
            finally:
                db.close()

    def test_unknown_repost_refuses_click(self):
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder) / 'test.db')
            try:
                add_posts(db, 188206921, [13390])
                status_write(db, 188206921, 13390, 'like', 'already')
                row = get_tasks(db, 188206921, 1)[0]
                fake = MagicMock()
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state', return_value={
                    'liked': True, 'reposted': None, 'source': 'none',
                    'canLike': True, 'canRepost': None,
                }), patch('vk_batch.click_share_then_publish') as share:
                    with self.assertRaisesRegex(StopRun, 'guard_pre_action_state_unknown'):
                        process_one(fake, db, 188206921, row, 0)
                    share.assert_not_called()
                stored = db.execute('SELECT repost_status FROM posts').fetchone()[0]
                self.assertEqual(stored, 'uncertain')
            finally:
                db.close()

    def test_ambiguous_share_button_does_not_click(self):
        page = MagicMock()
        not_safe = dict(ok=False, reason='Несколько кнопок', likeElements=1,
                        visibleLikes=1, targetCount=2, liked=False)
        with patch('vk_batch.post_footer_info', return_value=not_safe), \
             patch('vk_batch.ensure_ok'):
            with self.assertRaisesRegex(StopRun, 'Нет безопасной кнопки share'):
                click_share_then_publish(page, 188206921, 13390)
        page.locator.assert_not_called()

    def test_uncertain_does_not_mark_done(self):
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder) / 'test.db')
            try:
                add_posts(db, 188206921, [13390])
                row = get_tasks(db, 188206921, 1)[0]
                page = MagicMock()
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state', return_value={
                    'liked': False, 'reposted': False, 'source': 'wall.getById',
                    'canLike': True, 'canRepost': True,
                }), patch('vk_batch.wait_post_footer_info', return_value={'ok':True,'liked':False}), \
                     patch('vk_batch.ensure_ok'), patch('vk_batch.verified_like_button'), \
                     patch('vk_batch.click_like'), \
                     patch('vk_batch.after_action_state', return_value={
                         'liked': False, 'reposted': False, 'source': 'wall.getById',
                         'canLike': True, 'canRepost': True, 'main_post_liked':False,
                     }), patch('vk_batch.time.sleep'):
                    with self.assertRaisesRegex(StopRun, 'не подтвердил'):
                        process_one(page, db, 188206921, row, 0)
                self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0], 'uncertain')
            finally:
                db.close()

class BrowserJSFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True,
                    args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_discovery_only_specified_group(self):
        page = self.browser.new_page()
        try:
            page.set_content('''
                <main role="main">
                  <div id="post-188206921_13390"><a href="/wall-188206921_13390">Пост</a></div>
                  <div data-post-id="-188206921_13389"><a href="/wall-188206921_13389">Пост</a></div>
                  <div id="post-123_98"><a href="/wall-123_98">Пост</a></div>
                  <a href="/wall-188206921_13388?from=share">Ссылка на третий</a>
                </main>
            ''')
            found = page.evaluate(DISCOVER_JS, {'groupId': 188206921})
            self.assertEqual(sorted(found['ids']), [13388, 13389, 13390])
        finally:
            page.close()

    def test_post_state_from_prefetch_not_token(self):
        page = self.browser.new_page()
        try:
            page.set_content('''
                <script>window.cur={apiPrefetchCache:[{
                  method:'wall.getById',request:{posts:'-188206921_13390'},
                  response:{items:[{
                    id:13390,owner_id:-188206921,
                    likes:{user_likes:0,can_like:1,repost_disabled:false},
                    reposts:{user_reposted:1}
                  }]}}
                ]};</script>
            ''')
            result = page.evaluate(POST_STATE_JS, {'groupId': 188206921, 'postId':13390})
            self.assertEqual(result, {'liked':False,'reposted':True,'canLike':True,
                                      'canRepost':True,'source':'live'})
            self.assertEqual(page.evaluate(POST_STATE_JS, {'groupId': 188206921, 'postId':13391})['source'], 'none')
        finally:
            page.close()

    def test_automatic_like_and_repost_controls_on_offline_modal(self):
        """Comments and unrelated posts must stay untouched during both actions."""
        page = self.browser.new_page(viewport={'width':1320, 'height':900})
        try:
            page.set_content("""
              <article id="post-123_12" style="width:500px;height:50px">
                <button aria-label="Поставить лайк" data-testid="post_footer_action_like"
                        data-user-likes="false">Лайк другого поста</button>
                <button aria-label="99 Поделиться">Чужой репост</button>
              </article>
              <section id="page-single-post">
                <div><article id="post-188206921_13390" style="width:550px;height:130px">
                  <div class="post-footer" style="display:flex;gap:20px;padding:10px">
                    <div role="button" tabindex="-1" aria-hidden="true"
                      data-testid="post_footer_action_like" data-user-likes="false"
                      aria-label="Отправить реакцию «Лайк»"
                      onclick="this.dataset.clicked='1';this.setAttribute('data-user-likes','true');this.setAttribute('aria-label','Убрать реакцию «Лайк»')">Лайк</div>
                    <button aria-label="6 Поделиться" onclick="document.querySelector('#share').style.display='block'">6 Поделиться</button>
                  </div>
                </article></div>
                <div id="comments"><button aria-label="Поставить лайк" data-comment-id="555"
                    onclick="this.dataset.clicked='1'">Лайк комментария</button></div>
              </section>
              <section id="share" role="dialog" style="display:none;width:450px;height:480px;position:fixed;right:30px;top:30px;background:white">
                <h2>Поделиться</h2>
                <label><input type="radio" name="dest"> <span>На своей стене</span></label>
                <label><input type="radio" name="dest" checked> <span>В личном сообщении</span></label>
                <div data-testid="modalfooter-actions">
                  <button data-testid="share_tab_submit" onclick="document.body.dataset.shared='1'">Поделиться</button>
                </div>
              </section>
            """)
            click_like(page, 188206921, 13390)
            self.assertEqual(page.locator('#post-188206921_13390 [data-testid="post_footer_action_like"]').get_attribute('data-clicked'), '1')
            self.assertIsNone(page.locator('[data-comment-id="555"]').get_attribute('data-clicked'))
            self.assertIsNone(page.locator('#post-123_12 button').first.get_attribute('data-clicked'))
            click_share_then_publish(page, 188206921, 13390)
            self.assertEqual(page.locator('body').get_attribute('data-shared'), '1')
            self.assertTrue(page.locator('#share input').first.is_checked())
            self.assertFalse(page.locator('#share input').nth(1).is_checked())
        finally:
            page.close()

    def test_vks_protection_warning_stops(self):
        page = self.browser.new_page()
        try:
            page.set_content('<p>Действие временно невозможно, поскольку вы совершили слишком много однотипных действий</p>')
            self.assertIsNotNone(page.evaluate(PROTECTION_JS))
            page.set_content('<p>Обычная запись сообщества</p>')
            self.assertIsNone(page.evaluate(PROTECTION_JS))
        finally:
            page.close()


if __name__ == '__main__':
    unittest.main()


class LoginOnlyTests(unittest.TestCase):
    def test_keeps_browser_open_until_operator_done(self):
        pw = MagicMock()
        context = pw.chromium.launch_persistent_context.return_value
        page = MagicMock()
        page.url = "https://vk.com/feed"
        page.is_closed.return_value = False
        context.pages = [page]
        with patch("builtins.input", return_value="ГОТОВО") as user_input:
            self.assertEqual(login_only(pw), 0)
        user_input.assert_called_once()
        page.goto.assert_called_once_with(
            "https://vk.com/feed", wait_until="domcontentloaded", timeout=30000
        )
        context.close.assert_called_once()

    def test_login_only_main_does_not_scan_or_open_database(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(sys, "argv", ["vk_batch.py", "--login-only"]), \
                 patch("vk_batch.PROFILE", Path(temp)), \
                 patch("vk_batch.sync_playwright") as pw, \
                 patch("vk_batch.login_only", return_value=0) as login, \
                 patch("vk_batch.db_open") as db, \
                 patch("vk_batch.scan_wall") as scan:
                self.assertEqual(main(), 0)
                login.assert_called_once_with(pw.return_value.__enter__.return_value)
                db.assert_not_called()
                scan.assert_not_called()

    def test_login_only_status_does_not_print_query_parameters(self):
        pw = MagicMock()
        context = pw.chromium.launch_persistent_context.return_value
        page = MagicMock()
        page.url = "https://vk.com/feed?access_token=SENSITIVE"
        page.is_closed.return_value = False
        context.pages = [page]
        with patch("builtins.input", side_effect=["СТАТУС", "ВЫХОД"]), \
             patch("builtins.print") as printed:
            self.assertEqual(login_only(pw), 1)
        output = "\n".join(str(x) for x in printed.call_args_list)
        self.assertNotIn("SENSITIVE", output)
        self.assertIn("vk.com/feed", output)
        context.close.assert_called_once()


class PostLocatorRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True,
                    args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_exact_data_post_id_recognized(self):
        page = self.browser.new_page()
        try:
            page.set_content('<main><div data-post-id="-188206921_13408" ' +
                 'style="height:180px;width:600px">Публикация</div></main>')
            self.assertTrue(page.evaluate(POST_ROOT_READY_JS,
                {'groupId':188206921,'postId':13408}))
            info = page.evaluate(POST_DIAG_JS,
                {'groupId':188206921,'postId':13408})
            self.assertTrue(info['visibleRoot'])
            self.assertFalse(info['idRoot'])
            self.assertTrue(info['dataRoot'])
        finally:
            page.close()

    def test_exact_id_root_recognized(self):
        page = self.browser.new_page()
        try:
            page.set_content('<div id="post-188206921_13408" ' +
                             'style="height:180px;width:600px">Публикация</div>')
            self.assertTrue(page.evaluate(POST_ROOT_READY_JS,
                {'groupId':188206921,'postId':13408}))
            info = page.evaluate(POST_DIAG_JS,
                {'groupId':188206921,'postId':13408})
            self.assertTrue(info['visibleRoot'])
            self.assertTrue(info['idRoot'])
        finally:
            page.close()

    def test_go_to_post_waits_exact_root_not_old_id_locator(self):
        page = MagicMock()
        page.url = 'https://vk.com/wall-188206921_13408'
        page.wait_for_function.return_value.json_value.return_value = {'kind':'ready'}
        with patch('vk_batch.ensure_ok'):
            go_to_post(page, 188206921, 13408)
        page.wait_for_function.assert_called_once()
        self.assertEqual(page.wait_for_function.call_args.kwargs['arg'],
                         {'groupId':188206921,'postId':13408})
        page.locator.assert_not_called()

    def test_neighbor_post_never_satisfies_target(self):
        page = self.browser.new_page()
        try:
            page.set_content('<div data-post-id="-188206921_13409" style="height:180px;width:600px">Другой пост</div>')
            self.assertFalse(page.evaluate(POST_ROOT_READY_JS,
                {'groupId': 188206921,'postId': 13408}))
            self.assertFalse(page.evaluate(POST_DIAG_JS,
                {'groupId': 188206921,'postId': 13408})['visibleRoot'])
        finally:
            page.close()

    def test_diagnostic_never_clicks(self):
        page = MagicMock()
        page.evaluate.return_value = {'host':'vk.com', 'path':'/wall-188206921_13408',
            'correctRoute':True,'documentState':'complete','idRoot':False,'dataRoot':False,
            'visibleRoot':False,'matchingLinks':0,'skeletons':1}
        footer = {'ok':False,'liked':None,'visibleLikes':0,'ariaHidden':None,
                  'reason':'Точный контейнер поста не найден'}
        with patch('vk_batch.ensure_ok'), patch('vk_batch.refresh_state', return_value={
                'liked':None, 'reposted':None, 'source':'none'}), \
             patch('vk_batch.post_footer_info',return_value=footer), \
             patch('builtins.input', return_value=''):
            inspect_post_only(page, 188206921, 13408)
        page.mouse.click.assert_not_called()
        page.locator.assert_not_called()

class StateRecoveryRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True,
                    args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_prefetch_inline_fallback_after_cur_cleared(self):
        import json
        page = self.browser.new_page()
        try:
            prefetch = [
                {"method": "wall.getById", "response": {"items": [
                    {"id":13408,"owner_id":-188206921,
                     "likes":{"user_likes":0,"can_like":1,"repost_disabled":False},
                     "reposts":{"user_reposted":0}},
                    {"id":13407,"owner_id":-188206921,
                     "likes":{"user_likes":1},"reposts":{"user_reposted":1}},
                ]}},
                {"method":"likes.isLiked","request":{
                    "item_id":"13408", "owner_id":"-188206921", "type":"post"},
                 "response":{"liked":0,"copied":0}},
            ]
            script = 'window.cur.apiPrefetchCache = '+json.dumps(prefetch)+';\nwindow.apiPrefetchReadyResolve();'
            page.set_content('<script>'+script+'</script>')
            page.evaluate('''() => {window.cur = {apiPrefetchCache:[]}}''')
            result = page.evaluate(POST_STATE_JS, {'groupId':188206921,'postId':13408})
            self.assertEqual(result, {'liked': False,'reposted':False,
                'canLike':True,'canRepost':True,'source':'inline'})
            self.assertEqual(page.evaluate(POST_STATE_JS, {'groupId':188206921,'postId':13409})['source'], 'none')
        finally:
            page.close()

    def test_token_never_leaves_page(self):
        import json
        page = self.browser.new_page()
        try:
            prefetch = [{"method":"wall.getById","response":{"items":[{
                "owner_id":-188206921,"id":13408,
                "likes":{"user_likes":1},"reposts":{"user_reposted":0}}]}}]
            page.set_content('''<script>window.vk={webToken:{access_token:"DUMMY_SECRET_FOR_TEST"}};</script>'''
              +'<script>window.cur.apiPrefetchCache = '+json.dumps(prefetch)+';\nwindow.apiPrefetchReadyResolve();</script>')
            returned = page.evaluate(POST_STATE_JS, {'groupId':188206921,'postId':13408})
            self.assertEqual(returned['liked'], True)
            self.assertEqual(returned['reposted'], False)
            self.assertNotIn('DUMMY_SECRET_FOR_TEST',repr(returned))
        finally:
            page.close()

    def test_live_vs_inline_conflict_fails_closed(self):
        import json
        page = self.browser.new_page()
        try:
            obj=lambda n: [{"method":"wall.getById","response":{"items":[{
                "owner_id":-188206921,"id":13408,"likes":{"user_likes":n},
                "reposts":{"user_reposted":0}}]}}]
            page.set_content('<script>window.cur.apiPrefetchCache = '+json.dumps(obj(0))+';\nwindow.apiPrefetchReadyResolve();</script>')
            page.evaluate('(arg)=>{window.cur={apiPrefetchCache:arg}}',obj(1))
            returned = page.evaluate(POST_STATE_JS, {'groupId':188206921,'postId':13408})
            self.assertIsNone(returned['liked'])
            self.assertEqual(returned['source'], 'conflict')
        finally:
            page.close()

    def test_short_label_like_unique(self):
        from vk_batch import verified_like_button
        page = MagicMock()
        info={'ok':True,'liked':False,'likeElements':1,'visibleLikes':1,'targetCount':1,
              'reason':'','ariaHidden':None}
        with patch('vk_batch.post_footer_info', return_value=info), \
             patch('vk_batch.ensure_ok'):
            self.assertIs(verified_like_button(page, 188206921, 13408),
                page.locator.return_value)
            page.locator.assert_called_once_with('[data-vk-batch-footer-action="like"]')

    def test_safe_recheck_before_click_updates_only_untouched(self):
        from vk_batch import recheck_one
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder)/'test.db')
            try:
                add_posts(db,188206921,[13408])
                status_write(db,188206921,13408,'like','uncertain',
                             'Не удалось проверить состояние до клика')
                fake = MagicMock()
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state',return_value={
                    'liked':False,'reposted':False,'source':'inline',
                    'canLike':True,'canRepost':True}):
                    recheck_one(fake,db,188206921,13408)
                self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0], 'pending')
            finally:
                db.close()

    def test_recheck_does_not_rearm_uncertainty_after_click(self):
        from vk_batch import recheck_one
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder)/'test.db')
            try:
                add_posts(db,188206921,[13408])
                status_write(db,188206921,13408,'like','uncertain',
                             'Клик выполнен, подтверждение VK ещё не получено')
                fake = MagicMock()
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state',return_value={
                    'liked':False,'reposted':False,'source':'inline',
                    'canLike':True,'canRepost':True}):
                    recheck_one(fake,db,188206921,13408)
                self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0], 'uncertain')
            finally:
                db.close()

class RecheckCliRegressionTests(unittest.TestCase):
    def test_recheck_mode_calls_only_readonly_recheck_not_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            dummy_ctx = MagicMock()
            dummy_ctx.pages = [MagicMock()]
            pw = MagicMock()
            pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = dummy_ctx
            with patch.object(sys, 'argv', ['vk_batch.py','--recheck-post','13408']), \
                 patch('vk_batch.PROFILE', Path(folder)/'profile'), \
                 patch('vk_batch.DB_PATH', Path(folder)/'test.sqlite3'), \
                 patch('vk_batch.sync_playwright',pw), \
                 patch('vk_batch.recheck_one') as recheck, \
                 patch('vk_batch.scan_wall') as scan, \
                 patch('vk_batch.process_one') as action:
                self.assertEqual(main(),0)
                self.assertEqual(recheck.call_args.args[2:],(188206921,13408))
                scan.assert_not_called()
                action.assert_not_called()


class ExactPostSelectionTests(unittest.TestCase):
    """A targeted test must never silently fall back to the 321-post queue."""

    def test_explicit_post_selects_only_target_and_does_not_reset_existing_status(self):
        from vk_batch import target_row
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder) / 'test.sqlite3')
            try:
                add_posts(db, 188206921, [13408, 13407, 13376])
                status_write(db, 188206921, 13408, 'like', 'uncertain', 'Клик уже был')
                selected = target_row(db, 188206921, 13376)
                self.assertEqual(selected['post_id'], 13376)
                self.assertEqual((selected['like_status'], selected['repost_status']),
                                 ('pending', 'pending'))
                self.assertEqual(target_row(db, 188206921, 13408)['like_status'], 'uncertain')
            finally:
                db.close()

    def test_missing_target_is_added_only_for_execute(self):
        from vk_batch import target_row
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder) / 'test.sqlite3')
            try:
                self.assertIsNone(target_row(db, 188206921, 13376))
                self.assertEqual(db.execute('SELECT count(*) FROM posts').fetchone()[0], 0)
                selected = target_row(db, 188206921, 13376, create=True)
                self.assertEqual(selected['post_id'], 13376)
                self.assertEqual(db.execute('SELECT count(*) FROM posts').fetchone()[0], 1)
            finally:
                db.close()

    def test_exact_post_cli_works_without_scan_or_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            pw = MagicMock()
            fake_context = pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            fake_page = MagicMock()
            fake_context.pages = [fake_page]
            with patch.object(sys, 'argv', ['vk_batch.py', '--post-id', '13376', '--execute']), \
                 patch('vk_batch.PROFILE', Path(folder) / 'profile'), \
                 patch('vk_batch.DB_PATH', Path(folder) / 'test.sqlite3'), \
                 patch('vk_batch.sync_playwright', pw), \
                 patch('vk_batch.scan_wall') as scan, \
                 patch('vk_batch.get_tasks') as tasks, \
                 patch('vk_batch.process_one', return_value=True) as action:
                self.assertEqual(main(), 0)
                scan.assert_not_called()
                tasks.assert_not_called()
                action.assert_called_once()
                self.assertEqual(action.call_args.args[3]['post_id'], 13376)
                self.assertEqual(action.call_args.args[2], 188206921)

    def test_read_only_target_neither_writes_db_nor_performs_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            pw = MagicMock()
            fake_context = pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            fake_context.pages = [MagicMock()]
            db_file = Path(folder) / 'test.sqlite3'
            with patch.object(sys, 'argv', ['vk_batch.py', '--post-id', '13376']), \
                 patch('vk_batch.PROFILE', Path(folder) / 'profile'), \
                 patch('vk_batch.DB_PATH', db_file), \
                 patch('vk_batch.sync_playwright', pw), \
                 patch('vk_batch.go_to_post') as goto, \
                 patch('vk_batch.refresh_state', return_value={'liked': False, 'reposted': False, 'source': 'inline'}) as read, \
                 patch('vk_batch.scan_wall') as scan, \
                 patch('vk_batch.process_one') as action:
                self.assertEqual(main(), 0)
                goto.assert_called_once()
                read.assert_called_once()
                scan.assert_not_called()
                action.assert_not_called()
            db = db_open(db_file)
            try:
                self.assertEqual(db.execute('SELECT count(*) FROM posts').fetchone()[0], 0)
            finally:
                db.close()

    def test_uncertain_target_stops_before_any_action(self):
        with tempfile.TemporaryDirectory() as folder:
            db_file = Path(folder) / 'test.sqlite3'
            db = db_open(db_file)
            add_posts(db, 188206921, [13376])
            status_write(db, 188206921, 13376, 'repost', 'uncertain', 'Клик ранее выполнен')
            db.close()
            pw = MagicMock()
            pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value.pages = [MagicMock()]
            with patch.object(sys, 'argv', ['vk_batch.py', '--post-id', '13376', '--execute']), \
                 patch('vk_batch.PROFILE', Path(folder) / 'profile'), \
                 patch('vk_batch.DB_PATH', db_file), \
                 patch('vk_batch.sync_playwright', pw), \
                 patch('vk_batch.process_one') as action, \
                 patch('vk_batch.scan_wall') as scan:
                self.assertEqual(main(), 2)
                action.assert_not_called()
                scan.assert_not_called()

    def test_safe_recheck_mark_already_when_clicked_state_now_true(self):
        from vk_batch import recheck_one
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder) / 'test.db')
            try:
                add_posts(db, 188206921, [13408])
                status_write(db, 188206921, 13408, 'like', 'uncertain',
                             'Клик выполнен, подтверждение VK ещё не получено')
                fake = MagicMock()
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state', return_value={
                    'liked': True, 'reposted': True, 'source': 'inline',
                    'canLike': True, 'canRepost': True}), \
                     patch('vk_batch.wait_post_footer_info', return_value={'ok':True,'liked':True}):
                    recheck_one(fake, db, 188206921, 13408)
                self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0], 'already')
            finally:
                db.close()


class ExactPostExecutionTests(unittest.TestCase):
    def test_missing_like_and_repost_on_target_are_verified_individually(self):
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder) / 'test.sqlite3')
            try:
                add_posts(db, 188206921, [13408, 13376])
                row = db.execute(
                    'SELECT post_id,like_status,repost_status FROM posts WHERE post_id=13376'
                ).fetchone()
                page = MagicMock()
                base = {'liked': False, 'reposted': False, 'canLike': True,
                        'canRepost': True, 'source': 'inline'}
                after_like = {**base, 'liked': True, 'main_post_liked':True}
                after_repost = {**base, 'liked': True, 'reposted': True, 'main_post_liked':True}
                with patch('vk_batch.go_to_post') as navigate, \
                     patch('vk_batch.refresh_state', return_value=base), \
                     patch('vk_batch.after_action_state', side_effect=[after_like, after_repost]), \
                     patch('vk_batch.wait_post_footer_info', return_value={'ok':True,'liked':False}), \
                     patch('vk_batch.verified_like_button') as preflight, \
                     patch('vk_batch.click_like') as like, \
                     patch('vk_batch.click_share_then_publish') as share, \
                     patch('vk_batch.ensure_ok'), patch('vk_batch.time.sleep'):
                    self.assertTrue(process_one(page, db, 188206921, row, 0))
                    navigate.assert_called_once_with(page, 188206921, 13376)
                    preflight.assert_called_once_with(page, 188206921, 13376)
                    like.assert_called_once_with(page, 188206921, 13376, prepared_button=preflight.return_value)
                    share.assert_called_once()
                    self.assertEqual(share.call_args.args, (page, 188206921, 13376))
                    self.assertTrue(callable(share.call_args.kwargs['before_publish']))
                self.assertEqual(tuple(db.execute(
                    'SELECT like_status,repost_status FROM posts WHERE post_id=13376'
                ).fetchone()), ('verified', 'verified'))
                self.assertEqual(tuple(db.execute(
                    'SELECT like_status,repost_status FROM posts WHERE post_id=13408'
                ).fetchone()), ('pending', 'pending'))
            finally:
                db.close()

class V65LiveLabelAndRecoveryTests(unittest.TestCase):
    """Regressions for the VK UI labels from post 13376 on 2026-10-20."""

    def test_post_13376_live_label_only_one_safe_like_candidate(self):
        """Select VK's post footer, not a comment button carrying the same label."""
        from vk_batch import verified_like_button
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                page = browser.new_page()
                page.set_content("""
                  <section id="page-single-post"><div>
                    <article id="post-188206921_13376" style="height:100px;width:350px">
                      <div style="display:flex;gap:10px;padding:10px">
                        <div role="button" data-testid="post_footer_action_like"
                             data-user-likes="false" aria-hidden="true"
                             aria-label="Отправить реакцию «Лайк»">Лайк</div>
                        <button aria-label="2 Поделиться">2 Поделиться</button>
                      </div>
                    </article></div>
                    <div id="comment"><button aria-label="Поставить лайк" data-comment-like="1">Лайк комментария</button></div>
                  </section>
                  <article id="post-188206921_13377" style="height:100px;width:350px">
                    <button aria-label="Поставить лайк">Поставить лайк</button>
                  </article>
                """)
                locator = verified_like_button(page, 188206921, 13376)
                self.assertEqual(locator.get_attribute('data-testid'), 'post_footer_action_like')
                self.assertEqual(locator.get_attribute('data-user-likes'), 'false')
                self.assertEqual(page.locator('[data-vk-batch-footer-action="like"]').count(), 1)
                self.assertIsNone(page.locator('[data-comment-like="1"]').get_attribute('data-vk-batch-footer-action'))
                page.close()
            finally:
                browser.close()

    def test_live_label_matcher_rejects_removal_and_other_reactions(self):
        """A generic Like label on a comment must never substitute for footer testid."""
        from vk_batch import verified_like_button
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                page=browser.new_page()
                page.set_content("""
                  <article id="post-188206921_13376" style="height:120px;width:350px">
                    <button data-testid="comment_like" aria-label="Поставить лайк">Поставить лайк</button>
                  </article>
                  """)
                with self.assertRaisesRegex(StopRun, 'Нет безопасной кнопки like'):
                    verified_like_button(page,188206921,13376)
                self.assertEqual(page.locator('[data-vk-batch-footer-action]').count(),0)
                page.close()
            finally:
                browser.close()

    def test_missing_like_control_keeps_pending_with_no_click(self):
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder) / 'test.sqlite3')
            try:
                add_posts(db, 188206921, [13376])
                row = db.execute('SELECT post_id,like_status,repost_status FROM posts').fetchone()
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state', return_value={
                    'liked':False, 'reposted':False, 'canLike':True, 'canRepost':True,
                    'source':'inline'}), patch('vk_batch.ensure_ok'), \
                     patch('vk_batch.wait_post_footer_info',side_effect=StopRun('Нет безопасной кнопки like основного поста')), \
                     patch('vk_batch.click_like') as like, patch('vk_batch.click_share_then_publish') as share:
                    with self.assertRaisesRegex(StopRun, 'Нет безопасной кнопки like'):
                        process_one(MagicMock(), db, 188206921, row, 0)
                    like.assert_not_called()
                    share.assert_not_called()
                stored = db.execute('SELECT like_status,repost_status FROM posts').fetchone()
                self.assertEqual(tuple(stored), ('pending','pending'))
            finally:
                db.close()

    def test_operator_acknowledgment_can_recover_old_preclick_uncertain(self):
        from vk_batch import recheck_one
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder) / 'test.sqlite3')
            try:
                add_posts(db, 188206921, [13376])
                status_write(db, 188206921, 13376, 'like', 'uncertain',
                             'Подготовка клика; требуется перепроверка')
                data = {'liked':False, 'reposted':False, 'source':'inline',
                        'canLike':True,'canRepost':True}
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state', return_value=data):
                    recheck_one(MagicMock(), db, 188206921, 13376)
                    self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0], 'uncertain')
                    recheck_one(MagicMock(), db, 188206921, 13376, confirm_no_click_like=True)
                    self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0], 'pending')
            finally:
                db.close()

    def test_operator_acknowledgment_cannot_recover_postclick_uncertainty(self):
        from vk_batch import recheck_one
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder) / 'test.sqlite3')
            try:
                add_posts(db, 188206921, [13376])
                status_write(db, 188206921, 13376, 'like', 'uncertain',
                             'Клик выполнен, подтверждение VK ещё не получено')
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state', return_value={
                    'liked':False,'reposted':False,'source':'inline',
                    'canLike':True, 'canRepost':True}):
                    recheck_one(MagicMock(), db, 188206921, 13376, confirm_no_click_like=True)
                    self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0], 'uncertain')
            finally:
                db.close()

class ConfirmNoClickCliTests(unittest.TestCase):
    def test_explicit_confirm_only_rechecks_one_post_without_scanning(self):
        with tempfile.TemporaryDirectory() as temp:
            pw = MagicMock()
            context = pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            context.pages = [MagicMock()]
            with patch.object(sys, 'argv', ['vk_batch.py', '--recheck-post', '13376', '--confirm-no-click-like']), \
                 patch('vk_batch.PROFILE', Path(temp) / 'profile'), \
                 patch('vk_batch.DB_PATH', Path(temp) / 'posts.sqlite3'), \
                 patch('vk_batch.sync_playwright', pw), \
                 patch('vk_batch.recheck_one') as recheck, \
                 patch('vk_batch.scan_wall') as scan, \
                 patch('vk_batch.process_one') as process:
                self.assertEqual(main(), 0)
                recheck.assert_called_once()
                self.assertEqual(recheck.call_args.args[2:], (188206921, 13376))
                self.assertEqual(recheck.call_args.kwargs, {'confirm_no_click_like': True,
                    'confirm_comment_click_like': False, 'confirm_repost_dialog_no_submit': False})
                scan.assert_not_called()
                process.assert_not_called()

    def test_preflight_waits_for_button_without_selecting_another_post(self):
        from vk_batch import verified_like_button
        page = MagicMock()
        empty = {'ok':False,'reason':'No footer','likeElements':0,'visibleLikes':0,'targetCount':0}
        good = {'ok':True,'liked':False,'likeElements':1,'visibleLikes':1,'targetCount':1}
        with patch('vk_batch.post_footer_info', side_effect=[empty, empty, good]), \
             patch('vk_batch.ensure_ok') as ensure:
            result = verified_like_button(page, 188206921, 13376)
        self.assertIs(result, page.locator.return_value)
        self.assertEqual(page.wait_for_timeout.call_count, 2)
        self.assertEqual(ensure.call_count, 3)
        page.locator.assert_called_once_with('[data-vk-batch-footer-action="like"]')



class WrongCommentLikeSafetyTests(unittest.TestCase):
    """Regression: v6.5 liked the comment below post 13376, not its main footer."""

    def test_operator_recovers_wrong_comment_click_without_touching_repost(self):
        from vk_batch import recheck_one
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder) / 'test.db')
            try:
                add_posts(db, 188206921, [13376, 13377])
                status_write(db, 188206921, 13376, 'like', 'verified', 'Old false positive')
                status_write(db, 188206921, 13376, 'repost', 'verified', 'Leave this alone')
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state', return_value={
                    'liked':False,'reposted':True,'source':'inline'
                }), patch('vk_batch.wait_post_footer_info', return_value={'ok':True,'liked':False}):
                    recheck_one(MagicMock(), db, 188206921, 13376, confirm_comment_click_like=True)
                row=db.execute('SELECT like_status,repost_status FROM posts WHERE post_id=13376').fetchone()
                self.assertEqual(tuple(row), ('pending','verified'))
                row=db.execute('SELECT like_status,repost_status FROM posts WHERE post_id=13377').fetchone()
                self.assertEqual(tuple(row), ('pending','pending'))
            finally:
                db.close()

    def test_refuse_recovery_if_post_flag_still_true_or_conflicts(self):
        from vk_batch import recheck_one
        for inline,dom in [(True,False),(False,True),(None,False)]:
            with self.subTest(inline=inline,dom=dom), tempfile.TemporaryDirectory() as folder:
                db=db_open(Path(folder)/'test.db')
                try:
                    add_posts(db,188206921,[13376])
                    status_write(db,188206921,13376,'like','uncertain','clicked comment')
                    with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state',return_value={
                        'liked':inline,'reposted':False,'source':'inline'
                    }), patch('vk_batch.wait_post_footer_info',return_value={'ok':True,'liked':dom}):
                        with self.assertRaisesRegex(StopRun,'Нельзя исправить состояние'):
                            recheck_one(MagicMock(),db,188206921,13376,confirm_comment_click_like=True)
                    self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0],'uncertain')
                finally:
                    db.close()

    def test_post_liked_conflict_never_marks_already_or_clicks(self):
        with tempfile.TemporaryDirectory() as folder:
            db=db_open(Path(folder)/'test.db')
            try:
                add_posts(db,188206921,[13376])
                row=get_tasks(db,188206921,1)[0]
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state',return_value={
                    'liked':True, 'reposted':False,'source':'inline','canLike':True,'canRepost':True,
                }), patch('vk_batch.wait_post_footer_info',return_value={'ok':True,'liked':False}), \
                  patch('vk_batch.click_like') as like, patch('vk_batch.click_share_then_publish') as share:
                    with self.assertRaisesRegex(StopRun,'не совпадает'):
                        process_one(MagicMock(),db,188206921,row,0)
                    like.assert_not_called()
                    share.assert_not_called()
                self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0],'pending')
            finally:
                db.close()

    def test_post_verification_requires_fresh_main_footer_true(self):
        with tempfile.TemporaryDirectory() as folder:
            db=db_open(Path(folder)/'test.db')
            try:
                add_posts(db,188206921,[13376])
                row=get_tasks(db,188206921,1)[0]
                base={'liked':False, 'reposted':False,'source':'inline','canLike':True,'canRepost':True}
                # SSR says like succeeded but the post footer is still unliked.
                false_success={**base,'liked':True,'main_post_liked':False}
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state',return_value=base), \
                     patch('vk_batch.wait_post_footer_info',return_value={'ok':True,'liked':False}), \
                     patch('vk_batch.verified_like_button'), patch('vk_batch.click_like'), \
                     patch('vk_batch.after_action_state',return_value=false_success), \
                     patch('vk_batch.ensure_ok'),patch('vk_batch.time.sleep'):
                    with self.assertRaisesRegex(StopRun,'не подтвердил'):
                        process_one(MagicMock(),db,188206921,row,0)
                self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0],'uncertain')
            finally:
                db.close()

    def test_missing_post_footer_fails_even_if_comment_like_is_visible(self):
        from vk_batch import verified_like_button
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--no-sandbox'])
            try:
                page=browser.new_page()
                page.set_content('''<section id="page-single-post"><div><article id="post-188206921_13376">
                    <button data-testid="post_footer_action_like" data-user-likes="false" style="display:none">Лайк</button>
                  </article></div><div class="comment"><button aria-label="Поставить лайк">Лайк комментария</button></div></section>''')
                with self.assertRaisesRegex(StopRun,'Нет безопасной кнопки like'):
                    verified_like_button(page,188206921,13376)
                self.assertEqual(page.locator('[data-vk-batch-footer-action]').count(),0)
            finally:
                browser.close()

    def test_comment_share_is_not_same_toolbar_as_main_post(self):
        from vk_batch import post_footer_info
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--no-sandbox'])
            try:
                page=browser.new_page(viewport={'width':1000,'height':750})
                page.set_content('''<section id="page-single-post"><div><article id="post-188206921_13376">
                    <div style="padding:10px;height:45px"><div role="button" data-testid="post_footer_action_like" data-user-likes="false">Лайк</div></div>
                    <div style="padding:10px;margin-top:90px"><button aria-label="2 Поделиться">Поделиться комментарием</button></div>
                  </article></div></section>''')
                result=post_footer_info(page,188206921,13376,'share')
                self.assertFalse(result['ok'])
                self.assertEqual(result['targetCount'],0)
            finally:
                browser.close()

class RealVkLongPostRegressionTests(unittest.TestCase):
    """The 2026-10-20 VK DOM uses a nested repost and an off-screen main footer."""

    def test_long_post_offscreen_footer_scrolls_but_nested_repost_and_comment_do_not(self):
        from vk_batch import post_footer_info, verified_like_button
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True,
                args=['--no-sandbox'])
            try:
                page = browser.new_page(viewport={'width':900,'height':500})
                page.set_content('''
                <section id="page-single-post"><div><article>
                  <div data-testid="post" data-post-id="-188206921_13376" data-post-nesting-lvl="0">
                    <div data-testid="post-content-container" style="height:1250px">
                      <div data-testid="post" data-post-id="-34888580_398036" data-post-nesting-lvl="1">
                        <div role="button" data-testid="post_footer_action_like" data-user-likes="false"
                             aria-label="Отправить реакцию «Лайк»">Лайк вложенного репоста</div>
                        <div role="button" data-testid="post_footer_action_share" aria-label="99 Поделиться">Поделиться вложенным</div>
                      </div>
                    </div>
                    <div class="footer" style="display:flex;gap:14px;padding:10px">
                      <div role="button" data-testid="post_footer_action_like" data-user-likes="false"
                         aria-hidden="true" aria-label="Отправить реакцию «Лайк»"
                         onclick="this.setAttribute('data-user-likes','true')">Лайк поста</div>
                      <div role="button" data-testid="post_footer_action_share" aria-label="2 Поделиться">Поделиться</div>
                    </div>
                  </div>
                  <div data-testid="wall_comments_comment_root">
                     <div role="button" data-testid="comment-like" aria-label="Поставить лайк"
                          onclick="this.dataset.clicked='true'">Лайк комментария</div>
                  </div>
                </article></div>
                <div data-testid="post-feed-continuation-section"><article>
                 <div data-testid="post" data-post-id="-188206921_13409" data-post-nesting-lvl="0">
                   <div role="button" data-testid="post_footer_action_like" data-user-likes="false">Пост ленты</div>
                 </div>
                </article></div>
                </section>''')
                # This is what v6.6 incorrectly called not visible, since it was below the fold.
                self.assertGreater(page.locator('.footer').bounding_box()['y'], 500)
                read = post_footer_info(page,188206921,13376,'like')
                self.assertTrue(read['ok'],read)
                self.assertEqual(read['likeElements'],1)
                self.assertEqual(read['visibleLikes'],1)
                self.assertEqual(read['liked'],False)
                self.assertEqual(read['ariaHidden'],'true')
                share = post_footer_info(page,188206921,13376,'share')
                self.assertTrue(share['ok'],share)
                self.assertEqual(share['targetCount'],1)
                button = verified_like_button(page,188206921,13376)
                self.assertGreater(page.evaluate('scrollY'),500)
                self.assertEqual(button.get_attribute('data-testid'),'post_footer_action_like')
                button.click()
                self.assertEqual(button.get_attribute('data-user-likes'),'true')
                self.assertIsNone(page.locator('[data-testid="comment-like"]').get_attribute('data-clicked'))
                nested = page.locator('[data-post-id="-34888580_398036"] [data-testid="post_footer_action_like"]')
                self.assertEqual(nested.get_attribute('data-user-likes'),'false')
                other = post_footer_info(page,188206921,13409,'like')
                self.assertFalse(other['ok'])
                page.close()
            finally:
                browser.close()

    def test_two_main_footer_controls_fail_closed(self):
        from vk_batch import post_footer_info
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--no-sandbox'])
            try:
                page=browser.new_page()
                page.set_content('''<div id="page-single-post"><div><article>
                 <div data-testid="post" data-post-id="-188206921_13376" data-post-nesting-lvl="0">
                  <div style="padding:10px"><div role="button" data-testid="post_footer_action_like" data-user-likes="false">1</div></div>
                  <div style="padding:10px"><div role="button" data-testid="post_footer_action_like" data-user-likes="false">2</div></div>
                 </div></article></div></div>''')
                result=post_footer_info(page,188206921,13376,'like')
                self.assertFalse(result['ok'])
                self.assertEqual(result['likeElements'],2)
                page.close()
            finally:
                browser.close()

class RepostModalRegressionV622(unittest.TestCase):
    """UI fixtures and action-ledger invariants for failed repost modal."""

    def test_popup_opening_without_submit_leaves_pending_and_budget_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            db = db_open(Path(folder)/'state.db')
            try:
                add_posts(db, 188206921, [13418])
                status_write(db, 188206921, 13418, 'like', 'verified')
                row = db.execute('SELECT * FROM posts WHERE group_id=? AND post_id=?',
                                 (188206921,13418)).fetchone()
                budget=MagicMock()
                state={'liked':True,'reposted':False,'source':'inline',
                       'canLike':True,'canRepost':True}
                with patch('vk_batch.go_to_post'), patch('vk_batch.ensure_ok'), \
                     patch('vk_batch.refresh_state',return_value=state), \
                     patch('vk_batch.click_share_then_publish',
                           side_effect=StopRun('Не найдены обе радиокнопки назначения')):
                    with self.assertRaisesRegex(StopRun,'радиокнопки'):
                        process_one(MagicMock(),db,188206921,row,0,
                                    safety=budget,actions={'repost'})
                self.assertEqual(db.execute('SELECT repost_status FROM posts').fetchone()[0], 'pending')
                budget.reserve.assert_not_called()
            finally:
                db.close()

    def test_final_submit_callback_marks_uncertain_and_reserves_once(self):
        with tempfile.TemporaryDirectory() as folder:
            db=db_open(Path(folder)/'state.db')
            try:
                add_posts(db, 188206921,[13418]); status_write(db,188206921,13418,'like','verified')
                row=db.execute('SELECT * FROM posts').fetchone()
                state={'liked':True,'reposted':False,'source':'inline','canLike':True,'canRepost':True}
                budget=MagicMock()
                def simulate_submit(page,gid,pid,*,before_publish=None):
                    self.assertIsNotNone(before_publish)
                    self.assertEqual(db.execute('SELECT repost_status FROM posts').fetchone()[0],'pending')
                    before_publish()
                    self.assertEqual(db.execute('SELECT repost_status FROM posts').fetchone()[0],'uncertain')
                    raise StopRun('Упали сразу после попытки финального клика')
                with patch('vk_batch.go_to_post'), patch('vk_batch.ensure_ok'),\
                     patch('vk_batch.refresh_state',return_value=state),\
                     patch('vk_batch.click_share_then_publish',side_effect=simulate_submit):
                    with self.assertRaisesRegex(StopRun,'Упали сразу'):
                        process_one(MagicMock(),db,188206921,row,0,
                                    safety=budget,actions={'repost'})
                budget.reserve.assert_called_once_with(13418,'repost')
                self.assertEqual(db.execute('SELECT repost_status FROM posts').fetchone()[0],'uncertain')
            finally:
                db.close()

    def test_recover_v621_pre_submit_error_requires_confirmed_no_repost(self):
        import vk_batch
        with tempfile.TemporaryDirectory() as folder:
            db=db_open(Path(folder)/'state.db')
            try:
                add_posts(db,188206921,[13418]);status_write(db,188206921,13418,'like','verified')
                status_write(db,188206921,13418,'repost','uncertain',
                             'Подготовка клика; требуется перепроверка')
                state={'liked':True,'reposted':False,'source':'inline','canLike':True,'canRepost':True}
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state',return_value=state):
                    vk_batch.recheck_one(MagicMock(),db,188206921,13418,
                        confirm_repost_dialog_no_submit=True)
                self.assertEqual(db.execute('SELECT repost_status FROM posts').fetchone()[0],'pending')
                status_write(db,188206921,13418,'repost','uncertain','После финального клика')
                with patch('vk_batch.go_to_post'), patch('vk_batch.refresh_state',return_value=state):
                    with self.assertRaisesRegex(StopRun,'только к неопределённому'):
                        vk_batch.recheck_one(MagicMock(),db,188206921,13418,
                            confirm_repost_dialog_no_submit=True)
                self.assertEqual(db.execute('SELECT repost_status FROM posts').fetchone()[0],'uncertain')
            finally:
                db.close()

    def test_async_share_radio_render_is_waited_for(self):
        from vk_batch import wait_share_destination, click_share_then_publish, SHARE_DIAG_JS
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1320,'height':900})
            try:
                page.set_content('''
                  <section id="page-single-post"><article id="post-188206921_13418">
                  <div style="display:flex;gap:15px">
                    <div data-testid="post_footer_action_like" role="button"
                         aria-label="Убрать реакцию «Лайк»" data-user-likes="true">Нравится</div>
                    <button id="open" aria-label="Поделиться" onclick="setTimeout(()=>{
                       document.getElementById('modal').style.display='block';},1050)">Поделиться</button>
                  </div></article></section>
                  <div id="modal" role="dialog" style="display:none;position:fixed;right:20px;top:20px;
                    width:430px;height:440px;background:white;padding:20px">
                    <h2>Поделиться</h2>
                    <label><input type="radio" name="choice" checked><span>На своей стене</span></label>
                    <label><input type="radio" name="choice"><span>В личном сообщении</span></label>
                    <button data-testid="share_tab_submit" onclick="document.body.dataset.published='yes'">Поделиться</button>
                  </div>
                ''')
                from vk_batch import click_share_then_publish
                callback=[]
                click_share_then_publish(page,188206921,13418,
                                         before_publish=lambda:callback.append(True))
                self.assertEqual(callback,[True])
                self.assertEqual(page.locator('body').get_attribute('data-published'),'yes')
            finally:
                page.close();browser.close()

    def test_unknown_modal_labels_not_publish_and_diagnostic_no_private_text(self):
        from vk_batch import wait_share_destination
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1320,'height':900})
            try:
                page.set_content('''<section role="dialog" style="height:430px;width:400px">
                  <h2>Новый диалог</h2><label><input type="radio">Выберите получателя</label>
                  <span>Фамилия Реального Пользователя</span>
                  <button data-testid="share_tab_submit" onclick="document.body.dataset.sent='yes'">Отправить</button>
                </section>''')
                from contextlib import redirect_stdout
                import io
                out=io.StringIO()
                with redirect_stdout(out):
                    check=wait_share_destination(page,timeout_ms=700)
                self.assertFalse(check['ok'])
                self.assertNotIn('Фамилия Реального Пользователя',out.getvalue())
                self.assertIsNone(page.locator('body').get_attribute('data-sent'))
            finally:
                page.close();browser.close()
