"""Offline acceptance tests for VK Unified v6.21's automatic recent API queue.

No network, VK profile, cookies or live likes/reposts are used.
"""
from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import vk_api_read as api
import vk_batch as engine
import vk_groups as multi
import vk_unified as u


class AutomaticPairsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = engine.db_open(self.root/'state.sqlite')
        self.addCleanup(self.db.close)
        multi.init_dates(self.db)
        api.init_api(self.db)
        self.now = int(datetime.now(timezone.utc).timestamp())
        self.groups = [dict(id=101, name='Управление 1', enabled=True,
                            like_enabled=True, repost_enabled=True),
                       dict(id=102, name='Управление 2', enabled=True,
                            like_enabled=True, repost_enabled=True)]
        self.cfg = {'lookback_days':7, 'read_pause_seconds':20, 'action_pause_seconds':0,
                    'limits': {'actions_per_run':3, 'actions_per_24h':25,
                               'likes_per_run':2, 'likes_per_24h':20,
                               'reposts_per_run':1, 'reposts_per_24h':5}}

    def add(self, gid, pid, *, delta=100, api_post=True, like='pending',
            repost='pending', exact=True, mismatch=False, access=None):
        engine.add_posts(self.db,gid,[pid])
        ts=self.now-delta
        multi.save_date(self.db,gid,pid,ts, api.API_SOURCE if api_post else 'browser-date', exact)
        self.db.execute('UPDATE posts SET like_status=?,repost_status=? WHERE group_id=? AND post_id=?',
                        (like,repost,gid,pid))
        if api_post:
            self.db.execute('INSERT INTO api_seen_posts(group_id,post_id,published_at,seen_at,valid) '
                            'VALUES(?,?,?,?,1)', (gid,pid,ts-1 if mismatch else ts,engine.moment()))
        if access:
            engine.record_post_access(self.db,gid,pid,access,'no access')
        self.db.commit()

    def start(self, target=2, name='auto_pairs_test', gids=(101,102)):
        return u.start_auto_pair_campaign(self.db,name,target,list(gids),7)

    def fake_browser(self, fake_process):
        fake_pw=MagicMock()
        fake_context=fake_pw.chromium.launch_persistent_context.return_value
        fake_context.pages=[MagicMock()]
        fake_manager=MagicMock()
        fake_manager.__enter__.return_value=fake_pw
        fake_manager.__exit__.return_value=False
        return fake_manager

    def test_queue_is_api_only_exact_fresh_safe(self):
        self.add(101,11)
        self.add(101,12,api_post=False)
        self.add(101,13,delta=8*86400)
        self.add(101,14,exact=False)
        self.add(101,15,mismatch=True)
        self.add(101,16,access='hidden')
        self.add(101,17,like='uncertain')
        self.add(101,18,repost='error')
        self.add(101,19,like='verified',repost='verified')
        self.add(101,20,delta=-2*3600)
        self.add(102,21)
        got=u.auto_pair_candidates(self.db,self.groups,7)
        self.assertEqual({(x['group_id'],x['post_id']) for x in got},{(101,11),(102,21)})

    def test_queue_prefers_incomplete_pair_before_newest(self):
        self.add(101,11,delta=10,like='pending',repost='pending')
        self.add(102,12,delta=3600,like='verified',repost='pending')
        campaign=self.start()
        self.db.execute('INSERT INTO pair_experiment_posts(campaign,group_id,post_id) VALUES(?,?,?)',
                        (campaign['name'],102,12))
        self.db.commit()
        got=u.auto_pair_candidates(self.db,self.groups,7,campaign)
        self.assertEqual(got[0]['post_id'],12)
        self.assertEqual(got[1]['post_id'],11)

    def test_auto_campaign_is_resumable_and_distinct_from_manual(self):
        c1=self.start(target=2)
        c2=self.start(target=2)
        self.assertEqual(c1['baseline_action_id'],c2['baseline_action_id'])
        self.assertTrue(c2['approved_digest'].startswith('auto-api-v1:'))
        self.assertEqual(c1['created_at'],c2['created_at'])
        with self.assertRaisesRegex(ValueError,'уже другие настройки'):
            self.start(target=1)
        with self.assertRaisesRegex(ValueError,'уже другие настройки'):
            self.start(target=2,gids=(101,))
        self.db.execute('INSERT INTO pair_experiments(name,target_each,created_at,baseline_action_id,approved_digest) '
                        'VALUES(?,?,?,?,?)',('legacy_manual',2,engine.moment(),0,'manual_digest'))
        self.db.commit()
        with self.assertRaisesRegex(ValueError,'другие настройки'):
            self.start(target=2,name='legacy_manual')

    def test_auto_run_finishes_partial_pairs_and_respects_limits(self):
        self.add(101,11,delta=100)
        self.add(102,12,delta=200)
        c=self.start()
        clicks=[]
        def fake_process(page,db,gid,row,pause,safety,actions,already_open):
            self.assertTrue(already_open)
            self.assertIsNotNone(db.execute('SELECT 1 FROM pair_experiment_posts '
                'WHERE campaign=? AND group_id=? AND post_id=?',(c['name'],gid,row['post_id'])).fetchone())
            for action in ('like','repost'):
                if action in actions and row[f'{action}_status']=='pending':
                    safety.reserve(row['post_id'],action)
                    engine.status_write(db,gid,row['post_id'],action,'verified','VK confirmed')
                    clicks.append((gid,row['post_id'],action))
            return True
        fake_pw=self.fake_browser(fake_process)
        with patch.object(u,'sync_playwright',return_value=fake_pw), \
             patch.object(engine,'go_to_post'), \
             patch.object(multi,'exact_post_date_info',return_value={'publishedAt':self.now-100}), \
             patch.object(engine,'process_one',side_effect=fake_process), \
             patch.object(engine,'configure_navigation_pacing'), \
             patch.object(engine,'watch_rate_limit_responses'), \
             patch.object(engine,'require_platform_unlocked'), \
             patch.object(engine,'clear_post_access'), redirect_stdout(io.StringIO()):
            # When a second post's exact date differs the guard MUST prevent
            # its actions; first we only complete the newest row.
            first=u.run_approved_pairs(self.db,self.cfg,self.groups,c,max_posts=1,pause=20,auto_mode=True)
            self.assertEqual(first,1)
            self.assertEqual((u.paired_progress(self.db,c)['like'],
                              u.paired_progress(self.db,c)['repost']),(1,1))
            # Dates for the second post must match VK; now use a post-aware mock.
            with patch.object(multi,'exact_post_date_info',side_effect=lambda page,gid,pid:
                              {'publishedAt': self.now-(100 if pid==11 else 200)}):
                second=u.run_approved_pairs(self.db,self.cfg,self.groups,c,max_posts=2,pause=20,auto_mode=True)
        self.assertEqual(second,1)
        self.assertEqual((u.paired_progress(self.db,c)['like'],u.paired_progress(self.db,c)['repost']),(2,2))
        self.assertEqual(clicks,[(101,11,'like'),(101,11,'repost'),(102,12,'like'),(102,12,'repost')])
        self.assertIsNotNone(self.db.execute('SELECT completed_at FROM pair_experiments WHERE name=?',
                                           (c['name'],)).fetchone()['completed_at'])
        count=self.db.execute('SELECT COUNT(*) FROM pair_experiment_posts WHERE campaign=?',
                              (c['name'],)).fetchone()[0]
        self.assertEqual(count,2)

    def test_csv_only_reports_checked_selected_posts_and_new_flags(self):
        self.add(101,11,like='already',repost='pending')
        self.add(102,12)
        campaign=self.start()
        self.db.executemany('INSERT INTO pair_experiment_posts(campaign,group_id,post_id) VALUES(?,?,?)',
                            [(campaign['name'],101,11),(campaign['name'],102,12)])
        self.db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                        (101,11,'repost',engine.moment()))
        engine.status_write(self.db,101,11,'repost','verified','confirmed VK')
        dest=self.root/'report.csv'
        with redirect_stdout(io.StringIO()):
            n=u.export_auto_pair_results(self.db,campaign,self.groups,dest)
        self.assertEqual(n,2)
        with dest.open(encoding='utf-8-sig',newline='') as fh:
            rows=list(csv.DictReader(fh,delimiter=';'))
        self.assertEqual(len(rows),2)
        by_post={int(row['url'].split('_')[-1]):row for row in rows}
        self.assertEqual(by_post[11]['liked'],'да')
        self.assertEqual(by_post[11]['reposted'],'да')
        self.assertEqual(by_post[11]['new_like_in_campaign'],'нет')
        self.assertEqual(by_post[11]['new_repost_in_campaign'],'да')
        self.assertEqual(by_post[12]['liked'],'нет')
        self.assertEqual(by_post[12]['reposted'],'нет')
        self.assertFalse((self.root/'report.csv.tmp').exists())

    def test_report_written_when_run_raises(self):
        self.add(101,11)
        campaign=self.start()
        self.db.execute('INSERT INTO pair_experiment_posts(campaign,group_id,post_id) VALUES(?,?,?)',
                        (campaign['name'],101,11))
        self.db.commit()
        dest=self.root/'auto.csv'
        with patch.object(u,'exclusive_run',return_value=nullcontext()), \
             patch.object(engine,'DB_PATH',self.root/'state.sqlite'), \
             patch.object(u,'run_approved_pairs',side_effect=engine.StopRun('Mocked unsafe state')), \
             redirect_stdout(io.StringIO()):
            # CLI reads a real config, but uses only the DB and fake runner.
            cfgfile=self.root/'cfg.json'
            import json
            config=json.loads((Path(u.__file__).parent/'vk_groups.json').read_text())
            config['groups']=[config['groups'][0]]
            config['groups'][0]['id']=101
            config['groups'][0]['url']='https://vk.com/club101'
            cfgfile.write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
            # Different group scope would reject existing campaign; new default scope same single group.
            campaign=u.start_auto_pair_campaign(self.db,'scope_101',2,[101],7)
            self.db.execute('INSERT INTO pair_experiment_posts(campaign,group_id,post_id) VALUES(?,?,?)', ('scope_101',101,11))
            self.db.commit()
            code=u.main(['--run-pairs','--pair-name','scope_101','--goal-pairs','2',
                         '--max-posts','1','--confirm','--config',str(cfgfile),
                         '--report-file',str(dest)])
        self.assertEqual(code,2)
        self.assertTrue(dest.exists())
        with dest.open(encoding='utf-8-sig') as f:
            self.assertGreater(len(list(csv.DictReader(f,delimiter=';'))),0)

    def test_pair_status_creates_csv_without_vk_requests(self):
        self.add(101,11)
        campaign=u.start_auto_pair_campaign(self.db,'scope_101',2,[101],7)
        self.db.execute('INSERT INTO pair_experiment_posts(campaign,group_id,post_id) VALUES(?,?,?)',
                        ('scope_101',101,11))
        self.db.commit()
        # Direct functions: same as local --pair-status dispatch, no browser.
        dest=self.root/'local_only.csv'
        with redirect_stdout(io.StringIO()) as out:
            u.print_pair_status(self.db,campaign['name'])
            u.export_auto_pair_results(self.db,campaign,self.groups,dest)
        self.assertIn('автоматическая API-очередь',out.getvalue())
        self.assertTrue(dest.exists())

    def test_cli_requires_confirmation_and_rejects_removed_approval_arg(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exc:
            u.main(['--run-pairs'])
        self.assertEqual(exc.exception.code,2)
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exc:
            u.main(['--run-pairs','--approved-file','old.csv','--confirm'])
        self.assertEqual(exc.exception.code,2)


if __name__=='__main__':
    unittest.main()
