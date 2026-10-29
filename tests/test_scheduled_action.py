"""Single-action scheduler execution, temporary DBs and mocked browser only."""
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, closing, nullcontext, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import vk_accounts as accounts
import vk_account_smoke as smoke
import vk_api_read as api
import vk_batch as engine
import vk_community as community
import vk_groups as groups
import vk_unified as app


class ScheduledActionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.first=self.root/'first.sqlite3';self.second=self.root/'second.sqlite3'
        self.profile=self.root/'profile';self.profile.mkdir()
        self.now=int(datetime.now(timezone.utc).timestamp());self.stamp=self.now-100
        self.row=dict(group_id=101,post_id=11,published_at=self.stamp,community_id=501)
        cfg=json.loads((Path(app.__file__).parent/'vk_groups.json').read_text(encoding='utf-8'))
        cfg['groups']=[dict(id=101,name='Source',url='https://vk.com/club101',enabled=True,like_enabled=True,repost_enabled=True)]
        self.config=self.root/'groups.json';self.config.write_text(json.dumps(cfg),encoding='utf-8')
        with closing(engine.db_open(self.first)) as db:
            groups.init_dates(db);api.init_api(db);engine.add_posts(db,101,[11])
            groups.save_date(db,101,11,self.stamp,api.API_SOURCE,True)
            db.execute('INSERT INTO api_seen_posts VALUES(?,?,?,?,1)',(101,11,self.stamp,engine.moment()));db.commit()

    def patches(self,module):
        stack=ExitStack()
        stack.enter_context(patch.object(engine,'DB_PATH',self.first))
        stack.enter_context(patch.object(smoke,'ACCOUNT2_DB',self.second))
        stack.enter_context(patch.object(app,'exclusive_run',return_value=nullcontext()))
        stack.enter_context(patch.object(accounts,'exclusive_profile',return_value=nullcontext()))
        stack.enter_context(patch.object(accounts,'profile_for_account',return_value=self.profile))
        stack.enter_context(patch.object(community,'pinned_identity',side_effect=lambda account:1001 if account==1 else 2002))
        stack.enter_context(patch.object(community,'live_identity'))
        stack.enter_context(patch.object(engine,'go_to_post'))
        stack.enter_context(patch.object(engine,'ensure_ok'))
        stack.enter_context(patch.object(engine,'watch_rate_limit_responses'))
        stack.enter_context(patch.object(engine,'configure_navigation_pacing'))
        stack.enter_context(patch.object(module.availability,'confirmed_denial',return_value=None))
        stack.enter_context(patch.object(groups,'exact_post_date_info',return_value={'publishedAt':self.stamp}))
        stack.enter_context(patch.object(community.ui,'repost_reference',create=True,return_value={
            'group_id':101,'post_id':11,'published_at':self.stamp}))
        self.context=MagicMock();self.page=self.context.new_page.return_value
        self.page.is_closed.return_value=False
        pw=MagicMock();pw.chromium.launch_persistent_context.return_value=self.context
        cm=MagicMock();cm.__enter__.return_value=pw;cm.__exit__.return_value=False
        stack.enter_context(patch.object(module,'sync_playwright',return_value=cm))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return stack

    def execute(self,module,account,kind,**callbacks):
        return module.execute_action(account,kind,self.row,config_path=self.config,**callbacks)

    def test_typed_initial_chooser_timeout_survives_executor_without_intent_or_send(self):
        import vk_scheduled_action as scheduled
        with self.patches(scheduled),patch.object(community.ui,'scan_destination',return_value=[]),\
             patch.object(community.ui,'prepare',side_effect=community.ui.CommunityPreparationTimeout()),\
             patch.object(community.ui,'submit') as submit,patch('time.sleep'):
            before=MagicMock();record=MagicMock()
            with self.assertRaises(community.ui.CommunityPreparationTimeout) as error:
                self.execute(scheduled,1,'community',before_attempt=before,record_attempt=record)
        self.assertEqual(error.exception.stage,'community_prepare')
        before.assert_not_called();record.assert_not_called();submit.assert_not_called()
        with closing(engine.db_open(self.first)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM action_attempts').fetchone()[0],0)

    def test_personal_and_like_are_separate_single_attempts_for_both_accounts(self):
        import vk_scheduled_action as scheduled
        for account,kind in ((1,'like'),(2,'personal')):
            events=[];wanted='like' if kind=='like' else 'repost'
            def process(page,db,gid,row,pause,*,safety,actions,already_open):
                self.assertEqual(actions,{wanted});self.assertTrue(already_open)
                safety.reserve(row['post_id'],wanted)
                events.append('click')
                engine.status_write(db,gid,row['post_id'],wanted,'verified','offline')
            def record(attempt_id):
                path=self.first if account==1 else self.second
                with closing(engine.db_open(path)) as db:
                    self.assertEqual(db.execute('SELECT action FROM action_attempts WHERE id=?',(attempt_id,)).fetchone()[0],wanted)
                events.append('record')
            with self.patches(scheduled),patch.object(engine,'process_one',side_effect=process),\
                 patch('time.sleep',side_effect=lambda seconds:events.append(('sleep',seconds))):
                result=self.execute(scheduled,account,kind,before_attempt=lambda:events.append('guard'),record_attempt=record)
            self.assertEqual(result['status'],'verified');self.assertIsInstance(result['attempt_id'],int)
            self.assertEqual(events,['guard','record','click',('sleep',20)])
            self.context.new_page.assert_called_once();self.page.close.assert_called_once();self.context.close.assert_called_once()

    def test_community_uses_separate_ledger_and_fresh_target_proof(self):
        import vk_scheduled_action as scheduled
        events=[]
        def submit(page,proof,*,before_publish,identity_guard=None):
            before_publish();events.append('click')
        with self.patches(scheduled),patch.object(engine,'process_one',side_effect=AssertionError('wrong personal engine')),\
             patch.object(community.ui,'prepare',return_value={'community_id':501}),\
             patch.object(community.ui,'submit',side_effect=submit),\
             patch.object(community.ui,'scan_destination',return_value=[]),\
             patch.object(community.ui,'confirm_destination',return_value={'post_id':91,'published_at':self.now}) as confirm,\
             patch('time.sleep',side_effect=lambda seconds:events.append(('sleep',seconds))):
            result=self.execute(scheduled,2,'community',before_attempt=lambda:events.append('guard'),record_attempt=lambda _:events.append('record'))
        self.assertEqual(result,{'status':'verified','attempt_id':1,'target_post_id':91})
        self.assertEqual(events,['guard','record','click',('sleep',20)])
        self.assertEqual(confirm.call_count,1)
        self.assertIs(type(confirm.call_args.kwargs['not_before']),int)
        with closing(engine.db_open(self.second)) as db:
            self.assertEqual(community.get_state(db,501,101,11)['status'],'verified')
            self.assertEqual(db.execute('SELECT repost_status FROM posts').fetchone()[0],'pending')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM community_run_attempts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM command_action_attempts').fetchone()[0],0)

    def test_community_diagnostic_failure_preserves_final_guard_attempt_and_cooldown(self):
        import vk_scheduled_action as scheduled
        events=[]
        def diagnostic(event,**data):
            if event=='final_click_check':events.append('diagnostic')
            raise RuntimeError('telemetry unavailable')
        def submit(page,proof,*,before_publish,identity_guard=None,before_final_click=None):
            before_publish()
            self.assertEqual(before_final_click(),700)
            events.append('click')
        with self.patches(scheduled),patch.object(community.ui,'prepare',return_value={'community_id':501}),\
             patch.object(community.ui,'submit',side_effect=submit),\
             patch.object(community.ui,'scan_destination',return_value=[]),\
             patch.object(community.ui,'confirm_destination',return_value={'post_id':91,'published_at':self.now}),\
             patch('time.sleep',side_effect=lambda seconds:events.append(('sleep',seconds))):
            def guard():events.append('guard');return 700
            result=self.execute(scheduled,1,'community',before_attempt=lambda:None,record_attempt=lambda _:None,
                                before_click=guard,diagnostic=diagnostic)
        self.assertEqual(result['status'],'verified')
        self.assertEqual(events,['diagnostic','guard','click',('sleep',20)])
        with closing(engine.db_open(self.first)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],1)

    def test_browser_open_failure_has_accurate_stage(self):
        import vk_scheduled_action as scheduled
        with self.patches(scheduled),patch.object(engine,'process_one') as process,patch('time.sleep') as sleep:
            self.context.new_page.side_effect=scheduled.PlaywrightError('private browser error')
            with self.assertRaises(scheduled.ScheduledActionStopped) as stopped:
                self.execute(scheduled,1,'like',before_attempt=lambda:None,record_attempt=lambda _:None)
        self.assertEqual(stopped.exception.stage,'browser_open')
        process.assert_not_called();sleep.assert_not_called()

    def test_page_cleanup_failure_preserves_verified_result_if_context_closed_and_cooled(self):
        import vk_scheduled_action as scheduled
        events=[]
        def process(page,db,gid,row,pause,*,safety,**kw):
            safety.reserve(row['post_id'],'like')
            engine.status_write(db,gid,row['post_id'],'like','verified','offline')
        with self.patches(scheduled),patch.object(engine,'process_one',side_effect=process) as action,patch('time.sleep') as sleep:
            self.page.close.side_effect=scheduled.PlaywrightError('private close error')
            result=self.execute(scheduled,1,'like',before_attempt=lambda:None,record_attempt=lambda _:None,
                diagnostic=lambda event,**data:events.append((event,data)))
        self.assertEqual(result['status'],'verified')
        self.assertEqual(sum(event=='cleanup_warning' for event,data in events),1)
        self.context.close.assert_called_once()
        self.assertEqual(action.call_count,1);sleep.assert_called_once_with(20)

    def test_context_close_failure_still_blocks_without_repeating_attempt(self):
        import vk_scheduled_action as scheduled
        def process(page,db,gid,row,pause,*,safety,**kw):
            safety.reserve(row['post_id'],'like');engine.status_write(db,gid,row['post_id'],'like','verified','offline')
        with self.patches(scheduled),patch.object(engine,'process_one',side_effect=process) as action,patch('time.sleep') as sleep:
            self.context.close.side_effect=scheduled.PlaywrightError('context not confirmed closed')
            with self.assertRaises(scheduled.ScheduledActionStopped) as stopped:
                self.execute(scheduled,1,'like',before_attempt=lambda:None,record_attempt=lambda _:None)
        self.assertEqual(stopped.exception.stage,'cleanup');self.assertEqual(action.call_count,1);sleep.assert_called_once_with(20)

    def test_raw_context_close_error_carries_blocking_cleanup_stage(self):
        import vk_scheduled_action as scheduled
        with self.patches(scheduled),patch.object(engine,'process_one'),patch('time.sleep'):
            self.context.close.side_effect=OSError('unconfirmed context closure')
            with self.assertRaises(OSError) as stopped:
                self.execute(scheduled,1,'like',before_attempt=lambda:None,record_attempt=lambda _:None)
        self.assertEqual(stopped.exception.stage,'cleanup')

    def test_wrapped_source_checks_and_confirms_canonical_original(self):
        import vk_scheduled_action as scheduled
        reference={'group_id':301,'post_id':41,'published_at':self.stamp}
        def submit(page,proof,*,before_publish,identity_guard=None):before_publish()
        with self.patches(scheduled),patch.object(community.ui,'repost_reference',create=True,return_value=reference),\
             patch.object(community.ui,'scan_destination',return_value=[]) as scan,\
             patch.object(community.ui,'prepare',return_value={'community_id':501}) as prepare,\
             patch.object(community.ui,'submit',side_effect=submit),\
             patch.object(community.ui,'confirm_destination',return_value={'post_id':91,'published_at':self.now}) as confirm,patch('time.sleep'):
            result=self.execute(scheduled,1,'community',before_attempt=lambda:None,record_attempt=lambda _:None)
        self.assertEqual(result['status'],'verified')
        self.assertEqual(scan.call_args.args[1:],(301,41,501))
        self.assertEqual(confirm.call_args.args[1:],(301,41,501))
        self.assertEqual(prepare.call_args.args[1:],(101,11,501))

    def test_personal_original_is_preserved_through_single_submit_and_confirmation(self):
        import vk_scheduled_action as scheduled
        reference={'group_id':-301,'post_id':41,'published_at':self.stamp}
        def submit(page,proof,*,before_publish,identity_guard=None):before_publish()
        with self.patches(scheduled),patch.object(community.ui,'repost_reference',return_value=reference),\
             patch.object(community.ui,'scan_destination',return_value=[]) as scan,\
             patch.object(community.ui,'prepare',return_value={'community_id':501}) as prepare,\
             patch.object(community.ui,'submit',side_effect=submit) as send,\
             patch.object(community.ui,'confirm_destination',return_value={'post_id':91,'published_at':self.now}) as confirm,patch('time.sleep') as sleep:
            result=self.execute(scheduled,1,'community',before_attempt=lambda:None,record_attempt=lambda _:None)
        self.assertEqual(result['status'],'verified')
        self.assertEqual(scan.call_args.args[1:],(-301,41,501))
        self.assertEqual(confirm.call_args.args[1:],(-301,41,501))
        self.assertEqual(prepare.call_args.args[1:],(101,11,501))
        send.assert_called_once();sleep.assert_called_once_with(20)

    def test_original_owner_sign_change_before_prepare_stops_before_reservation(self):
        import vk_scheduled_action as scheduled
        with self.patches(scheduled),patch.object(community.ui,'repost_reference',side_effect=[
                {'group_id':-301,'post_id':41,'published_at':self.stamp},
                {'group_id':301,'post_id':41,'published_at':self.stamp}]),\
             patch.object(community.ui,'scan_destination',return_value=[]),patch.object(community.ui,'submit') as send,patch('time.sleep') as sleep:
            with self.assertRaises(scheduled.ScheduledActionStopped):
                self.execute(scheduled,1,'community',before_attempt=lambda:self.fail('reserved'),record_attempt=lambda _:self.fail('recorded'))
        send.assert_not_called();sleep.assert_not_called()

    def test_canonical_history_prevents_another_wrapper_send(self):
        import vk_scheduled_action as scheduled
        with closing(engine.db_open(self.first)) as db:
            community.init_state(db,1001);community.remember_reference(db,201,21,301,41)
            community.write_state(db,501,201,21,'verified',target_post_id=91)
        with self.patches(scheduled),patch.object(community.ui,'repost_reference',return_value={
                'group_id':301,'post_id':41,'published_at':self.stamp}),\
             patch.object(community.ui,'scan_destination') as scan,patch.object(community.ui,'submit') as send,patch('time.sleep') as sleep:
            result=self.execute(scheduled,1,'community',before_attempt=lambda:self.fail('reserved'),record_attempt=lambda _:self.fail('recorded'))
        self.assertEqual(result['status'],'already');self.assertEqual(result['target_post_id'],91)
        scan.assert_not_called();send.assert_not_called();sleep.assert_not_called()

    def test_changed_source_chain_before_prepare_never_submits(self):
        import vk_scheduled_action as scheduled
        with self.patches(scheduled),patch.object(community.ui,'repost_reference',side_effect=[
                {'group_id':301,'post_id':41,'published_at':self.stamp},
                {'group_id':301,'post_id':42,'published_at':self.stamp}]),\
             patch.object(community.ui,'scan_destination',return_value=[]),patch.object(community.ui,'submit') as send,patch('time.sleep') as sleep:
            with self.assertRaises(scheduled.ScheduledActionStopped):
                self.execute(scheduled,1,'community',before_attempt=lambda:self.fail('reserved'),record_attempt=lambda _:self.fail('recorded'))
        send.assert_not_called();sleep.assert_not_called()

    def test_unknown_outcome_keeps_uncertain_and_cools_down_without_retry(self):
        import vk_scheduled_action as scheduled
        def submit(page,proof,*,before_publish,identity_guard=None):
            before_publish();raise engine.StopRun('unknown send')
        with self.patches(scheduled),patch.object(community.ui,'prepare',return_value={'community_id':501}),\
             patch.object(community.ui,'submit',side_effect=submit) as send,\
             patch.object(community.ui,'scan_destination',return_value=[]),patch('time.sleep') as sleep:
            with self.assertRaisesRegex(engine.StopRun,'unknown send'):
                self.execute(scheduled,1,'community',before_attempt=lambda:None,record_attempt=lambda _:None)
        self.assertEqual(send.call_count,1);sleep.assert_called_once_with(20)
        with closing(engine.db_open(self.first)) as db:
            self.assertEqual(community.get_state(db,501,101,11)['status'],'uncertain')

    def test_parent_rejects_attempt_before_any_durable_action_or_sleep(self):
        import vk_scheduled_action as scheduled
        def process(page,db,gid,row,pause,*,safety,**kw):safety.reserve(row['post_id'],'like')
        with self.patches(scheduled),patch.object(engine,'process_one',side_effect=process),patch('time.sleep') as sleep:
            with self.assertRaisesRegex(engine.StopRun,'window closed'):
                self.execute(scheduled,1,'like',before_attempt=lambda:(_ for _ in ()).throw(engine.StopRun('window closed')),
                             record_attempt=lambda _:self.fail('recorded'))
        sleep.assert_not_called()
        with closing(engine.db_open(self.first)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)

    def test_identity_mismatch_stops_before_action(self):
        import vk_scheduled_action as scheduled
        with self.patches(scheduled),patch.object(community,'live_identity',side_effect=engine.StopRun('identity mismatch')),\
             patch.object(engine,'process_one') as process,patch('time.sleep') as sleep:
            with self.assertRaisesRegex(engine.StopRun,'identity mismatch'):
                self.execute(scheduled,1,'like',before_attempt=lambda:self.fail('guard'),record_attempt=lambda _:self.fail('record'))
        process.assert_not_called();sleep.assert_not_called()

    def test_source_date_can_arrive_after_post_root_without_renavigation(self):
        import vk_scheduled_action as scheduled
        with self.patches(scheduled),patch.object(groups,'exact_post_date_info',side_effect=[
                {'publishedAt':None,'source':'date-unknown'}, {'publishedAt':self.stamp,'exact':True}]),\
             patch.object(engine,'process_one') as process,patch('time.sleep'):
            def verified(page,db,gid,row,*args,**kwargs):
                engine.status_write(db,gid,row['post_id'],'like','already','offline')
            process.side_effect=verified
            result=self.execute(scheduled,1,'like',before_attempt=lambda:self.fail('reserved'),record_attempt=lambda _:self.fail('recorded'))
            self.assertEqual(result['status'],'already')
            engine.go_to_post.assert_called_once()
            self.page.wait_for_timeout.assert_called_once_with(250)

    def test_source_failure_codes_distinguish_missing_mismatch_and_navigation(self):
        import vk_scheduled_action as scheduled
        cases=[('source_date_unavailable',{'publishedAt':None,'source':'date-unknown'},None),
               ('source_date_mismatch',{'publishedAt':self.stamp-1},None),
               ('source_date_conflict',{'publishedAt':None,'source':'date-conflict'},None),
               ('source_navigation',{'publishedAt':self.stamp},engine.PostTemporaryError('offline navigation'))]
        for stage,date,error in cases:
            with self.subTest(stage=stage),self.patches(scheduled),\
                 patch.object(groups,'exact_post_date_info',return_value=date),\
                 patch.object(scheduled.time,'monotonic',side_effect=[0,10]),\
                 patch.object(engine,'process_one') as process,patch('time.sleep'):
                if error:engine.go_to_post.side_effect=error
                with self.assertRaises(scheduled.ScheduledActionStopped) as failure:
                    self.execute(scheduled,1,'like',before_attempt=lambda:self.fail('reserved'),record_attempt=lambda _:self.fail('recorded'))
                self.assertEqual(failure.exception.reason_code,'action_stopped_'+stage)
                process.assert_not_called()
        with closing(engine.db_open(self.first)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)

    def test_duplicate_reservation_cannot_create_two_actions(self):
        import vk_scheduled_action as scheduled
        def process(page,db,gid,row,pause,*,safety,**kw):
            safety.reserve(row['post_id'],'like');safety.reserve(row['post_id'],'like')
        with self.patches(scheduled),patch.object(engine,'process_one',side_effect=process),patch('time.sleep') as sleep:
            with self.assertRaises(engine.StopRun):
                self.execute(scheduled,1,'like',before_attempt=lambda:None,record_attempt=lambda _:None)
        sleep.assert_called_once_with(20)
        with closing(engine.db_open(self.first)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],1)

    def test_known_community_repost_never_reserves_or_sleeps(self):
        import vk_scheduled_action as scheduled
        with self.patches(scheduled),patch.object(community.ui,'scan_destination',return_value=[{'post_id':90,'published_at':None}]),\
             patch.object(community.ui,'submit') as send,patch('time.sleep') as sleep:
            result=self.execute(scheduled,1,'community',before_attempt=lambda:self.fail('guard'),record_attempt=lambda _:self.fail('record'))
        self.assertEqual(result,{'status':'already','attempt_id':None,'target_post_id':90})
        send.assert_not_called();sleep.assert_not_called()

    def test_stale_or_invalid_catalog_source_never_launches_browser(self):
        import vk_scheduled_action as scheduled
        with closing(engine.db_open(self.first)) as db:db.execute('UPDATE api_seen_posts SET valid=0');db.commit()
        with self.patches(scheduled),patch.object(scheduled,'sync_playwright') as browser,patch('time.sleep') as sleep:
            with self.assertRaises(engine.StopRun):
                self.execute(scheduled,1,'like',before_attempt=lambda:self.fail('guard'),record_attempt=lambda _:self.fail('record'))
        browser.assert_not_called();sleep.assert_not_called()

    def test_parent_record_failure_keeps_durable_uncertain_attempt_and_cooldown(self):
        import vk_scheduled_action as scheduled
        def process(page,db,gid,row,pause,*,safety,**kw):
            safety.reserve(row['post_id'],'like');self.fail('outward click after record failure')
        with self.patches(scheduled),patch.object(engine,'process_one',side_effect=process),patch('time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError,'scheduler write failed'):
                self.execute(scheduled,1,'like',before_attempt=lambda:None,
                    record_attempt=lambda _:(_ for _ in ()).throw(RuntimeError('scheduler write failed')))
        sleep.assert_called_once_with(20)
        with closing(engine.db_open(self.first)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0],'uncertain')

    def test_old_or_missing_community_confirmation_stays_uncertain(self):
        import vk_scheduled_action as scheduled
        def submit(page,proof,*,before_publish,identity_guard=None):before_publish()
        for account in (1,2):
            with self.subTest(account=account),self.patches(scheduled),\
                 patch.object(community.ui,'prepare',return_value={'community_id':501}),\
                 patch.object(community.ui,'submit',side_effect=submit) as send,\
                 patch.object(community.ui,'scan_destination',return_value=[]),\
                 patch.object(community.ui,'confirm_destination',side_effect=community.ui.CommunityConfirmationError()),patch('time.sleep') as sleep:
                with self.assertRaisesRegex(engine.StopRun,'community_confirmation_timeout'):
                    self.execute(scheduled,account,'community',before_attempt=lambda:None,record_attempt=lambda _:None)
            send.assert_called_once();sleep.assert_called_once_with(20)
            with closing(engine.db_open(self.first if account==1 else self.second)) as db:
                self.assertEqual(community.get_state(db,501,101,11)['status'],'uncertain')

    def test_existing_uncertain_is_not_automatically_retried(self):
        import vk_scheduled_action as scheduled
        with closing(engine.db_open(self.first)) as db:
            engine.status_write(db,101,11,'repost','uncertain','prior crash')
        with self.patches(scheduled),patch.object(scheduled,'sync_playwright') as browser,patch('time.sleep') as sleep:
            with self.assertRaisesRegex(engine.StopRun,'повтор запрещён'):
                self.execute(scheduled,1,'personal',before_attempt=lambda:self.fail('guard'),record_attempt=lambda _:self.fail('record'))
        browser.assert_not_called();sleep.assert_not_called()

    def test_like_failure_does_not_touch_personal_repost_state(self):
        import vk_scheduled_action as scheduled
        def process(page,db,gid,row,pause,*,safety,actions,**kw):
            self.assertEqual(actions,{'like'})
            safety.reserve(row['post_id'],'like');raise engine.StopRun('like result unknown')
        with self.patches(scheduled),patch.object(engine,'process_one',side_effect=process),patch('time.sleep') as sleep:
            with self.assertRaisesRegex(engine.StopRun,'like result unknown'):
                self.execute(scheduled,1,'like',before_attempt=lambda:None,record_attempt=lambda _:None)
        sleep.assert_called_once_with(20)
        with closing(engine.db_open(self.first)) as db:
            self.assertEqual(tuple(db.execute('SELECT like_status,repost_status FROM posts').fetchone()),('uncertain','pending'))

    def test_proven_vk_denial_never_reserves_clicks_or_changes_actor_flags(self):
        import vk_scheduled_action as scheduled
        for account,kind in ((1,'like'),(2,'like'),(1,'personal'),(2,'community')):
            with self.subTest(account=account,kind=kind),self.patches(scheduled),\
                 patch.object(scheduled.availability,'confirmed_denial',return_value={'reason':'vk_like_denied' if kind=='like' else 'vk_repost_denied','permission':'likes.can_like' if kind=='like' else 'likes.can_publish','value':False,'observations':2}),\
                 patch.object(community.ui,'scan_destination',return_value=[]),\
                 patch.object(engine,'process_one',side_effect=AssertionError('No action preparation after a VK denial')),\
                 patch.object(community.ui,'prepare',side_effect=AssertionError('No community chooser after a VK denial')),patch('time.sleep') as sleep:
                result=self.execute(scheduled,account,kind,before_attempt=lambda:self.fail('reserve'),record_attempt=lambda _:self.fail('record'))
            self.assertEqual(result['status'],'skipped_vk');self.assertIsNone(result['attempt_id'])
            sleep.assert_not_called()
            with closing(engine.db_open(self.first if account==1 else self.second)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
                self.assertEqual(tuple(db.execute('SELECT like_status,repost_status FROM posts WHERE post_id=11').fetchone()),('pending','pending'))

    def test_unproven_disabled_flag_cannot_be_silently_skipped(self):
        import vk_scheduled_action as scheduled
        with self.patches(scheduled),patch.object(engine,'refresh_state',return_value=dict(liked=False,reposted=False,canLike=False,canRepost=True,source='offline')),\
             patch.object(engine,'wait_post_footer_info',return_value={'liked':False}),patch('time.sleep') as sleep:
            with self.assertRaises(engine.StopRun):
                self.execute(scheduled,1,'like',before_attempt=lambda:self.fail('reserve'),record_attempt=lambda _:self.fail('record'))
        sleep.assert_not_called()
        with closing(engine.db_open(self.first)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0],'pending')

    def test_like_final_guard_runs_after_slow_footer_resolution(self):
        import vk_scheduled_action as scheduled
        late=False;button=MagicMock()
        def slow_footer(*args,**kwargs):
            nonlocal late
            late=True;return button
        def deadline():
            if late:raise engine.StopRun('19:00 cutoff')
            return 500
        pre=dict(liked=False,reposted=False,canLike=True,canRepost=True,source='offline')
        with self.patches(scheduled),patch.object(engine,'refresh_state',return_value=pre),\
             patch.object(engine,'wait_post_footer_info',return_value={'liked':False}),\
             patch.object(engine,'verified_like_button',return_value=MagicMock()),\
             patch.object(engine,'post_footer_button',side_effect=slow_footer),patch('time.sleep') as sleep:
            with self.assertRaisesRegex(engine.StopRun,'19:00 cutoff'):
                self.execute(scheduled,1,'like',before_attempt=lambda:None,record_attempt=lambda _:None,before_click=deadline)
        button.click.assert_not_called();sleep.assert_called_once_with(20)
        with closing(engine.db_open(self.first)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0],'uncertain')

    def test_personal_final_guard_runs_after_durable_reservation(self):
        import vk_scheduled_action as scheduled
        late=False;opener=MagicMock();final=MagicMock()
        state=dict(ok=True,state={'wall':True,'direct_message':False})
        def reserved(_):
            nonlocal late
            late=True
        def deadline():
            if late:raise engine.StopRun('19:00 cutoff')
            return 500
        def evaluate(script,*args):
            if script==engine.PUBLISH_CONTROLS_JS:return {'ok':True,'candidates':[{'disabled':False}]}
            if script==engine.SHARE_DIALOG_JS:return state
            return True
        with self.patches(scheduled),patch.object(engine,'refresh_state',return_value=dict(liked=False,reposted=False,canLike=True,canRepost=True,source='offline')),\
             patch.object(engine,'post_footer_button',return_value=opener),\
             patch.object(engine,'wait_share_destination',return_value=state),patch('time.sleep') as sleep:
            self.page.evaluate.side_effect=evaluate;self.page.locator.return_value=final
            with self.assertRaisesRegex(engine.StopRun,'19:00 cutoff'):
                self.execute(scheduled,1,'personal',before_attempt=lambda:None,record_attempt=reserved,before_click=deadline)
        opener.click.assert_called_once();final.click.assert_not_called();sleep.assert_called_once_with(20)


class FinalClickGuardTests(unittest.TestCase):
    def test_legacy_magicmock_budget_does_not_manufacture_deadline_callback(self):
        for action in ('like','repost'):
            with self.subTest(action=action),tempfile.TemporaryDirectory() as folder,\
                 closing(engine.db_open(Path(folder)/'state.sqlite3')) as db:
                engine.add_posts(db,10,[20]);row=db.execute('SELECT * FROM posts').fetchone()
                budget=MagicMock()
                def old_like(page,gid,pid,*,prepared_button=None):pass
                def old_repost(page,gid,pid,*,before_publish=None):before_publish()
                pre=dict(liked=False,reposted=False,canLike=True,canRepost=True,source='offline')
                after=dict(liked=True,reposted=True,main_post_liked=True)
                with patch.object(engine,'go_to_post'),patch.object(engine,'ensure_ok'),\
                     patch.object(engine,'refresh_state',return_value=pre),\
                     patch.object(engine,'wait_post_footer_info',return_value={'liked':False}),\
                     patch.object(engine,'verified_like_button'),\
                     patch.object(engine,'click_like',side_effect=old_like),\
                     patch.object(engine,'click_share_then_publish',side_effect=old_repost),\
                     patch.object(engine,'after_action_state',return_value=after),patch('time.sleep'),\
                     redirect_stdout(io.StringIO()):
                    engine.process_one(MagicMock(),db,10,row,0,safety=budget,actions={action})
                budget.reserve.assert_called_once_with(20,action)
                self.assertEqual(db.execute(f'SELECT {action}_status FROM posts').fetchone()[0],'verified')

    def test_like_uses_timeout_from_last_guard(self):
        button=MagicMock();page=MagicMock()
        with patch.object(engine,'post_footer_button',return_value=button),patch.object(engine,'ensure_ok'):
            engine.click_like(page,10,20,before_click=lambda:321)
        button.click.assert_called_once_with(timeout=321)

    def test_invalid_guard_timeout_never_disables_click_deadline(self):
        for timeout in (0,-1,9001,True,None):
            with self.subTest(timeout=timeout):
                button=MagicMock()
                with patch.object(engine,'post_footer_button',return_value=button):
                    with self.assertRaises(engine.StopRun):
                        engine.click_like(MagicMock(),10,20,before_click=lambda:timeout)
                button.click.assert_not_called()

    def test_community_checks_deadline_after_last_destination_check(self):
        import vk_community_ui as ui
        page=MagicMock();final=MagicMock();proof={'source_group_id':10,'source_post_id':20,'community_id':99}
        ui._PREPARED[page]=(dict(proof),MagicMock(),final)
        phase=[]
        def check(*args):
            phase.append('check')
            return final
        def deadline():
            self.assertEqual(phase,['check','check','reserved','check'])
            raise engine.StopRun('19:00 cutoff')
        with patch.object(ui,'_check_destination',side_effect=check):
            with self.assertRaisesRegex(engine.StopRun,'19:00 cutoff'):
                ui.submit(page,proof,before_publish=lambda:phase.append('reserved'),before_final_click=deadline)
        final.click.assert_called_once_with(trial=True,timeout=9000)


if __name__=='__main__':unittest.main()
