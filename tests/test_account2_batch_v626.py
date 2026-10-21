"""v6.26: read-only shared post catalog, separately bound account #2 action ledger.

No network, no credentials, no real VK UI. All profile checks are faked.
"""
import io
import csv
import json
import tempfile
import unittest
from contextlib import closing, nullcontext, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import vk_account_batch as catalog
import vk_account_smoke as smoke
import vk_accounts as accounts
import vk_api_read as api
import vk_batch as engine
import vk_groups as multi
import vk_unified as app


class Account2BatchTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.first=self.root/'first.sqlite3'
        self.second=self.root/'second.sqlite3'
        self.profile=self.root/'profile2'
        self.profile.mkdir()
        self.gid=188206921
        self.now=int(datetime.now(timezone.utc).timestamp())
        self.date=self.now-3600
        self.cfg={'groups':[{'id':self.gid,'name':'Администрация','enabled':True,
                             'like_enabled':True,'repost_enabled':True}],
                  'actions':{'like':True,'repost':True},
                  'lookback_days':7, 'action_pause_seconds':0,
                  'read_pause_seconds':20.0}
        with closing(engine.db_open(self.first)) as db:
            multi.init_dates(db)
            api.init_api(db)
            for pid,timestamp,valid,provenance in [
                (13418,self.date,1,True),
                (13419,self.date-3600,1,True),
                (13420,self.date-1800,1,False),
                (13421,self.now-900000,1,True),
                (13422,self.date,0,True)]:
                engine.add_posts(db,self.gid,[pid])
                multi.save_date(db,self.gid,pid,timestamp,
                                api.API_SOURCE if provenance else 'feed-approx',provenance)
                if provenance:
                    db.execute('INSERT INTO api_seen_posts(group_id,post_id,published_at,seen_at,valid) '
                               'VALUES(?,?,?,?,?)', (self.gid,pid,timestamp,engine.moment(),valid))
            db.execute('UPDATE posts SET like_status=?,repost_status=? WHERE post_id=13418',
                       ('verified','verified'))
            db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) "
                       "VALUES(?,?,?,?)",(self.gid,13418,'like',engine.moment()))
            engine.lock_on_platform_warning(db,self.gid,'historical first-account warning')
            db.commit()

    def bind_second(self,db):
        smoke.init_account2(db,202002)

    def test_import_only_exact_public_ids_never_first_status_or_safety(self):
        with closing(engine.db_open(self.second)) as db:
            self.bind_second(db)
            count=catalog.refresh_public_catalog(db,source_db=self.first,
                        target_db=self.second,days=7,now=self.now)
            self.assertEqual(count,2)
            self.assertEqual(tuple(x[0] for x in db.execute('SELECT post_id FROM posts ORDER BY post_id')),
                             (13418,13419))
            row=db.execute('SELECT like_status,repost_status FROM posts WHERE post_id=13418').fetchone()
            self.assertEqual(tuple(row),('pending','pending'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM safety_lock').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT vk_id FROM local_account_identity').fetchone()[0],202002)
            rows=app.command_action_candidates(db,self.cfg['groups'],7,1,1)
            self.assertEqual([r['post_id'] for r in rows],[13418,13419])
        with closing(engine.db_open(self.first)) as db:
            self.assertEqual(db.execute('SELECT like_status FROM posts WHERE post_id=13418').fetchone()[0],
                             'verified')

    def test_preserves_second_status_and_attempt_history_across_refresh(self):
        with closing(engine.db_open(self.second)) as db:
            self.bind_second(db)
            catalog.refresh_public_catalog(db,source_db=self.first,target_db=self.second,now=self.now)
            engine.status_write(db,self.gid,13418,'like','verified','account2 verified')
            engine.status_write(db,self.gid,13418,'repost','uncertain','unconfirmed')
            catalog.refresh_public_catalog(db,source_db=self.first,target_db=self.second,now=self.now)
            row=db.execute('SELECT like_status,repost_status FROM posts WHERE post_id=13418').fetchone()
            self.assertEqual(tuple(row),('verified','uncertain'))
            self.assertNotIn(13418,[r['post_id'] for r in app.command_action_candidates(
                db,self.cfg['groups'],7,1,1)])

    def test_disallow_same_catalog_database(self):
        with closing(engine.db_open(self.first)) as db:
            with self.assertRaises(catalog.CatalogError):
                catalog.refresh_public_catalog(db,source_db=self.first,target_db=self.first)

    def test_missing_primary_never_creates_one(self):
        missing=self.root/'missing.sqlite3'
        with closing(engine.db_open(self.second)) as db:
            with self.assertRaises(catalog.CatalogError):
                catalog.refresh_public_catalog(db,source_db=missing,target_db=self.second)
        self.assertFalse(missing.exists())

    def test_invalid_second_identity_is_not_adopted(self):
        with closing(engine.db_open(self.second)) as db:
            self.bind_second(db)
            with self.assertRaises(smoke.SmokeError):
                smoke.init_account2(db,202003)
            self.assertEqual(db.execute('SELECT vk_id FROM local_account_identity').fetchone()[0],202002)

    def test_local_account_2_preview_and_status_are_independent(self):
        with patch.object(smoke,'ACCOUNT2_DB',self.second),\
             patch.object(engine,'DB_PATH',self.first),\
             patch.object(smoke,'pinned_user_id',return_value=202002),\
             patch.object(accounts,'profile_for_account',return_value=self.profile),\
             patch.object(accounts,'exclusive_profile',return_value=nullcontext()),\
             patch.object(app,'exclusive_run',return_value=nullcontext()),\
             patch.object(multi,'load_config',return_value=self.cfg),\
             patch.object(multi,'selected_groups',return_value=self.cfg['groups']),\
             patch.object(app,'sync_playwright',side_effect=AssertionError('preview opened browser')):
            import argparse
            opts=argparse.Namespace(run_actions=False,action_status=False,preview=True,
                likes=None,reposts=None,confirm=False,read_pause=None,max_posts=3,
                config=self.root/'cfg.json',only=None,report_file=self.root/'vk_actions_results_account_2.csv',
                test_actions=None,run_pairs=False,run=False,discover=False)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(app.run_account2_mode(opts,argparse.ArgumentParser()),0)
            self.assertIn('13418',output.getvalue())
            opts.preview=False;opts.action_status=True
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(app.run_account2_mode(opts,argparse.ArgumentParser()),0)
            self.assertIn('аккаунт=2',output.getvalue())
            self.assertTrue((self.root/'vk_actions_results_account_2.csv').exists())

    @staticmethod
    def fake_browser():
        browser=MagicMock()
        browser.chromium.launch_persistent_context.return_value.pages=[MagicMock()]
        cm=MagicMock();cm.__enter__.return_value=browser;cm.__exit__.return_value=False
        return cm

    def test_identity_rechecked_before_each_reserved_click(self):
        with closing(engine.db_open(self.second)) as db:
            self.bind_second(db)
            catalog.refresh_public_catalog(db,source_db=self.first,target_db=self.second,now=self.now)
            checks=[]
            def guard(context):
                checks.append(context)
            def fake_process(page, database, gid, row, pause, safety, actions, already_open):
                self.assertTrue(already_open)
                for action in sorted(actions):
                    safety.reserve(row['post_id'],action)
                    engine.status_write(database,gid,row['post_id'],action,'verified','fake VK success')
            with patch.object(app,'sync_playwright',return_value=self.fake_browser()),\
                 patch.object(engine,'go_to_post'),\
                 patch.object(multi,'exact_post_date_info',return_value={'publishedAt':self.date}),\
                 patch.object(engine,'watch_rate_limit_responses'),\
                 patch.object(engine,'configure_navigation_pacing'),\
                 patch.object(engine,'process_one',side_effect=fake_process):
                with redirect_stdout(io.StringIO()):
                    app.run_command_actions(db,self.cfg,self.cfg['groups'],likes=1,reposts=1,
                        max_posts=1,pause=20,profile=self.profile,identity_guard=guard)
            self.assertGreaterEqual(len(checks),3)  # once after navigation, once before each real click
            self.assertEqual(dict(db.execute('SELECT action,COUNT(*) FROM action_attempts GROUP BY action')),
                             {'like':1,'repost':1})
            self.assertEqual(app.command_action_progress(db,1)['like_verified'],1)

    def test_failed_identity_prevents_any_attempt(self):
        with closing(engine.db_open(self.second)) as db:
            self.bind_second(db)
            catalog.refresh_public_catalog(db,source_db=self.first,target_db=self.second,now=self.now)
            def deny(context): raise smoke.SmokeError('VK ID mismatch')
            with patch.object(app,'sync_playwright',return_value=self.fake_browser()),\
                 patch.object(engine,'go_to_post'),\
                 patch.object(multi,'exact_post_date_info',return_value={'publishedAt':self.date}),\
                 patch.object(engine,'watch_rate_limit_responses'),\
                 patch.object(engine,'configure_navigation_pacing'),\
                 patch.object(engine,'process_one',side_effect=AssertionError('Should not click')):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(smoke.SmokeError):
                        app.run_command_actions(db,self.cfg,self.cfg['groups'],likes=1,reposts=1,
                            max_posts=1,pause=20,profile=self.profile,identity_guard=deny)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT result FROM command_action_runs').fetchone()[0],
                             'partial')

    def test_cli_five_likes_two_reposts_uses_only_second_account(self):
        """Catch wrong CLI routing, shared ledgers and unequal-count overshoot."""
        with closing(engine.db_open(self.first)) as db:
            for pid in range(14000, 14008):
                engine.add_posts(db,self.gid,[pid])
                multi.save_date(db,self.gid,pid,self.date,api.API_SOURCE,True)
                db.execute('INSERT INTO api_seen_posts VALUES(?,?,?,?,1)',
                           (self.gid,pid,self.date,engine.moment()))
            # Reactions and the active warning of account #1 must not prevent
            # the independent account #2 from acting on the same public posts.
            db.execute("UPDATE posts SET like_status='verified',repost_status='verified'")
            db.commit()
        with closing(engine.db_open(self.second)) as db:
            self.bind_second(db)
            engine.add_posts(db,self.gid,[13418])
            for action in ('like','repost'):
                engine.status_write(db,self.gid,13418,action,'verified','prior account2 action')

        cfg=json.loads((Path(app.__file__).parent/'vk_groups.json').read_text(encoding='utf-8'))
        cfg['groups']=[{**g,'url':f'https://vk.com/club{g["id"]}'} for g in self.cfg['groups']]
        config=self.root/'config.json'
        config.write_text(json.dumps(cfg,ensure_ascii=False),encoding='utf-8')
        report=self.root/'results2.csv'
        first_before=self.first.read_bytes()
        clicks=[]
        checks=[]
        browser=self.fake_browser()
        def process(page,db,gid,row,pause,safety,actions,already_open):
            for action in sorted(actions):
                safety.reserve(row['post_id'],action)
                engine.status_write(db,gid,row['post_id'],action,'verified','fake VK success')
                clicks.append((row['post_id'],action))
        with patch.object(smoke,'ACCOUNT2_DB',self.second),\
             patch.object(engine,'DB_PATH',self.first),\
             patch.object(smoke,'pinned_user_id',return_value=202002),\
             patch.object(smoke,'_live_identity',side_effect=lambda ctx,uid: checks.append(uid)),\
             patch.dict(accounts.PROFILE_DIRS,{2:self.profile}),\
             patch.dict(accounts.LOCK_DIRS,{2:self.root/'profile2.lock'}),\
             patch.object(app,'exclusive_run',return_value=nullcontext()),\
             patch.object(app,'sync_playwright',return_value=browser),\
             patch.object(engine,'go_to_post'),\
             patch.object(multi,'exact_post_date_info',return_value={'publishedAt':self.date}),\
             patch.object(engine,'watch_rate_limit_responses'),\
             patch.object(engine,'configure_navigation_pacing'),\
             patch.object(engine,'process_one',side_effect=process),\
             redirect_stdout(io.StringIO()) as output:
            result=app.main(['--account','2','--run-actions','--likes','5','--reposts','2',
                             '--max-posts','10','--confirm','--config',str(config),
                             '--report-file',str(report)])
        self.assertEqual(result,0,output.getvalue())
        self.assertEqual(sum(a=='like' for _,a in clicks),5)
        self.assertEqual(sum(a=='repost' for _,a in clicks),2)
        self.assertEqual(len(clicks),len(set(clicks)))
        self.assertNotIn(13418,[pid for pid,_ in clicks])
        self.assertEqual(set(checks),{202002})
        self.assertGreaterEqual(len(checks),7)
        launch=browser.__enter__.return_value.chromium.launch_persistent_context
        self.assertEqual(launch.call_args.kwargs['user_data_dir'],str(self.profile))
        with closing(engine.db_open(self.second)) as db:
            progress=app.command_action_progress(db,1)
            self.assertEqual((progress['like_verified'],progress['repost_verified']),(5,2))
            self.assertLessEqual(progress['posts'],10)
            self.assertEqual(db.execute('SELECT result FROM command_action_runs').fetchone()[0],
                             'complete')
        self.assertEqual(self.first.read_bytes(),first_before)
        with report.open(encoding='utf-8-sig') as stream:
            rows=list(csv.DictReader(stream,delimiter=';'))
        self.assertEqual(sum(r['new_like_in_campaign']=='да' for r in rows),5)
        self.assertEqual(sum(r['new_repost_in_campaign']=='да' for r in rows),2)


if __name__=='__main__':
    unittest.main()
