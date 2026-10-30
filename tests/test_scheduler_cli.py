import io
import unittest
from contextlib import redirect_stdout,redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

import vk_unified as app


class SchedulerCliTests(unittest.TestCase):
    def test_scheduler_modes_route_before_legacy_without_account_actions(self):
        seen=[]
        fake=SimpleNamespace(run_cli=lambda args,parser:seen.append(args) or 0)
        with patch.dict('sys.modules',{'vk_scheduler':fake}):
            for mode in ('--scan-all','--engagement-report','--schedule-preview','--scheduler-status',
                         '--scheduler-once','--scheduler-daemon','--scheduler-stop','--scheduler-check',
                         '--scheduler-manual-block','--scheduler-manual-unblock'):
                self.assertEqual(app.main([mode]),0)
        self.assertEqual(len(seen),10)
        self.assertEqual(sum(a.scheduler_daemon for a in seen),1)

    def test_only_one_scheduler_mode_allowed(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):app.main(['--scan-all','--scheduler-daemon'])


if __name__=='__main__':unittest.main()
