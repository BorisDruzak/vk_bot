"""Community destination CLI/ledger regressions; no VK network or credentials."""
import csv
import io
import json
import tempfile
import unittest
from contextlib import closing, nullcontext, redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import vk_accounts as accounts
import vk_account_smoke as smoke
import vk_api_read as api
import vk_batch as engine
import vk_groups as groups
import vk_unified as app


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.first=self.root/'first.sqlite3'; self.second=self.root/'second.sqlite3'
        self.now=int(datetime.now(timezone.utc).timestamp())
        self.cfg=json.loads((Path(app.__file__).parent/'vk_groups.json').read_text(encoding='utf-8'))
        self.cfg['groups']=[{'id':101,'name':'Source','url':'https://vk.com/club101',
            'enabled':True,'like_enabled':True,'repost_enabled':True}]
        self.config=self.root/'groups.json'
        self.config.write_text(json.dumps(self.cfg),encoding='utf-8')
        self.profile=self.root/'profile';self.profile.mkdir()
        with closing(engine.db_open(self.first)) as db:
            groups.init_dates(db);api.init_api(db)
            engine.add_posts(db,101,[11,12])
            for pid in (11,12):
                groups.save_date(db,101,pid,self.now-100,api.API_SOURCE,True)
                db.execute('INSERT INTO api_seen_posts VALUES(?,?,?,?,1)',(101,pid,self.now-100,engine.moment()))
            db.execute("UPDATE posts SET like_status='already',repost_status='verified'")
            db.commit()

    def test_cli_routes_explicit_destination_before_legacy_handler(self):
        from types import SimpleNamespace
        seen=[]
        fake=SimpleNamespace(run_cli=lambda args,parser,**kw: seen.append(args) or 0)
        with patch.dict('sys.modules',{'vk_community':fake}),patch.object(app,'run_account2_mode',side_effect=AssertionError('legacy route')):
            for account in (1,2):
                self.assertEqual(app.main(['--account',str(account),'--run-actions',
                    '--reposts','2','--repost-community','501','--dry-run']),0)
        self.assertEqual([(a.account,a.repost_community,a.dry_run) for a in seen],[(1,501,True),(2,501,True)])

    def test_community_state_is_independent_of_personal_and_other_destinations(self):
        import vk_community as c
        with closing(engine.db_open(self.first)) as db:
            c.init_state(db,1001)
            rows=c.select_candidates(db,self.cfg['groups'],7,501,0,2,source_db=self.first)
            self.assertEqual([r['post_id'] for r in rows],[12,11])
            c.write_state(db,501,101,12,'verified',target_post_id=91)
            self.assertEqual([r['post_id'] for r in c.select_candidates(db,self.cfg['groups'],7,501,0,2,source_db=self.first)],[11])
            self.assertEqual(len(c.select_candidates(db,self.cfg['groups'],7,502,0,2,source_db=self.first)),2)
            self.assertEqual(db.execute('SELECT repost_status FROM posts WHERE post_id=12').fetchone()[0],'verified')
            with self.assertRaises(engine.StopRun):c.init_state(db,9999)

    def test_uncertain_destination_cannot_be_reserved_again(self):
        import vk_community as c
        with closing(engine.db_open(self.first)) as db:
            c.init_state(db,1001)
            run=c.begin_run(db,501,0,1,3)
            budget=c.CommunityBudget(db,run,501,0,1)
            budget.current_group_id=101
            budget.reserve(11,'repost')
            self.assertEqual(c.get_state(db,501,101,11)['status'],'uncertain')
            self.assertEqual(c.progress(db,run)['repost_attempts'],1)
            self.assertEqual(c.progress(db,run)['repost_verified'],0)
            other=c.CommunityBudget(db,c.begin_run(db,501,0,1,3),501,0,1)
            other.current_group_id=101
            with self.assertRaises(engine.StopRun):other.reserve(11,'repost')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],1)

    def test_current_source_invalidation_is_respected_by_second_account(self):
        import vk_community as c
        with closing(engine.db_open(self.second)) as db:
            c.init_state(db,2002)
            self.assertEqual(len(c.select_candidates(db,self.cfg['groups'],7,501,0,2,source_db=self.first)),2)
            with closing(engine.db_open(self.first)) as source:
                source.execute('UPDATE api_seen_posts SET valid=0 WHERE post_id=12');source.commit()
            self.assertEqual([r['post_id'] for r in c.select_candidates(db,self.cfg['groups'],7,501,0,2,source_db=self.first)],[11])

    @staticmethod
    def browser():
        pw=MagicMock();pw.chromium.launch_persistent_context.return_value.pages=[MagicMock()]
        cm=MagicMock();cm.__enter__.return_value=pw;cm.__exit__.return_value=False
        return cm

    def runner_patches(self,c):
        from contextlib import ExitStack
        stack=ExitStack()
        stack.enter_context(patch.object(c,'sync_playwright',return_value=self.browser()))
        stack.enter_context(patch.object(c,'live_identity'))
        stack.enter_context(patch.object(engine,'go_to_post'))
        stack.enter_context(patch.object(engine,'watch_rate_limit_responses'))
        stack.enter_context(patch.object(engine,'configure_navigation_pacing'))
        stack.enter_context(patch.object(groups,'exact_post_date_info',return_value={'publishedAt':self.now-100}))
        stack.enter_context(patch('time.sleep'))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return stack

    def test_dry_run_never_submits_likes_or_reserves_attempts(self):
        import vk_community as c
        with closing(engine.db_open(self.first)) as db:
            c.init_state(db,1001)
            with self.runner_patches(c),patch.object(c.ui,'prepare',return_value={'community_id':501,'community_label':'Target'}) as prep,\
                 patch.object(c.ui,'submit',side_effect=AssertionError('real submit')) as submit,\
                 patch.object(engine,'process_one',side_effect=AssertionError('like click')),patch('time.sleep') as sleep:
                result=c.run_actions(db,self.cfg,self.cfg['groups'],community_id=501,likes=1,reposts=2,
                    max_posts=2,pause=20,profile=self.profile,expected_vk_id=1001,source_db=self.first,dry_run=True)
            self.assertEqual(result['prepared'],2)
            self.assertEqual(prep.call_count,2);self.assertEqual(submit.call_count,0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM community_runs').fetchone()[0],0)
            sleep.assert_not_called()

    def test_repost_pause_precedes_verification_and_next_publish(self):
        import vk_community as c
        events=[];scans=0
        with closing(engine.db_open(self.first)) as db:
            c.init_state(db,1001)
            def submit(page,prepared,*,before_publish,identity_guard=None):
                before_publish();events.append('send')
            def sleep(seconds):
                self.assertEqual(seconds,20)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM community_reposts WHERE status='uncertain'").fetchone()[0],1)
                events.append('pause')
            def scan(*args):
                nonlocal scans
                scans+=1;events.append('precheck' if scans%2 else 'verify')
                return [] if scans%2 else [{'post_id':90+scans,'published_at':self.now}]
            with self.runner_patches(c),patch.object(c.ui,'prepare',return_value={'community_id':501}),\
                 patch.object(c.ui,'submit',side_effect=submit),patch.object(c.ui,'scan_destination',side_effect=scan),\
                 patch('time.sleep',side_effect=sleep):
                result=c.run_actions(db,self.cfg,self.cfg['groups'],community_id=501,likes=0,reposts=2,
                    max_posts=2,pause=20,profile=self.profile,expected_vk_id=1001,source_db=self.first)
            self.assertEqual(result['repost_verified'],2)
            self.assertEqual(events,['precheck','send','pause','verify']*2)

    def test_uncertain_send_still_sleeps_and_does_not_retry(self):
        import vk_community as c
        with closing(engine.db_open(self.first)) as db:
            c.init_state(db,1001)
            def submit(page,prepared,*,before_publish,identity_guard=None):
                before_publish();raise engine.StopRun('send outcome unknown')
            with self.runner_patches(c),patch.object(c.ui,'prepare',return_value={'community_id':501}),\
                 patch.object(c.ui,'submit',side_effect=submit) as send,\
                 patch.object(c.ui,'scan_destination',return_value=[]) as scan,patch('time.sleep') as sleep:
                with self.assertRaisesRegex(engine.StopRun,'send outcome unknown'):
                    c.run_actions(db,self.cfg,self.cfg['groups'],community_id=501,likes=0,reposts=2,
                        max_posts=2,pause=20,profile=self.profile,expected_vk_id=1001,source_db=self.first)
            sleep.assert_called_once_with(20)
            self.assertEqual(send.call_count,1);self.assertEqual(scan.call_count,1)
            self.assertEqual(c.get_state(db,501,101,12)['status'],'uncertain')

    def test_actual_run_verifies_new_target_post_and_does_not_change_personal_repost(self):
        import vk_community as c
        with closing(engine.db_open(self.first)) as db:
            c.init_state(db,1001)
            def submit(page,prepared,*,before_publish,identity_guard=None):before_publish()
            with self.runner_patches(c),patch.object(c.ui,'prepare',return_value={'community_id':501,'community_label':'Target'}),\
                 patch.object(c.ui,'submit',side_effect=submit),\
                 patch.object(c.ui,'scan_destination',side_effect=[[],[{'post_id':91,'published_at':self.now}]]):
                result=c.run_actions(db,self.cfg,self.cfg['groups'],community_id=501,likes=0,reposts=1,
                    max_posts=2,pause=20,profile=self.profile,expected_vk_id=1001,source_db=self.first)
            self.assertEqual(result['repost_verified'],1)
            self.assertEqual(c.get_state(db,501,101,12)['target_post_id'],91)
            self.assertEqual(db.execute('SELECT repost_status FROM posts WHERE post_id=12').fetchone()[0],'verified')
            dest=self.root/'target.csv';c.export_results(db,501,dest)
            with dest.open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f,delimiter=';'))
            self.assertEqual(rows[0]['community_id'],'501')
            self.assertEqual(rows[0]['target_post_url'],'https://vk.com/wall-501_91')

    def test_missing_or_old_confirmation_preserves_uncertain_and_stops(self):
        import vk_community as c
        for evidence in ([],[{'post_id':91,'published_at':None}],[{'post_id':91,'published_at':self.now-3600}]):
            with self.subTest(evidence=evidence),closing(engine.db_open(self.root/f'target{len(str(evidence))}.sqlite')) as db:
                c.init_state(db,1001)
                def submit(page,prepared,*,before_publish,identity_guard=None):before_publish()
                with self.runner_patches(c),patch.object(c.ui,'prepare',return_value={'community_id':501,'community_label':'Target'}),\
                     patch.object(c.ui,'submit',side_effect=submit),patch.object(c.ui,'scan_destination',side_effect=[[],evidence]):
                    with self.assertRaises(engine.StopRun):
                        c.run_actions(db,self.cfg,self.cfg['groups'],community_id=501,likes=0,reposts=1,
                            max_posts=2,pause=20,profile=self.profile,expected_vk_id=1001,source_db=self.first)
                self.assertEqual(c.get_state(db,501,101,12)['status'],'uncertain')
                self.assertEqual(c.progress(db,1)['repost_verified'],0)

    def test_cli_rejects_invalid_destination_or_unsafe_flag_combinations(self):
        cases=[['--preview','--repost-community','-1'],['--status','--repost-community','501'],
               ['--run-actions','--reposts','1','--repost-community','501'],
               ['--run-actions','--reposts','1','--repost-community','501','--dry-run','--confirm'],
               ['--run-actions','--reposts','1','--dry-run']]
        for args in cases:
            with self.subTest(args=args),redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):app.main(args)

    def test_real_cli_dry_run_uses_both_account_databases_without_attempts(self):
        import vk_community as c
        with patch.object(engine,'DB_PATH',self.first),patch.object(smoke,'ACCOUNT2_DB',self.second),\
             patch.object(app,'exclusive_run',return_value=nullcontext()),\
             patch.dict(accounts.PROFILE_DIRS,{1:self.profile,2:self.profile}),\
             patch.dict(accounts.LOCK_DIRS,{2:self.root/'second.lock'}),\
             patch.object(c,'pinned_identity',side_effect=lambda n:1001 if n==1 else 2002),\
             self.runner_patches(c),patch.object(c.ui,'prepare',return_value={'community_id':501,'community_label':'Target'}) as prep,\
             patch.object(c.ui,'submit',side_effect=AssertionError('real submit')):
            for account in (1,2):
                self.assertEqual(app.main(['--account',str(account),'--run-actions','--reposts','2',
                    '--repost-community','501','--dry-run','--config',str(self.config),'--max-posts','2']),0)
        self.assertEqual(prep.call_count,4)
        for path,vk_id in ((self.first,1001),(self.second,2002)):
            with closing(engine.db_open(path)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT vk_id FROM community_account_identity').fetchone()[0],vk_id)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM community_runs').fetchone()[0],0)
        with closing(engine.db_open(self.second)) as db:
            self.assertEqual({r[0] for r in db.execute('SELECT repost_status FROM posts')},{'pending'})

    def test_local_preview_and_status_work_without_browser_profile(self):
        import vk_community as c
        missing=self.root/'missing-profile'
        with patch.object(engine,'DB_PATH',self.first),patch.object(smoke,'ACCOUNT2_DB',self.second),\
             patch.object(app,'exclusive_run',return_value=nullcontext()),\
             patch.dict(accounts.PROFILE_DIRS,{1:missing,2:missing}),\
             patch.dict(accounts.LOCK_DIRS,{2:self.root/'second.lock'}),\
             patch.object(c,'pinned_identity',side_effect=lambda n:1001 if n==1 else 2002),\
             patch.object(c,'sync_playwright',side_effect=AssertionError('local report launched browser')),\
             redirect_stdout(io.StringIO()):
            for account in (1,2):
                for mode in ('--preview','--action-status'):
                    with self.subTest(account=account,mode=mode):
                        self.assertEqual(app.main(['--account',str(account),mode,'--repost-community','501',
                            '--config',str(self.config),'--report-file',str(self.root/f'report-{account}.csv')]),0)
                self.assertEqual(app.main(['--account',str(account),'--run-actions','--reposts','1',
                    '--repost-community','501','--dry-run','--config',str(self.config)]),2)

    def test_identity_failure_stops_before_chooser_or_attempt(self):
        import vk_community as c
        with closing(engine.db_open(self.first)) as db:
            c.init_state(db,1001)
            with self.runner_patches(c),patch.object(c,'live_identity',side_effect=engine.StopRun('ID mismatch')),\
                 patch.object(c.ui,'prepare') as prep,patch.object(c.ui,'submit') as submit:
                with self.assertRaises(engine.StopRun):
                    c.run_actions(db,self.cfg,self.cfg['groups'],community_id=501,likes=0,reposts=1,
                        max_posts=2,pause=20,profile=self.profile,expected_vk_id=1001,source_db=self.first,dry_run=True)
            self.assertEqual(prep.call_count,0);self.assertEqual(submit.call_count,0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)

    def test_chooser_failure_keeps_pending_and_does_not_reserve(self):
        import vk_community as c
        with closing(engine.db_open(self.first)) as db:
            c.init_state(db,1001)
            with self.runner_patches(c),patch.object(c.ui,'prepare',side_effect=engine.StopRun('target missing')),\
                 patch.object(c.ui,'scan_destination',return_value=[]):
                with self.assertRaises(engine.StopRun):
                    c.run_actions(db,self.cfg,self.cfg['groups'],community_id=501,likes=0,reposts=1,
                        max_posts=2,pause=20,profile=self.profile,expected_vk_id=1001,source_db=self.first)
            self.assertEqual(c.get_state(db,501,101,12)['status'],'pending')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)

    def test_independent_like_and_community_repost_counts(self):
        import vk_community as c
        with closing(engine.db_open(self.first)) as db:
            c.init_state(db,1001)
            db.execute("UPDATE posts SET like_status='pending'");db.commit()
            def submit(page,prepared,*,before_publish,identity_guard=None):before_publish()
            def like(page,db,gid,row,pause,*,safety,actions,already_open):
                self.assertEqual(actions,{'like'});safety.reserve(row['post_id'],'like')
                engine.status_write(db,gid,row['post_id'],'like','verified','offline')
            evidence=[[],[{'post_id':91,'published_at':self.now}],[],[{'post_id':92,'published_at':self.now}]]
            with self.runner_patches(c),patch.object(c.ui,'prepare',return_value={'community_id':501,'community_label':'Target'}),\
                 patch.object(c.ui,'submit',side_effect=submit),patch.object(c.ui,'scan_destination',side_effect=evidence),\
                 patch.object(engine,'process_one',side_effect=like):
                result=c.run_actions(db,self.cfg,self.cfg['groups'],community_id=501,likes=1,reposts=2,
                    max_posts=2,pause=20,profile=self.profile,expected_vk_id=1001,source_db=self.first)
            self.assertEqual((result['like_verified'],result['repost_verified']),(1,2))
            self.assertEqual((result['like_attempts'],result['repost_attempts']),(1,2))
            self.assertEqual(db.execute('SELECT result FROM community_runs').fetchone()[0],'complete')


if __name__=='__main__':unittest.main()
