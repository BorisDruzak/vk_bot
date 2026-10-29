"""v6.13: exact vs relative/tooltip dates and retry-safe VK SPA loads."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import vk_batch as engine
import vk_groups as multi

# Fixtures contain no accounts, tokens, cookie jars or real profile data.
GROUP = 203298456
POST = 1711
PERMALINK = f'/wall-{GROUP}_{POST}'


def browser_page():
    return f'''<main id="page-single-post"><article>
    <div data-testid="post" data-post-nesting-lvl="0" data-post-id="-{GROUP}_{POST}">
    <a data-testid="post_date_block_preview" href="{PERMALINK}">1 ч назад</a>
    <div data-testid="post" data-post-nesting-lvl="1" data-post-id="-555_3">
      <a data-testid="post_date_block_preview" href="/wall-555_3"><time datetime="2026-10-20T12:22:00+05:00"></time></a>
    </div></div></article></main>'''


class PostDateBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_relative_time_link_does_not_provide_exact_epoch(self):
        p=self.browser.new_page()
        try:
            p.set_content(browser_page())
            info=p.evaluate(multi.EXACT_DATE_JS,{'groupId':GROUP,'postId':POST})
            self.assertIsNone(info['publishedAt'])
            self.assertFalse(info['exact'])
            hint=multi.read_post_date_hint(p,GROUP,POST,hover=False)
            self.assertEqual(hint['text'],'1 ч назад')
            self.assertEqual(multi.approximate_from_date_hint(hint)[1],'post-relative-approx')
        finally:
            p.close()

    def test_matching_dom_iso_with_timezone_is_exact(self):
        p=self.browser.new_page()
        try:
            html=browser_page().replace('>1 ч назад</a>',
                '><time datetime="2026-10-20T13:35:00+05:00">1 ч назад</time></a>')
            p.set_content(html)
            date=p.evaluate(multi.EXACT_DATE_JS,{'groupId':GROUP,'postId':POST})
            self.assertEqual(date['publishedAt'],int(datetime(2026,10,20,8,35,tzinfo=timezone.utc).timestamp()))
            self.assertEqual(date['source'],'vk-dom-iso')
        finally:
            p.close()

    def test_missing_timezone_does_not_provide_exact_epoch(self):
        p=self.browser.new_page()
        try:
            p.set_content(browser_page().replace('>1 ч назад</a>',
                '><time datetime="2026-10-20T13:35:00">1 ч назад</time></a>'))
            d=p.evaluate(multi.EXACT_DATE_JS,{'groupId':GROUP,'postId':POST})
            self.assertIsNone(d['publishedAt'])
        finally:
            p.close()

    def test_unrelated_repost_date_not_confused_with_main_post(self):
        p=self.browser.new_page()
        try:
            p.set_content(browser_page())
            d=p.evaluate(multi.EXACT_DATE_JS,{'groupId':GROUP,'postId':POST})
            self.assertIsNone(d['publishedAt'])
        finally:
            p.close()

    def test_json_is_preferrred_to_dom_date(self):
        p=self.browser.new_page()
        try:
            p.set_content(browser_page().replace('>1 ч назад</a>',
                '><time datetime="2026-10-20T13:35:00+05:00"></time></a>'))
            expected=1792490000
            p.evaluate('''({gid,pid,stamp}) => { window.cur={apiPrefetchCache:[{
                method:'wall.getById',response:{items:[{owner_id:-gid,id:pid,date:stamp}]}
                }]}; }''',{'gid':GROUP,'pid':POST,'stamp':expected})
            d=p.evaluate(multi.EXACT_DATE_JS,{'groupId':GROUP,'postId':POST})
            self.assertEqual(d['publishedAt'],expected)
            self.assertEqual(d['source'],'vk-post-json')
        finally:
            p.close()

    def test_hover_tooltip_is_read_only_hint_not_exact(self):
        p=self.browser.new_page()
        try:
            p.set_content(browser_page()+'''
                <script>
                document.querySelector('[data-testid="post_date_block_preview"]').addEventListener('mouseenter',()=>{
                  const el=document.createElement('div'); el.setAttribute('role','tooltip');
                  el.textContent='20 октября 2026 г. в 13:35';
                  document.body.append(el);
                });
                </script>''')
            hint=multi.read_post_date_hint(p,GROUP,POST)
            self.assertEqual(hint['tooltip'],'20 октября 2026 г. в 13:35')
            stamp,source=multi.approximate_from_date_hint(hint)
            self.assertEqual(source,'tooltip-local-approx')
            self.assertIsNotNone(stamp)
            self.assertIsNone(p.evaluate(multi.EXACT_DATE_JS,{'groupId':GROUP,'postId':POST})['publishedAt'])
        finally:
            p.close()


class TooltipParserTests(unittest.TestCase):
    def test_human_absolute_date_is_not_exact_source(self):
        epoch=multi._display_date_timestamp('20 октября 2026 г. в 13:35')
        self.assertEqual(epoch,int(datetime(2026,10,20,13,35).timestamp()))
        self.assertEqual(multi._display_date_timestamp('20.10.2026 13:35'),epoch)
        self.assertIsNone(multi._display_date_timestamp('1 ч назад'))
        self.assertIsNone(multi._display_date_timestamp('ошибка загрузки'))

    def test_today_yesterday_with_clock(self):
        today=datetime.now().astimezone()
        self.assertEqual(multi._display_date_timestamp('Сегодня в 14:01'),
                         int(datetime(today.year,today.month,today.day,14,1).timestamp()))
        earlier=today.date()-timedelta(days=1)
        self.assertEqual(multi._display_date_timestamp('Вчера в 14:01'),
                         int(datetime(earlier.year,earlier.month,earlier.day,14,1).timestamp()))


class MigrationAndAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=engine.db_open(Path(self.temp.name)/'test.db')
        self.gid=GROUP
        self.pid=POST
        engine.add_posts(self.db,self.gid,[self.pid,self.pid-1])

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_migrates_old_publication_table_without_losing_dates(self):
        epoch=int(datetime.now(timezone.utc).timestamp())
        self.db.execute('''CREATE TABLE post_publication (group_id INTEGER NOT NULL,post_id INTEGER NOT NULL,
          published_at INTEGER,source TEXT NOT NULL,exact INTEGER NOT NULL DEFAULT 0,
          checked_at TEXT NOT NULL,PRIMARY KEY(group_id,post_id))''')
        self.db.execute('INSERT INTO post_publication VALUES (?,?,?,?,?,?)',
                        (self.gid,self.pid,epoch,'vk-post-json',1,engine.moment()))
        self.db.commit()
        multi.init_dates(self.db)
        multi.init_dates(self.db)
        row=self.db.execute('SELECT published_at,source,exact,exact_attempt_at FROM post_publication').fetchone()
        self.assertEqual((row['published_at'],row['source'],row['exact']),
                         (epoch,'vk-post-json',1))
        self.assertIsNone(row['exact_attempt_at'])

    def test_failed_exact_lookup_preserves_approx_date_and_skips_one_day(self):
        multi.init_dates(self.db)
        approx=int(datetime.now(timezone.utc).timestamp()-3600)
        multi.save_date(self.db,self.gid,self.pid,approx,'feed-approx',False)
        multi.save_date(self.db,self.gid,self.pid,None,'date-unknown',False,date_checked=True)
        row=self.db.execute('SELECT published_at,source,exact_attempt_at FROM post_publication').fetchone()
        self.assertEqual(row['published_at'],approx)
        self.assertEqual(row['source'],'feed-approx')
        self.assertTrue(row['exact_attempt_at'])
        groups=[{'id':self.gid}]
        rows=multi.candidate_rows(self.db,groups,7,include_unknown=True,for_audit=True,limit=10)
        self.assertNotIn(self.pid,[r['post_id'] for r in rows])
        self.assertIn(self.pid-1,[r['post_id'] for r in rows])
        old=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat(timespec='seconds')
        self.db.execute('UPDATE post_publication SET exact_attempt_at=? WHERE group_id=? AND post_id=?',
                        (old,self.gid,self.pid)); self.db.commit()
        rows=multi.candidate_rows(self.db,groups,7,include_unknown=True,for_audit=True,limit=10)
        self.assertIn(self.pid,[r['post_id'] for r in rows])

    def test_relative_tooltip_does_not_authorize_execution(self):
        multi.init_dates(self.db)
        page=MagicMock()
        with patch.object(multi,'exact_post_date_info', return_value={'publishedAt':None,'source':'date-unknown'}), \
             patch.object(multi,'read_post_date_hint',return_value={'text':'1 ч назад','title':'','aria':'','tooltip':''}):
            result=multi.apply_actual_date(self.db,self.gid,self.pid,page)
        self.assertIsNone(result)
        row=self.db.execute('SELECT published_at,exact,source FROM post_publication').fetchone()
        self.assertIsNotNone(row['published_at'])
        self.assertEqual(row['exact'],0)
        self.assertFalse(multi.date_allowed(result,7,True))


class NavigationRetryTests(unittest.TestCase):
    def test_correct_route_recovers_on_second_read_only_navigation(self):
        page=MagicMock()
        page.url=f'https://vk.ru/wall-{GROUP}_{POST}'
        success=MagicMock()
        success.json_value.return_value={'kind':'ready'}
        page.wait_for_function.side_effect=[PWTimeout('No post root'), success]
        with patch.object(engine,'ensure_ok'), patch.object(engine,'diagnose_post',
            return_value={'correctRoute':True,'skeletons':0,'idRoot':False,'dataRoot':False}):
            engine.go_to_post(page,GROUP,POST)
        self.assertEqual(page.goto.call_count,2)
        page.locator.assert_not_called()
        self.assertEqual(page.wait_for_function.call_args.kwargs['timeout'],20000)

if __name__ == '__main__':
    unittest.main()
