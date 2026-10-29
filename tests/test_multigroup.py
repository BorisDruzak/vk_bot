import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import vk_batch as engine
import vk_groups as multi


class MultiGroupConfigTests(unittest.TestCase):
    def test_imported_groups_are_all_unique_and_include_administration(self):
        cfg=multi.load_config()
        self.assertEqual(len(cfg['groups']),64)
        self.assertEqual(len({x['id'] for x in cfg['groups']}),64)
        self.assertIn(188206921,{x['id'] for x in cfg['groups']})
        self.assertEqual(cfg['lookback_days'],7)

    def test_filter_groups_by_id(self):
        cfg=multi.load_config()
        x=multi.selected_groups(cfg,'188206921,198577444',0)
        self.assertEqual({g['id'] for g in x},{188206921,198577444})
        self.assertEqual(len(multi.selected_groups(cfg,None,2)),2)

    def test_duplicate_group_configuration_rejected(self):
        cfg=multi.load_config()
        cfg['groups'].append(copy.deepcopy(cfg['groups'][0]))
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'groups.json'
            target.write_text(json.dumps(cfg),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'Дублируется'):
                multi.load_config(target)


class GroupDateTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,20,13,0,0,tzinfo=timezone.utc)

    def test_relative_date_3_days(self):
        value=multi.approximate_post_date('3 д назад',now=self.now)
        self.assertEqual(value,int((self.now-timedelta(days=3)).timestamp()))

    def test_hours_and_yesterday(self):
        value=multi.approximate_post_date('2 ч назад',now=self.now)
        self.assertEqual(value,int((self.now-timedelta(hours=2)).timestamp()))
        self.assertEqual(multi.approximate_post_date('вчера в 18:30',now=self.now),
                         int((self.now-timedelta(days=1)).timestamp()))

    def test_month_march_is_not_may(self):
        self.assertEqual(datetime.fromtimestamp(multi.approximate_post_date('5 марта',now=self.now),tz=timezone.utc).month,3)
        self.assertEqual(datetime.fromtimestamp(multi.approximate_post_date('5 мая',now=self.now),tz=timezone.utc).month,5)

    def test_unknown_date_is_not_eligible(self):
        self.assertIsNone(multi.approximate_post_date('неизвестная дата',now=self.now))
        self.assertFalse(multi.date_allowed(None,7,True))

    def test_one_week_only_or_older_opt_in(self):
        now=datetime.now(timezone.utc)
        five=int((now-timedelta(days=5)).timestamp())
        eleven=int((now-timedelta(days=11)).timestamp())
        self.assertTrue(multi.date_allowed(five,7,True))
        self.assertFalse(multi.date_allowed(eleven,7,True))
        self.assertTrue(multi.date_allowed(eleven,7,False))


class MultiGroupDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=engine.db_open(Path(self.tmp.name)/'state.sqlite3')
        multi.init_dates(self.db)
        self.groups=[{'id':2},{'id':1}]
        engine.add_posts(self.db,1,[101,102,103])
        engine.add_posts(self.db,2,[201,202])

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_recent_items_across_groups_sorted_by_date(self):
        now=int(datetime.now(timezone.utc).timestamp())
        multi.save_date(self.db,1,101,now-3600,'feed-approx',False)
        multi.save_date(self.db,2,201,now-100,'feed-approx',False)
        multi.save_date(self.db,1,102,now-10*86400,'feed-approx',False)
        rows=multi.candidate_rows(self.db,self.groups,7,include_unknown=False,limit=10)
        self.assertEqual([(r['group_id'],r['post_id']) for r in rows],[(2,201),(1,101)])

    def test_old_backlog_only_after_recent_when_enabled(self):
        now=int(datetime.now(timezone.utc).timestamp())
        multi.save_date(self.db,1,101,now-15*86400,'feed-approx',False)
        multi.save_date(self.db,2,201,now-1*86400,'feed-approx',False)
        rows=multi.candidate_rows(self.db,self.groups,7,include_unknown=False,include_older=True,limit=10)
        self.assertEqual([(r['group_id'],r['post_id']) for r in rows],[(2,201),(1,101)])

    def test_unknown_candidates_round_robin_in_audit(self):
        rows=multi.candidate_rows(self.db,self.groups,7,include_unknown=True,limit=4)
        self.assertEqual([(r['group_id'],r['post_id']) for r in rows],[(2,202),(1,103),(2,201),(1,102)])

    def test_exact_date_protected_from_approximate_overwrite(self):
        t=int(datetime.now(timezone.utc).timestamp())
        multi.save_date(self.db,1,101,t,'vk-post-json',True)
        multi.save_date(self.db,1,101,t-100000,'feed-approx',False)
        row=self.db.execute('SELECT published_at,exact,source FROM post_publication WHERE group_id=1 AND post_id=101').fetchone()
        self.assertEqual((row['published_at'],row['exact'],row['source']),(t,1,'vk-post-json'))

    def test_audit_does_not_repeat_same_missing_post(self):
        now=int(datetime.now(timezone.utc).timestamp())
        multi.save_date(self.db,1,103,now-3600,'feed-approx',False)
        multi.save_date(self.db,2,202,now-5400,'feed-approx',False)
        first=multi.candidate_rows(self.db,self.groups,7,include_unknown=True,for_audit=True,limit=2)
        self.assertEqual([(r['group_id'],r['post_id']) for r in first],[(1,103),(2,202)])
        engine.save_audit(self.db,1,103,False,False,'mock')
        second=multi.candidate_rows(self.db,self.groups,7,include_unknown=True,for_audit=True,limit=2)
        self.assertNotIn((1,103),[(r['group_id'],r['post_id']) for r in second])
        self.assertIn((2,202),[(r['group_id'],r['post_id']) for r in second])

    def test_missing_date_does_not_monopolize_repeated_runs(self):
        multi.save_date(self.db,2,202,None,'date-unknown',False)
        second=multi.candidate_rows(self.db,self.groups,7,include_unknown=True,for_audit=True,limit=5)
        self.assertNotIn((2,202),[(r['group_id'],r['post_id']) for r in second])
        then=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat(timespec='seconds')
        self.db.execute('UPDATE post_publication SET checked_at=? WHERE group_id=2 AND post_id=202',(then,))
        self.db.commit()
        third=multi.candidate_rows(self.db,self.groups,7,include_unknown=True,for_audit=True,limit=5)
        self.assertIn((2,202),[(r['group_id'],r['post_id']) for r in third])

    def test_ledger_count_is_profile_wide_and_per_action(self):
        cfg=multi.load_config()
        limit={**cfg['limits'],'actions_per_run':3,'actions_per_24h':3,'likes_per_run':2,'likes_per_24h':2,'reposts_per_run':2,'reposts_per_24h':2}
        budget=multi.MultiBudget(self.db,limit)
        budget.current_group_id=1
        budget.reserve(101,'like')
        budget.current_group_id=2
        budget.reserve(201,'repost')
        self.assertEqual(budget.past_count(),2)
        self.assertEqual(budget.past_count('like'),1)
        self.assertEqual(budget.past_count('repost'),1)
        budget.reserve(202,'like')
        self.assertFalse(budget.available_actions({'like','repost'}))

    def test_persistent_warning_blocks_all_groups(self):
        engine.lock_on_platform_warning(self.db,1,'VK сообщил об ограничении')
        with self.assertRaises(engine.StopRun):
            multi.MultiBudget(self.db,multi.load_config()['limits'])


class BrowserMarkupTests(unittest.TestCase):
    def test_feed_scan_ignores_nested_other_group(self):
        from playwright.sync_api import sync_playwright
        html='''<div data-testid="post" data-post-id="-188_3" data-post-nesting-lvl="0">
         <div data-testid="post" data-post-id="-299_5" data-post-nesting-lvl="1">
          <a data-testid="post_date_block_preview">6 д назад</a></div>
         <a data-testid="post_date_block_preview">1 д назад</a></div>
         <div data-testid="post" data-post-id="-188_4" data-post-nesting-lvl="0">
           <a data-testid="post_date_block_preview">2 ч назад</a></div>'''
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                page=browser.new_page()
                page.set_content(html)
                data=page.evaluate(multi.DISCOVER_DATED_JS,{'groupId':188})
                self.assertEqual([x['postId'] for x in data],[3,4])
                self.assertEqual([x['dateText'] for x in data],['1 д назад','2 ч назад'])
            finally:
                browser.close()

    def test_exact_date_from_matching_post_only(self):
        from playwright.sync_api import sync_playwright
        html='''<script>
        window.cur={apiPrefetchCache:[{"method":"wall.getById","response":{"items":[
          {"owner_id":-188,"id":10,"date":1790000000},
          {"owner_id":-188,"id":11,"date":1790099999}]}}]};
        </script>'''
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                page=browser.new_page()
                page.set_content(html)
                data=page.evaluate(multi.EXACT_DATE_JS,{'groupId':188,'postId':10})
                self.assertEqual(data['publishedAt'],1790000000)
                data=page.evaluate(multi.EXACT_DATE_JS,{'groupId':188,'postId':12})
                self.assertIsNone(data['publishedAt'])
            finally:
                browser.close()


class SinglePostCompatibilityTests(unittest.TestCase):
    def test_engine_can_skip_one_action_without_affecting_other(self):
        # Do not click or inspect like-footer when configured for repost only.
        row={'post_id':10,'like_status':'pending','repost_status':'pending'}
        state={'liked':True,'reposted':True,'canLike':True,'canRepost':True,'source':'mock'}
        with patch.object(engine,'go_to_post'), patch.object(engine,'refresh_state',return_value=state),\
             patch.object(engine,'status_write') as status_write,\
             patch.object(engine,'wait_post_footer_info',side_effect=AssertionError('like footer accessed')):
            engine.process_one(None,None,188,row,0,actions={'repost'})
            self.assertEqual(status_write.call_count,1)
            self.assertEqual(status_write.call_args.args[3:5],('repost','already'))


if __name__=='__main__':
    unittest.main()
