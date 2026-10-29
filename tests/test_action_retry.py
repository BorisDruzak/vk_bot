"""Durable scheduler retry: temporary actor ledger and mocked browser only."""
import unittest
from contextlib import closing
from unittest.mock import patch

import test_scheduled_action as fixtures
import vk_batch as engine
import vk_community as community
import vk_unified as app
import vk_scheduled_action as scheduled


class ActionRetryTests(unittest.TestCase):
    setUp = fixtures.ScheduledActionTests.setUp
    patches = fixtures.ScheduledActionTests.patches
    execute = fixtures.ScheduledActionTests.execute

    def prior(self, kind='like', *, gid=101, pid=11, destination=501):
        with closing(engine.db_open(self.first)) as db:
            community.init_state(db,1001);app.init_command_actions(db)
            action='like' if kind=='like' else 'repost'
            if kind=='community':
                run=community.begin_run(db,destination,0,1,1)
                db.execute('INSERT INTO community_run_posts VALUES(?,?,?)',(run,gid,pid))
                community.remember_reference(db,gid,pid,-301,41)
                community.write_state(db,destination,gid,pid,'uncertain')
                link='community_run_attempts'
            else:
                run=app.begin_command_action_run(db,int(kind=='like'),int(kind=='personal'),1)
                db.execute('INSERT INTO command_action_posts VALUES(?,?,?)',(run,gid,pid))
                engine.status_write(db,gid,pid,action,'uncertain','prior attempt')
                link='command_action_attempts'
            self.attempt_time=engine.moment()
            aid=db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                           (gid,pid,action,self.attempt_time)).lastrowid
            db.execute(f'INSERT INTO {link} VALUES(?,?)',(run,aid));db.commit()
            return aid

    def recover(self, kind='like', **options):
        return self.execute(scheduled,1,kind,before_attempt=options.pop('before_attempt',lambda:self.fail('unexpected reservation')),
                            record_attempt=options.pop('record_attempt',lambda _:self.fail('unexpected attempt')),**options)

    def state(self, **changes):
        return dict(dict(liked=False,reposted=False,canLike=True,canRepost=True,source='inline'),**changes)

    def test_positive_like_confirms_prior_without_new_attempt_or_click(self):
        aid=self.prior()
        with self.patches(scheduled),patch.object(engine,'refresh_state',return_value=self.state(liked=True)),\
             patch.object(engine,'post_footer_info',return_value={'ok':True,'liked':True}),\
             patch.object(engine,'process_one') as process,patch('time.sleep') as sleep:
            result=self.recover(recovery_attempt_id=aid)
        self.assertEqual(result,{'status':'verified','attempt_id':None})
        process.assert_not_called();sleep.assert_not_called()
        with closing(engine.db_open(self.first)) as db:
            self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0],'verified')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],1)

    def test_exact_negative_like_retries_once_with_virtual_pending_and_parent_guard(self):
        aid=self.prior();events=[]
        def process(page,db,gid,row,pause,*,safety,**kwargs):
            self.assertEqual(row['like_status'],'pending')
            self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0],'uncertain')
            safety.reserve(11,'like');events.append('click')
            engine.status_write(db,101,11,'like','verified','mock exact confirmation')
        with self.patches(scheduled),patch.object(engine,'refresh_state',return_value=self.state()),\
             patch.object(engine,'post_footer_info',return_value={'ok':True,'liked':False}),\
             patch.object(engine,'process_one',side_effect=process),patch('time.sleep',side_effect=lambda n:events.append(n)):
            result=self.recover(recovery_attempt_id=aid,before_attempt=lambda:events.append('guard'),record_attempt=lambda n:events.append(('record',n)))
        self.assertEqual(result,{'status':'verified','attempt_id':aid+1})
        self.assertEqual(events,['guard',('record',aid+1),'click',20])
        with closing(engine.db_open(self.first)) as db:
            self.assertEqual(tuple(db.execute('SELECT id,attempted_at FROM action_attempts WHERE id=?',(aid,)).fetchone()),(aid,self.attempt_time))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM command_action_attempts').fetchone()[0],2)

    def test_unknown_like_or_unproven_permission_does_not_click(self):
        for state,footer in ((self.state(liked=None),False),(self.state(canLike=None),False),
                             (self.state(canLike=False),False),(self.state(),None)):
            with self.subTest(state=state,footer=footer):
                aid=self.prior()
                with self.patches(scheduled),patch.object(engine,'refresh_state',return_value=state),\
                     patch.object(engine,'post_footer_info',return_value={'ok':footer is not None,'liked':footer}),\
                     patch.object(engine,'process_one') as process,patch('time.sleep') as sleep:
                    result=self.recover(recovery_attempt_id=aid)
                self.assertEqual(result,{'status':'uncertain','attempt_id':None})
                process.assert_not_called();sleep.assert_not_called()

    def test_like_conflict_is_hard_stop(self):
        aid=self.prior()
        with self.patches(scheduled),patch.object(engine,'refresh_state',return_value=self.state(liked=True)),\
             patch.object(engine,'post_footer_info',return_value={'ok':True,'liked':False}),\
             patch.object(engine,'process_one') as process:
            with self.assertRaises(engine.StopRun):self.recover(recovery_attempt_id=aid)
        process.assert_not_called()

    def test_disabled_retry_click_still_checks_positive_proof_but_never_sends(self):
        aid=self.prior()
        with self.patches(scheduled),patch.object(engine,'refresh_state',return_value=self.state()),\
             patch.object(engine,'post_footer_info',return_value={'ok':True,'liked':False}),patch.object(engine,'process_one') as process:
            self.assertEqual(self.recover(recovery_attempt_id=aid,allow_retry_click=False)['status'],'uncertain')
        process.assert_not_called()

    def test_wrong_attempt_source_or_action_or_missing_attempt_never_opens_browser(self):
        aid=self.prior('personal')
        for prior in (aid,aid+100,True):
            with self.subTest(prior=prior),self.patches(scheduled),patch.object(scheduled,'sync_playwright') as browser:
                with self.assertRaises((engine.StopRun,ValueError)):self.recover(recovery_attempt_id=prior)
                browser.assert_not_called()

    def test_later_exact_attempt_invalidates_old_recovery_reference(self):
        aid=self.prior();self.prior()
        with self.patches(scheduled),patch.object(scheduled,'sync_playwright') as browser:
            with self.assertRaises(engine.StopRun):self.recover(recovery_attempt_id=aid)
        browser.assert_not_called()

    def test_pending_state_cannot_be_adopted_as_prior_uncertain(self):
        aid=self.prior()
        with closing(engine.db_open(self.first)) as db:
            db.execute("UPDATE posts SET like_status='pending'" );db.commit()
        with self.patches(scheduled),patch.object(scheduled,'sync_playwright') as browser:
            with self.assertRaises(engine.StopRun):self.recover(recovery_attempt_id=aid)
        browser.assert_not_called()

    def test_parent_guard_failure_preserves_prior_and_does_not_cooldown(self):
        aid=self.prior()
        def process(page,db,gid,row,pause,*,safety,**kwargs):safety.reserve(11,'like')
        with self.patches(scheduled),patch.object(engine,'refresh_state',return_value=self.state()),\
             patch.object(engine,'post_footer_info',return_value={'ok':True,'liked':False}),\
             patch.object(engine,'process_one',side_effect=process),patch('time.sleep') as sleep:
            with self.assertRaisesRegex(engine.StopRun,'window closed'):
                self.recover(recovery_attempt_id=aid,before_attempt=lambda:(_ for _ in ()).throw(engine.StopRun('window closed')))
        sleep.assert_not_called()
        with closing(engine.db_open(self.first)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],1)

    def test_personal_recovery_only_confirms_and_never_opens_share(self):
        aid=self.prior('personal')
        for positive in (False,True):
            with self.subTest(positive=positive),self.patches(scheduled),patch.object(engine,'refresh_state',return_value=self.state(reposted=positive)),\
                 patch.object(engine,'process_one') as process,patch.object(engine,'click_share_then_publish') as send,patch('time.sleep') as sleep:
                result=self.recover('personal',recovery_attempt_id=aid)
            self.assertEqual(result,{'status':'verified' if positive else 'uncertain','attempt_id':None})
            process.assert_not_called();send.assert_not_called();sleep.assert_not_called()

    def test_community_recovery_uses_stored_signed_original_and_original_timestamp(self):
        aid=self.prior('community')
        from datetime import datetime
        earliest=int(datetime.fromisoformat(self.attempt_time).timestamp())
        with self.patches(scheduled),patch.object(community.ui,'repost_reference',return_value={'group_id':-301,'post_id':41,'published_at':self.stamp}),\
             patch.object(community.ui,'confirm_destination',return_value={'post_id':91,'published_at':earliest}) as confirm,\
             patch.object(community.ui,'submit') as send,patch.object(community.ui,'prepare') as prepare,patch('time.sleep') as sleep:
            result=self.recover('community',recovery_attempt_id=aid)
        self.assertEqual(result,{'status':'verified','attempt_id':None,'target_post_id':91})
        self.assertEqual(confirm.call_args.args[1:],(-301,41,501));self.assertEqual(confirm.call_args.kwargs['not_before'],earliest)
        send.assert_not_called();prepare.assert_not_called();sleep.assert_not_called()

    def test_community_old_or_missing_proof_never_resends(self):
        aid=self.prior('community')
        for found in ({'post_id':91,'published_at':self.now-1000},community.ui.CommunityConfirmationError()):
            with self.subTest(found=found),self.patches(scheduled),\
                 patch.object(community.ui,'repost_reference',return_value={'group_id':-301,'post_id':41,'published_at':self.stamp}),\
                 patch.object(community.ui,'confirm_destination',side_effect=found if isinstance(found,Exception) else None,return_value=found),\
                 patch.object(community.ui,'submit') as send:
                result=self.recover('community',recovery_attempt_id=aid)
            self.assertEqual(result,{'status':'uncertain','attempt_id':None});send.assert_not_called()

    def test_community_wrong_original_destination_rejected_before_browser(self):
        aid=self.prior('community',destination=502)
        with closing(engine.db_open(self.first)) as db:community.write_state(db,501,101,11,'uncertain')
        with self.patches(scheduled),patch.object(scheduled,'sync_playwright') as browser:
            with self.assertRaises(engine.StopRun):self.recover('community',recovery_attempt_id=aid)
        browser.assert_not_called()

    def test_source_guard_and_cleanup_failures_remain_immediate_stops(self):
        aid=self.prior()
        with self.patches(scheduled),patch.object(community,'live_identity',side_effect=engine.StopRun('wrong actor')):
            with self.assertRaises(engine.StopRun):self.recover(recovery_attempt_id=aid)
        with self.patches(scheduled),patch.object(engine,'refresh_state',return_value=self.state()),\
             patch.object(engine,'post_footer_info',return_value={'ok':True,'liked':False}):
            self.context.close.side_effect=scheduled.PlaywrightError('unsafe cleanup')
            with self.assertRaises(engine.StopRun):self.recover(recovery_attempt_id=aid,allow_retry_click=False)

    def test_retry_uses_real_engine_single_click_and_new_confirmation(self):
        aid=self.prior();events=[]
        with self.patches(scheduled),patch.object(engine,'refresh_state',return_value=self.state()),\
             patch.object(engine,'post_footer_info',return_value={'ok':True,'liked':False}),\
             patch.object(engine,'wait_post_footer_info',return_value={'liked':False}),\
             patch.object(engine,'verified_like_button'),\
             patch.object(engine,'click_like',side_effect=lambda *a,**k:events.append('click')) as click,\
             patch.object(engine,'after_action_state',return_value={'liked':True,'main_post_liked':True}) as confirm,\
             patch('time.sleep',side_effect=lambda seconds:events.append(('sleep',seconds))):
            result=self.recover(recovery_attempt_id=aid,before_attempt=lambda:events.append('parent'),
                                record_attempt=lambda value:events.append(('attempt',value)))
        self.assertEqual(result,{'status':'verified','attempt_id':aid+1})
        click.assert_called_once();confirm.assert_called_once()
        self.assertEqual(events,['parent',('attempt',aid+1),'click',('sleep',0),('sleep',20)])

    def test_retry_record_failure_still_cools_and_cannot_reuse_old_id(self):
        aid=self.prior()
        def process(page,db,gid,row,pause,*,safety,**kw):
            safety.reserve(11,'like');self.fail('click after failed parent record')
        with self.patches(scheduled),patch.object(engine,'refresh_state',return_value=self.state()),\
             patch.object(engine,'post_footer_info',return_value={'ok':True,'liked':False}),\
             patch.object(engine,'process_one',side_effect=process),patch('time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError,'parent write failed'):
                self.recover(recovery_attempt_id=aid,before_attempt=lambda:None,
                             record_attempt=lambda _:(_ for _ in ()).throw(RuntimeError('parent write failed')))
        sleep.assert_called_once_with(20)
        with closing(engine.db_open(self.first)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0],'uncertain')
        with self.patches(scheduled),patch.object(scheduled,'sync_playwright') as browser:
            with self.assertRaises(engine.StopRun):self.recover(recovery_attempt_id=aid)
        browser.assert_not_called()

    def test_community_changed_signed_original_stops_instead_of_confirming_other_target(self):
        aid=self.prior('community')
        with self.patches(scheduled),patch.object(community.ui,'repost_reference',return_value={
                'group_id':301,'post_id':41,'published_at':self.stamp}),\
             patch.object(community.ui,'confirm_destination') as confirm:
            with self.assertRaises(engine.StopRun):self.recover('community',recovery_attempt_id=aid)
        confirm.assert_not_called()

    def test_platform_rate_limit_during_recovery_still_locks_actor(self):
        aid=self.prior()
        with self.patches(scheduled),patch.object(engine,'ensure_ok',side_effect=engine.PlatformRateLimited('429')),\
             patch.object(engine,'process_one') as process:
            with self.assertRaises(engine.PlatformRateLimited):self.recover(recovery_attempt_id=aid)
        process.assert_not_called()
        with closing(engine.db_open(self.first)) as db:
            with self.assertRaises(engine.StopRun):engine.require_platform_unlocked(db)

    def test_real_engine_action_transport_preserves_typed_cause_and_durable_attempt(self):
        for phase in ('click','confirmation'):
            with self.subTest(phase=phase),self.patches(scheduled),\
                 patch.object(engine,'refresh_state',return_value=self.state()),\
                 patch.object(engine,'wait_post_footer_info',return_value={'liked':False}),\
                 patch.object(engine,'verified_like_button'),\
                 patch.object(engine,'click_like',side_effect=scheduled.PlaywrightError('private transport detail') if phase=='click' else None),\
                 patch.object(engine,'after_action_state',side_effect=scheduled.PlaywrightError('private transport detail')),\
                 patch('time.sleep') as sleep:
                # Each fixture is a new logical source; reset only this temporary DB.
                with closing(engine.db_open(self.first)) as db:
                    db.execute("UPDATE posts SET like_status='pending'");db.commit()
                with self.assertRaises(engine.StopRun) as caught:
                    self.execute(scheduled,1,'like',before_attempt=lambda:None,record_attempt=lambda _:None)
                error=caught.exception
                self.assertEqual(type(error).__name__,'ActionTransportError')
                self.assertEqual(error.action,'like');self.assertEqual(error.reason_code,'like_transport_error')
                self.assertEqual(error.stage,'personal_action')
                self.assertIsInstance(error.__cause__,scheduled.PlaywrightError)
                self.assertNotIn('private transport detail',str(error))
                self.assertIn(unittest.mock.call(20),sleep.call_args_list)
                with closing(engine.db_open(self.first)) as db:
                    self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0],'uncertain')

    def test_real_engine_hard_action_errors_are_never_transport_errors(self):
        for error in (engine.PlatformRateLimited('429'),engine.StopRun('identity mismatch'),
                      engine.ActionGuardError('pre_action_like_state_conflict')):
            with self.subTest(error=error),self.patches(scheduled),\
                 patch.object(engine,'refresh_state',return_value=self.state()),\
                 patch.object(engine,'wait_post_footer_info',return_value={'liked':False}),\
                 patch.object(engine,'verified_like_button'),patch.object(engine,'click_like',side_effect=error),patch('time.sleep'):
                with closing(engine.db_open(self.first)) as db:
                    db.execute("UPDATE posts SET like_status='pending'");db.execute('DELETE FROM safety_lock');db.commit()
                with self.assertRaises(engine.StopRun) as caught:
                    self.execute(scheduled,1,'like',before_attempt=lambda:None,record_attempt=lambda _:None)
                self.assertNotEqual(type(caught.exception).__name__,'ActionTransportError')

    def test_identity_probe_playwright_failure_is_guard_stop_even_inside_confirmation(self):
        with self.patches(scheduled),patch.object(engine,'refresh_state',return_value=self.state()),\
             patch.object(engine,'wait_post_footer_info',return_value={'liked':False}),\
             patch.object(engine,'verified_like_button'),patch.object(engine,'click_like'),patch('time.sleep'):
            def confirmation(page,gid,pid,*,guard,**kwargs):
                with patch.object(community,'live_identity',side_effect=scheduled.PlaywrightError('identity read failed')):guard()
            with patch.object(engine,'after_action_state',side_effect=confirmation):
                with self.assertRaises(engine.StopRun) as caught:
                    self.execute(scheduled,1,'like',before_attempt=lambda:None,record_attempt=lambda _:None)
            self.assertEqual(caught.exception.reason_code,'action_stopped_source_guard')


if __name__=='__main__':unittest.main()
