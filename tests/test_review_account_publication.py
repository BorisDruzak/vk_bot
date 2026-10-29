"""A new disabled actor is published only after its empty ledger is ready."""
import tempfile
import unittest
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

import vk_accounts as accounts
import vk_batch as engine
import vk_control as control
import vk_connections as connections
import vk_scheduler as scheduler
import vk_scheduler_state as ledger


class AccountPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        stack = self.enterContext(ExitStack())
        for key,value in [('ROOT',self.root),('REGISTRY_PATH',self.root/'registry.json'),
                          ('BINDINGS_PATH',self.root/'bindings.json')]:
            stack.enter_context(patch.object(accounts,key,value))

    def test_worker_keeps_running_while_panel_prepares_new_actor_database(self):
        paths={n:accounts.database_for_account(n) for n in accounts.account_numbers()}
        for path in paths.values():
            with closing(engine.db_open(path)):pass
        now=datetime(2026,10,26,5,tzinfo=timezone.utc)
        state=self.enterContext(closing(ledger.State(self.root/'scheduler.sqlite3',paths)))
        state.policy_provider=lambda:accounts.effective_policy(scheduler.policy.validate_config())
        state.paths_provider=lambda:{n:accounts.database_for_account(n) for n in accounts.account_numbers()}
        state.enabled_provider=accounts.account_enabled
        state.day_plan(now)
        state.set('resume_not_before',now.timestamp()+600)
        worker=scheduler.Worker(state,paths[1],{'groups':[{'id':101}]},clock=lambda:now)
        preparing,allow=Event(),Event()
        original=engine.db_open
        def delayed(path):
            if Path(path).name=='vk_batch_account_3.sqlite3':
                preparing.set()
                if not allow.wait(10):raise RuntimeError('fixture timeout')
            return original(path)
        result={}
        def add():
            try:result['row']=control.dispatch({'operation':'add_account'})
            except Exception as exc:result['error']=type(exc).__name__
        with patch.object(engine,'db_open',side_effect=delayed):
            thread=Thread(target=add)
            thread.start()
            try:
                self.assertTrue(preparing.wait(10))
                self.assertNotIn(3,accounts.account_numbers())
                self.assertEqual(worker.tick(execute=True)['phase'],'waiting_batch')
            finally:
                allow.set();thread.join(10)
        self.assertNotIn('error',result)
        self.assertFalse(accounts.account_enabled(3))
        self.assertEqual(worker.tick(execute=True)['phase'],'waiting_batch')
        self.assertFalse(state.get('paused'))
        self.assertEqual(state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],0)

    def test_failed_ledger_initialization_does_not_publish_account(self):
        before=accounts.load_registry()
        with patch.object(engine,'db_open',side_effect=OSError('fixture unavailable')):
            with self.assertRaises(OSError):control.dispatch({'operation':'add_account'})
        self.assertEqual(accounts.load_registry(),before)

    def test_add_any_initializes_new_actor_before_first_registry_publication(self):
        for number in (1,2):accounts.configure_account(number,enabled=False)
        connections.initialize()
        slot=accounts.add_account('Fixture')
        connections.connect(slot['account'],333)
        before=accounts.REGISTRY_PATH.read_bytes()
        original=engine.db_open
        observed=[]
        def check(path):
            if Path(path).name=='vk_batch_account_4.sqlite3':
                observed.append(accounts.account_numbers())
                self.assertEqual(accounts.REGISTRY_PATH.read_bytes(),before)
            return original(path)
        with patch.object(engine,'db_open',side_effect=check):
            target=connections.connect(3,444)
        self.assertEqual(target,4)
        self.assertEqual(observed,[[1,2,3]])
        with closing(engine.db_open(accounts.database_for_account(4))) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
