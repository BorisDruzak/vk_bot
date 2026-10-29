import io
import json
import tempfile
import unittest
import subprocess
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import vk_accounts as accounts


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name,value in [('ROOT',self.root),('REGISTRY_PATH',self.root/'registry.json'),
                           ('BINDINGS_PATH',self.root/'bindings.json')]:
            p=patch.object(accounts,name,value);p.start();self.addCleanup(p.stop)

    def test_rpc_no_arbitrary_commands_or_unpause(self):
        import vk_control as control
        for operation in ('exec','unpause','retry','reset','scheduler_stop'):
            with self.assertRaises(ValueError): control.dispatch({'operation':operation})

    def test_add_creates_empty_own_history_only(self):
        import vk_control as control
        row=control.dispatch({'operation':'add_account','name':'Третий'})
        self.assertEqual(row['account'],3)
        import sqlite3
        with closing(sqlite3.connect(accounts.database_for_account(3))) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
        self.assertFalse(accounts.database_for_account(1).exists())

    def test_login_enabled_account_fails_without_launch(self):
        import vk_control as control
        with patch.object(control,'launch_login_task') as launch:
            with self.assertRaises(accounts.AccountError):
                control.dispatch({'operation':'login_start','account':1})
            launch.assert_not_called()

    def test_rpc_errors_are_fixed_codes(self):
        import vk_control as control
        src=io.StringIO(json.dumps({'operation':'x','password':'private'}))
        dest=io.StringIO()
        self.assertEqual(control.rpc(src,dest),2)
        self.assertNotIn('private',dest.getvalue())
        self.assertEqual(json.loads(dest.getvalue())['error'],'invalid_request')

    def test_interactive_web_opens_login_as_same_user_without_task_admin(self):
        import vk_control as control
        with patch.object(control,'desktop_session_id',return_value=2,create=True), \
             patch.object(control.subprocess,'Popen') as spawn, \
             patch.object(control.subprocess,'run') as run:
            control.launch_login_task(1,'a'*32)
        run.assert_not_called()
        args=spawn.call_args.args[0]
        self.assertIn('--login-worker',args)
        self.assertIn('1',args)
        self.assertNotIn('powershell.exe',args)

    def test_ssh_session_uses_interactive_task_fallback(self):
        import vk_control as control
        with patch.object(control,'desktop_session_id',return_value=0,create=True), \
             patch.object(control.subprocess,'Popen') as spawn, \
             patch.object(control.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'',b'')) as run:
            control.launch_login_task(1,'b'*32)
        spawn.assert_not_called()
        self.assertEqual(run.call_args.args[0][0],'powershell.exe')


if __name__=='__main__':unittest.main()
