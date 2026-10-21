"""Offline tests for per-command action amounts. No VK network access or clicks."""
from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import vk_api_read as api
import vk_batch as engine
import vk_groups as multi
import vk_unified as app


class CommandActionsTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root=Path(td.name)
        self.db=engine.db_open(self.root/'state.sqlite')
        self.addCleanup(self.db.close)
        multi.init_dates(self.db)
        api.init_api(self.db)
        self.now=int(datetime.now(timezone.utc).timestamp())
        self.groups=[{'id':101,'name':'Тестовая группа 1','enabled':True,
                      'like_enabled':True,'repost_enabled':True},
                     {'id':102,'name':'Тестовая группа 2','enabled':True,
                      'like_enabled':True,'repost_enabled':True}]
        # Historical JSON deliberately allows only ONE action per run and
        # no reposts per day; new explicit mode must not use these quotas.
        self.cfg={'lookback_days':7,'read_pause_seconds':20,'action_pause_seconds':0,
                  'limits':{'actions_per_run':1,'actions_per_24h':1,
                            'likes_per_run':1,'likes_per_24h':1,
                            'reposts_per_run':1,'reposts_per_24h':1}}

    def add(self,gid,pid,delta=100,like='pending',repost='pending',api_post=True,exact=True):
        engine.add_posts(self.db,gid,[pid])
        published=self.now-delta
        multi.save_date(self.db,gid,pid,published,api.API_SOURCE if api_post else 'browser-date',exact)
        self.db.execute('UPDATE posts SET like_status=?,repost_status=? WHERE group_id=? AND post_id=?',
                        (like,repost,gid,pid))
        if api_post:
            self.db.execute('INSERT INTO api_seen_posts(group_id,post_id,published_at,seen_at,valid) '
                            'VALUES(?,?,?,?,1)',(gid,pid,published,engine.moment()))
        self.db.commit()

    @staticmethod
    def browser():
        pw=MagicMock()
        pw.chromium.launch_persistent_context.return_value.pages=[MagicMock()]
        mgr=MagicMock()
        mgr.__enter__.return_value=pw
        mgr.__exit__.return_value=False
        return mgr

    def test_budget_ignores_historical_config_but_records_all_attempts(self):
        self.add(101,10)
        # User's exact scenario: historical repost budget at 5/5.
        for i in range(5):
            self.db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                            (101,900+i,'repost',engine.moment()))
        self.db.commit()
        run=app.begin_command_action_run(self.db,1,2,10)
        budget=app.CommandActionBudget(self.db,run,1,2)
        budget.current_group_id=101
        self.assertEqual(budget.available_actions({'like','repost'}),{'like','repost'})
        budget.reserve(10,'repost')
        budget.reserve(11,'like')
        budget.reserve(12,'repost')
        self.assertEqual(budget.available_actions({'like','repost'}),set())
        self.assertEqual(app.command_action_progress(self.db,run)['repost_attempts'],2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM action_attempts WHERE action="repost"').fetchone()[0],7)
        with self.assertRaises(engine.SafetyLimitReached):
            budget.reserve(13,'repost')

    def test_candidate_queue_is_api_only_and_prioritizes_half_done(self):
        self.add(101,11,delta=30,like='pending',repost='pending')
        self.add(102,12,delta=90,like='verified',repost='pending')
        self.add(101,13,api_post=False)
        self.add(101,14,delta=9*86400)
        self.add(101,15,exact=False)
        self.add(101,16,like='uncertain')
        got=app.command_action_candidates(self.db,self.groups,7,1,1)
        self.assertEqual([(x['group_id'],x['post_id']) for x in got],[(102,12),(101,11)])
        reposts_only=app.command_action_candidates(self.db,self.groups,7,0,1)
        self.assertEqual([(x['group_id'],x['post_id']) for x in reposts_only],[(102,12),(101,11)])
        self.assertEqual([x['post_id'] for x in app.command_action_candidates(self.db,self.groups,7,1,0)],[11])

    def test_run_two_posts_and_continue_without_double_clicks(self):
        self.add(101,11,delta=100,like='verified',repost='pending')
        self.add(102,12,delta=200,like='pending',repost='pending')
        clicks=[]
        def process(page,db,gid,row,pause,safety,actions,already_open):
            self.assertTrue(already_open)
            for action in ('like','repost'):
                if action in actions and row[action+'_status']=='pending':
                    safety.reserve(row['post_id'],action)
                    engine.status_write(db,gid,row['post_id'],action,'verified','VK confirmed')
                    clicks.append((gid,row['post_id'],action))
            return True
        with patch.object(app,'sync_playwright',return_value=self.browser()), \
             patch.object(engine,'PROFILE',self.root/'profile'), \
             patch.object(engine,'go_to_post'), \
             patch.object(multi,'exact_post_date_info',side_effect=lambda page,gid,pid:
                          {'publishedAt': self.now-(100 if pid==11 else 200)}), \
             patch.object(engine,'process_one',side_effect=process), \
             patch.object(engine,'configure_navigation_pacing'), \
             patch.object(engine,'watch_rate_limit_responses'), \
             patch.object(engine,'clear_post_access'),redirect_stdout(io.StringIO()):
            run=app.run_command_actions(self.db,self.cfg,self.groups,
                                        likes=1,reposts=2,max_posts=2,pause=20)
            # Second invocation finds no pending actions: no clicks repeated.
            again=app.run_command_actions(self.db,self.cfg,self.groups,
                                          likes=1,reposts=2,max_posts=2,pause=20)
        self.assertEqual(again,0)
        self.assertEqual(clicks,[(101,11,'repost'),(102,12,'like'),(102,12,'repost')])
        p=app.command_action_progress(self.db,run)
        self.assertEqual((p['like_attempts'],p['repost_attempts']), (1,2))
        self.assertEqual((p['like_verified'],p['repost_verified']), (1,2))
        self.assertEqual(p['posts'],2)
        self.assertEqual(self.db.execute('SELECT result FROM command_action_runs WHERE id=?',(run,)).fetchone()[0], 'complete')
        target=self.root/'report.csv'
        with redirect_stdout(io.StringIO()):
            self.assertEqual(app.export_command_action_results(self.db,self.groups,target),2)
            app.print_command_action_status(self.db)
        with target.open(encoding='utf-8-sig') as fh:
            rows=list(csv.DictReader(fh,delimiter=';'))
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r['reposted']=='да' for r in rows))
        self.assertEqual(sum(r['new_like_in_campaign']=='да' for r in rows),1)

    def test_interrupted_after_like_then_next_run_only_reposts(self):
        self.add(101,11,delta=100)
        state={'first':True}
        clicks=[]
        def fake_process(page,db,gid,row,pause,safety,actions,already_open):
            if state['first']:
                self.assertEqual(actions, {'like','repost'})
                safety.reserve(row['post_id'],'like')
                engine.status_write(db,gid,row['post_id'],'like','verified','VK confirmed')
                clicks.append('like')
                state['first']=False
                raise engine.StopRun('Тестовая остановка после подтверждённого лайка')
            self.assertEqual(actions, {'repost'})
            safety.reserve(row['post_id'],'repost')
            engine.status_write(db,gid,row['post_id'],'repost','verified','VK confirmed')
            clicks.append('repost')
            return True
        with patch.object(app,'sync_playwright',return_value=self.browser()), \
             patch.object(engine,'PROFILE',self.root/'profile'), \
             patch.object(engine,'go_to_post'), \
             patch.object(multi,'exact_post_date_info',return_value={'publishedAt':self.now-100}), \
             patch.object(engine,'process_one',side_effect=fake_process), \
             patch.object(engine,'configure_navigation_pacing'), \
             patch.object(engine,'watch_rate_limit_responses'), \
             patch.object(engine,'clear_post_access'),redirect_stdout(io.StringIO()):
            with self.assertRaises(engine.StopRun):
                app.run_command_actions(self.db,self.cfg,self.groups,
                                        likes=1,reposts=1,max_posts=1,pause=20)
            second=app.run_command_actions(self.db,self.cfg,self.groups,
                                           likes=1,reposts=1,max_posts=1,pause=20)
        self.assertEqual(clicks,['like','repost'])
        self.assertEqual(app.command_action_progress(self.db,second)['like_attempts'],0)
        self.assertEqual(app.command_action_progress(self.db,second)['repost_verified'],1)
        status=self.db.execute('SELECT result FROM command_action_runs ORDER BY id').fetchall()
        self.assertEqual([r[0] for r in status],['partial','partial'])

    def test_bad_live_date_stops_before_any_click(self):
        self.add(101,11,delta=100)
        with patch.object(app,'sync_playwright',return_value=self.browser()), \
             patch.object(engine,'PROFILE',self.root/'profile'), \
             patch.object(engine,'go_to_post'), \
             patch.object(multi,'exact_post_date_info',return_value={'publishedAt':self.now-999}), \
             patch.object(engine,'process_one') as process, \
             patch.object(engine,'configure_navigation_pacing'), \
             patch.object(engine,'watch_rate_limit_responses'),redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(engine.StopRun,'Дата'):
                app.run_command_actions(self.db,self.cfg,self.groups,
                                        likes=1,reposts=1,max_posts=2,pause=20)
        process.assert_not_called()
        latest=self.db.execute('SELECT * FROM command_action_runs ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(latest['result'],'partial')
        p=app.command_action_progress(self.db,latest['id'])
        self.assertEqual(p['like_attempts']+p['repost_attempts'],0)

    def test_vkontakte_lock_blocks_before_browser(self):
        self.add(101,11)
        engine.lock_on_platform_warning(self.db,0,'Too many requests per second')
        with patch.object(app,'sync_playwright') as browser:
            with self.assertRaises(engine.StopRun):
                app.run_command_actions(self.db,self.cfg,self.groups,
                                        likes=30,reposts=30,max_posts=60,pause=20)
            browser.assert_not_called()
        app.init_command_actions(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM command_action_runs').fetchone()[0],0)

    def test_cli_accepts_command_counts_without_json_quota_and_requires_confirm(self):
        cfg=json.loads((Path(app.__file__).parent/'vk_groups.json').read_text(encoding='utf-8'))
        cfg['groups']=[{**cfg['groups'][0], 'id':101,'url':'https://vk.com/club101'}]
        config=self.root/'cfg.json'
        config.write_text(json.dumps(cfg,ensure_ascii=False),encoding='utf-8')
        calls=[]
        def fake_run(db,cfg,groups,**kwargs):
            calls.append(kwargs)
            return 0
        with patch.object(engine,'DB_PATH',self.root/'state.sqlite'), \
             patch.object(app,'exclusive_run',return_value=nullcontext()), \
             patch.object(app,'run_command_actions',side_effect=fake_run), \
             patch.object(app,'export_command_action_results',return_value=0), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(['--run-actions','--likes','30','--reposts','30',
                                       '--max-posts','65','--confirm','--config',str(config)]),0)
            self.assertEqual(app.main(['--run-pairs','--likes','2','--reposts','3',
                                       '--max-posts','4','--confirm','--config',str(config)]),0)
        self.assertEqual((calls[0]['likes'],calls[0]['reposts'],calls[0]['max_posts']),(30,30,65))
        self.assertEqual((calls[1]['likes'],calls[1]['reposts'],calls[1]['max_posts']),(2,3,4))
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            app.main(['--run-actions','--likes','30','--reposts','30'])


if __name__=='__main__':
    unittest.main()
