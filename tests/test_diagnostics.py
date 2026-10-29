"""Diagnostic events must explain failures without leaking inputs or changing guards."""
import io,json,unittest
from contextlib import redirect_stdout,closing
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import tempfile
import vk_batch as engine


class DiagnosticsTests(unittest.TestCase):
    def test_exception_chain_and_context_never_log_messages_urls_or_private_actor_ids(self):
        import vk_diagnostics as diag
        try:
            try:raise ValueError('COOKIE_SECRET')
            except ValueError as cause:raise engine.StopRun('TOKEN_SECRET https://private/?key=SECRET') from cause
        except engine.StopRun as error:
            record=diag.make_record('action_failed',now=datetime.now(timezone.utc),error=error,
                job=7,account=2,kind='like',url='https://private/?token=SECRET',vk_id=410000001,
                reason='PRIVATE_REASON',stage='PRIVATE_STAGE',observation={'liked':False,'secret':'DOM_SECRET'})
        text=json.dumps(record)
        for private in ('SECRET','410000001','PRIVATE_REASON','PRIVATE_STAGE','https://'):
            self.assertNotIn(private,text)
        self.assertEqual(record['job'],7)
        self.assertEqual(record['error_chain'][0]['type'],'StopRun')
        self.assertEqual(record['error_chain'][1]['type'],'ValueError')
        self.assertEqual(record['observation'],{'liked':False})

    def test_footer_failure_has_specific_code_and_bounded_numeric_evidence(self):
        page=Mock();info={'ok':False,'reasonCode':'footer_like_not_unique','reason':'DOM_SECRET',
            'postRoot':True,'likeElements':0,'visibleLikes':0,'targetCount':0,'liked':None}
        with patch.object(engine,'ensure_ok'),patch.object(engine,'post_footer_info',return_value=info):
            with self.assertRaises(engine.ActionGuardError) as stopped:
                engine.wait_post_footer_info(page,101,11,'like')
        self.assertEqual(stopped.exception.reason_code,'guard_footer_like_not_unique')
        self.assertEqual(stopped.exception.observation['like_elements'],0)
        self.assertNotIn('DOM_SECRET',str(stopped.exception))
        page.click.assert_not_called()

    def test_vk_api_error_code_is_logged_without_error_message(self):
        import vk_diagnostics as diag
        import vk_api_read as api
        record=diag.make_record('scheduler_failed',error=api.VkApiFailure('TOKEN_SECRET',code=1051))
        self.assertEqual(record['error_chain'][0]['vk_error_code'],1051)
        self.assertNotIn('SECRET',json.dumps(record))

    def test_diagnostic_storage_failure_does_not_pause_or_change_action_state(self):
        import vk_scheduler_state as ledger
        with tempfile.TemporaryDirectory() as temp:
            paths={1:Path(temp)/'actor.sqlite3'}
            with closing(engine.db_open(paths[1])):pass
            with closing(ledger.State(Path(temp)/'scheduler.sqlite3',paths)) as state:
                state.db.execute('PRAGMA query_only=ON')
                with redirect_stdout(io.StringIO()):state.log_event('action_stage',datetime.now(timezone.utc),job=1,stage='source')
                self.assertIsNone(state.get('paused'))
                self.assertEqual(state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],0)

    def test_network_observer_keeps_only_allowlisted_endpoint_status_and_failure_code(self):
        import vk_diagnostics as diag
        class Page:
            def __init__(self):self.handlers={}
            def on(self,event,callback):self.handlers[event]=callback
        page=Page();events=[]
        diag.watch_network(page,lambda event,**data:events.append(diag.make_record(event,**data)))
        page.handlers['response'](SimpleNamespace(url='https://vk.com/al_like.php?access_token=TOKEN_SECRET',status=429))
        page.handlers['requestfailed'](SimpleNamespace(url='https://vk.com/al_wall.php?cookie=COOKIE_SECRET',failure='net::ERR_CONNECTION_RESET'))
        page.handlers['response'](SimpleNamespace(url='https://outside.example/al_like.php?token=SECRET',status=500))
        self.assertEqual(len(events),2)
        self.assertEqual(events[0]['http_status'],429)
        self.assertEqual(events[1]['network_error'],'net::ERR_CONNECTION_RESET')
        self.assertNotIn('SECRET',json.dumps(events))

    def test_observer_failure_cannot_raise_into_browser_event_loop(self):
        import vk_diagnostics as diag
        page=Mock();handlers={};page.on.side_effect=lambda name,fn:handlers.update({name:fn})
        diag.watch_network(page,lambda *a,**k:(_ for _ in ()).throw(RuntimeError('SECRET')))
        handlers['response'](SimpleNamespace(url='https://vk.com/al_like.php',status=200))

    def test_observer_registration_is_best_effort_for_each_event(self):
        import vk_diagnostics as diag
        page=Mock();page.on.side_effect=RuntimeError('SECRET')
        diag.watch_network(page,Mock())
        self.assertEqual(page.on.call_count,2)

    def test_logging_delay_precedes_final_deadline_recheck(self):
        import vk_scheduled_action as scheduled
        clock=[0];events=[]
        def diagnostic(event,**data):
            events.append(event);clock[0]+=10
        def before_click():
            events.append('guard')
            if clock[0]>=1:raise engine.StopRun('deadline')
            return 1000
        budget=scheduled._SingleBudget(None,'like',101,11,None,1,lambda:None,lambda:None,lambda _:None,before_click,diagnostic)
        with self.assertRaisesRegex(engine.StopRun,'deadline'):budget.final_click_timeout()
        self.assertEqual(events,['final_click_check','guard'])

    def test_diagnostics_preserve_uncommitted_caller_transaction(self):
        import vk_scheduler_state as ledger
        with tempfile.TemporaryDirectory() as temp:
            paths={1:Path(temp)/'actor.sqlite3'}
            with closing(engine.db_open(paths[1])):pass
            with closing(ledger.State(Path(temp)/'scheduler.sqlite3',paths)) as state:
                state.db.execute("INSERT INTO scheduler_meta(key,value) VALUES('caller_change','1')")
                with redirect_stdout(io.StringIO()):state.log_event('action_stage',datetime.now(timezone.utc),stage='source')
                self.assertTrue(state.db.in_transaction)
                state.db.rollback()
                self.assertIsNone(state.get('caller_change'))


if __name__=='__main__':unittest.main()
