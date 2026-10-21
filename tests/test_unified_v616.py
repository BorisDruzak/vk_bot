import contextlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import vk_api_read as api
import vk_batch as engine
import vk_groups as multi
import vk_unified as unified


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=engine.db_open(Path(self.tmp.name)/'db.sqlite')
        multi.init_dates(self.db)
        multi.init_scan_progress(self.db)
        self.g1={'id':101,'name':'Test1','enabled':True,'like_enabled':True,'repost_enabled':True}
        self.g2={'id':102,'name':'Test2','enabled':True,'like_enabled':True,'repost_enabled':True}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def post(self,gid,pid,age_hours=1,exact=True,liked='pending'):
        engine.add_posts(self.db,gid,[pid])
        multi.save_date(self.db,gid,pid,
                        int((datetime.now(timezone.utc)-timedelta(hours=age_hours)).timestamp()),
                        'vk-api-wall.get' if exact else 'feed-approx',exact)
        if liked!='pending':
            engine.status_write(self.db,gid,pid,'like',liked,'fixture')


class ApiReadTests(DbCase):
    def test_api_scan_exact_date_no_action_and_no_token_storage(self):
        token='TOP_SECRET_TOKEN_NOT_IN_DB'
        now=int(datetime.now(timezone.utc).timestamp())
        def transport(payload):
            self.assertEqual(payload['owner_id'],-101)
            self.assertEqual(payload['access_token'],token)
            self.assertEqual(payload['count'],100)
            return 200,{'response':{'count':2,'items':[
                {'owner_id':-101,'id':42,'date':now-150},
                {'owner_id':-101,'id':43,'date':now-360},
                {'owner_id':-999,'id':91,'date':now-260},
                {'owner_id':-101,'id':99,'date':'bad'}
            ]}}
        client=api.VKReadClient(self.db,token,transport=transport,sleeper=lambda x:None)
        res=api.scan_one_group(self.db,client,self.g1)
        self.assertEqual((res['fetched'],res['recent']),(2,2))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],2)
        dates=self.db.execute('SELECT group_id,post_id,exact,source FROM post_publication').fetchall()
        self.assertTrue(all(x['exact']==1 and x['source']=='vk-api-wall.get' for x in dates))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
        self.assertNotIn(token,str(list(self.db.iterdump())))
        self.assertFalse(res['saved_token'])

    def test_api_limit_code_saves_global_lock_no_retry(self):
        called=[]
        def transport(payload):
            called.append(1)
            return 200,{'error':{'error_code':6,'error_msg':'Too many requests per second'}}
        client=api.VKReadClient(self.db,'dummy',transport=transport)
        with self.assertRaises(api.VkApiLimited):
            client.wall_get(101)
        self.assertEqual(called,[1])
        self.assertIsNotNone(engine.safety_lock_state(self.db,0))
        with self.assertRaises(engine.StopRun):
            client.wall_get(101)
        self.assertEqual(called,[1])

    def test_http_429_saves_global_lock(self):
        c=api.VKReadClient(self.db,'dummy',transport=lambda p:(429,{}))
        with self.assertRaises(api.VkApiLimited):
            c.wall_get(101)
        self.assertIsNotNone(engine.safety_lock_state(self.db,0))

    def test_app_token_without_permission_rejected_no_actions(self):
        c=api.VKReadClient(self.db,'dummy',transport=lambda p:(200,{'error':{'error_code':7}}))
        with self.assertRaises(api.VkApiAuthFailure):
            c.wall_get(101)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
        self.assertIsNone(engine.safety_lock_state(self.db,0))

    def test_request_ledger_per_run_limited(self):
        calls=[]
        c=api.VKReadClient(self.db,'dummy',per_run=1,
                           transport=lambda p:(calls.append(1) or 200,{'response':{'items':[]}}))
        c.wall_get(101)
        with self.assertRaises(api.VkApiFailure):
            c.wall_get(102)
        self.assertEqual(len(calls),1)

    def test_api_read_and_browser_navigation_share_last_request_timestamp(self):
        clock=lambda: 2_000_000_000.0
        c=api.VKReadClient(self.db,'dummy',clock=clock,sleeper=lambda n:None,
                           transport=lambda p:(200,{'response':{'items':[]}}))
        c.wall_get(101)
        row=self.db.execute('SELECT last_navigation_at FROM read_navigation_pacing WHERE id=0').fetchone()
        self.assertEqual(row['last_navigation_at'],2_000_000_000.0)

    def test_cannot_mark_full_page_exhaustive(self):
        now=int(datetime.now(timezone.utc).timestamp())
        items=[{'owner_id':-101,'id':n+1,'date':now-60} for n in range(100)]
        c=api.VKReadClient(self.db,'dummy',transport=lambda p:(200,{'response':{'items':items}}))
        r=api.scan_one_group(self.db,c,self.g1)
        self.assertTrue(r['possibly_incomplete'])
        self.assertEqual(r['fetched'],100)

    def test_api_cannot_downgrade_verified_action(self):
        self.post(101,42,liked='verified')
        t=int(datetime.now(timezone.utc).timestamp())
        c=api.VKReadClient(self.db,'dummy',transport=lambda p:(200,{'response':{'items':[
            {'owner_id':-101,'id':42,'date':t-100}]}}))
        api.scan_one_group(self.db,c,self.g1)
        row=engine.target_row(self.db,101,42)
        self.assertEqual(row['like_status'],'verified')

    def test_read_pauses_before_second_api_request(self):
        now=[2_000_000_000.0]
        waits=[]
        def sleeper(seconds):
            waits.append(seconds)
            now[0]+=seconds
        c=api.VKReadClient(self.db,'dummy',per_run=2,pause_seconds=20,
                           clock=lambda:now[0],sleeper=sleeper,
                           transport=lambda p:(200,{'response':{'items':[]}}))
        c.wall_get(101)
        now[0]+=2
        c.wall_get(102)
        self.assertEqual(len(waits),1)
        self.assertAlmostEqual(waits[0],18.0,places=3)


class CampaignTests(DbCase):
    def test_old_action_does_not_count_and_new_verified_does(self):
        self.post(101,11,liked='verified')
        self.db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                        (101,11,'like',engine.moment()))
        self.db.commit()
        campaign=unified.start_campaign(self.db,'goal60',60,[101,102])
        self.assertEqual(unified.campaign_progress(self.db,campaign),0)
        self.post(102,12,liked='verified')
        self.db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                        (102,12,'like',engine.moment()))
        self.db.commit()
        self.assertEqual(unified.campaign_progress(self.db,campaign),1)
        # Duplicate attempt on same post doesn't credit the test twice.
        self.db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                        (102,12,'like',engine.moment()))
        self.db.commit()
        self.assertEqual(unified.campaign_progress(self.db,campaign),1)

    def test_completed_campaign_cannot_restart_or_change_target(self):
        c=unified.start_campaign(self.db,'goal60',1,[101])
        self.post(101,12,liked='verified')
        self.db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                        (101,12,'like',engine.moment()))
        self.db.commit()
        self.assertTrue(unified.finish_campaign_if_complete(self.db,c))
        self.assertIsNotNone(unified.get_campaign(self.db,'goal60')['completed_at'])
        same=unified.start_campaign(self.db,'goal60',1,[101])
        self.assertIsNotNone(same)
        with self.assertRaises(ValueError):
            unified.start_campaign(self.db,'goal60',60,[101])

    def test_queue_uses_only_exact_recent_like_pending(self):
        self.post(101,11,age_hours=2,exact=True)
        self.post(101,12,age_hours=2,exact=False)
        self.post(101,13,age_hours=250,exact=True)
        self.post(101,14,age_hours=3,exact=True,liked='already')
        self.post(102,15,age_hours=1,exact=True)
        rows=unified.pending_recent_likes(self.db,[self.g1,self.g2],7,limit=100)
        self.assertEqual([(r['group_id'],r['post_id']) for r in rows],[(102,15),(101,11)])
        self.g2['like_enabled']=False
        self.assertEqual(len(unified.pending_recent_likes(self.db,[self.g1,self.g2],7)),1)

    def test_no_action_if_24h_budget_exhausted(self):
        cfg=multi.load_config()
        self.post(101,20)
        for i in range(cfg['limits']['likes_per_24h']):
            self.db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                            (101,1000+i,'like',engine.moment()))
        self.db.commit()
        c=unified.start_campaign(self.db,'goal60',60,[101])
        with patch('vk_unified.sync_playwright') as context:
            result=unified.run_likes(self.db,cfg,[self.g1],c,max_posts=5,pause=20)
        self.assertEqual(result,0)
        context.assert_not_called()

    def test_run_likes_never_reposts_and_no_second_navigation(self):
        cfg=multi.load_config()
        cfg['limits']['likes_per_run']=1
        cfg['limits']['actions_per_run']=1
        self.post(101,11)
        self.post(101,12)
        c=unified.start_campaign(self.db,'goal60',60,[101])
        pw=MagicMock()
        ctx=pw.chromium.launch_persistent_context.return_value
        fake_page=ctx.pages[0]
        cm=MagicMock()
        cm.__enter__.return_value=pw
        cm.__exit__.return_value=False
        now=int(datetime.now(timezone.utc).timestamp())
        before=self.db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0]
        calls=[]
        def fake_process(page,db,gid,row,pause,safety,actions,already_open):
            self.assertEqual(actions,{'like'})
            self.assertTrue(already_open)
            safety.reserve(row['post_id'],'like')
            engine.status_write(db,gid,row['post_id'],'like','verified','stub VK confirmation')
            return True
        with patch('vk_unified.sync_playwright',return_value=cm),\
             patch('vk_unified.engine.go_to_post',side_effect=lambda page,gid,pid:calls.append((gid,pid))),\
             patch('vk_unified.multi.exact_post_date_info',side_effect=lambda page,gid,pid:{'publishedAt': self.db.execute(
                 'SELECT published_at FROM post_publication WHERE group_id=? AND post_id=?',(gid,pid)).fetchone()[0]}),\
             patch('vk_unified.engine.process_one',side_effect=fake_process):
            result=unified.run_likes(self.db,cfg,[self.g1],c,max_posts=12,pause=20)
        self.assertEqual(result,1)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(self.db.execute("SELECT * FROM action_attempts WHERE action='like'").fetchall()),1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0]-before,1)
        self.assertEqual(unified.campaign_progress(self.db,c),1)


    def test_engine_already_open_keeps_same_post_without_second_navigation(self):
        self.post(101,11)
        row=engine.target_row(self.db,101,11)
        page=MagicMock()
        page.evaluate.return_value=True  # Exact post root remains visible.
        with patch('vk_batch.go_to_post') as navigate, \
             patch('vk_batch.ensure_ok'), \
             patch('vk_batch.refresh_state',return_value={
                 'liked':True,'reposted':False,'source':'inline','canLike':True,'canRepost':True}), \
             patch('vk_batch.wait_post_footer_info',return_value={'ok':True,'liked':True}):
            engine.process_one(page,self.db,101,row,0,actions={'like'},already_open=True)
        navigate.assert_not_called()
        self.assertEqual(engine.target_row(self.db,101,11)['like_status'],'already')
        self.assertEqual(engine.target_row(self.db,101,11)['repost_status'],'pending')

    def test_engine_already_open_fails_if_post_root_disappears(self):
        self.post(101,11)
        row=engine.target_row(self.db,101,11)
        page=MagicMock()
        page.evaluate.return_value=False
        with patch('vk_batch.go_to_post') as navigate, patch('vk_batch.ensure_ok'), \
             patch('vk_batch.click_like') as click:
            with self.assertRaisesRegex(engine.StopRun,'исчез после проверки даты'):
                engine.process_one(page,self.db,101,row,0,actions={'like'},already_open=True)
        navigate.assert_not_called()
        click.assert_not_called()
        self.assertEqual(engine.target_row(self.db,101,11)['like_status'],'pending')

    def test_rate_lock_blocks_before_campaign_execution(self):
        engine.lock_on_platform_warning(self.db,0,'Too many requests per second')
        with self.assertRaises(engine.StopRun):
            engine.require_platform_unlocked(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)

    def test_exclusive_run_blocks_second_and_removes_lock(self):
        file=Path(self.tmp.name)/'run.lock'
        with unified.exclusive_run(file):
            self.assertTrue(file.exists())
            with self.assertRaises(unified.RunAlreadyActive):
                with unified.exclusive_run(file):
                    pass
        self.assertFalse(file.exists())

    def test_run_needs_explicit_confirm(self):
        with self.assertRaises(SystemExit) as caught:
            unified.main(['--run'])
        self.assertEqual(caught.exception.code,2)


if __name__=='__main__':
    unittest.main()
