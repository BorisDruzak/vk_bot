import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch

import vk_accounts as accounts
import vk_scheduler as scheduler
import vk_unified as app


class ActivityCoordinationTests(unittest.TestCase):
    def test_os_guard_releases_normally_and_does_not_delete_existing_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with accounts.exclusive_activity(root):
                with self.assertRaises(app.RunAlreadyActive):
                    with accounts.exclusive_activity(root):pass
            self.assertTrue((root/'.vk-activity.guard').exists())
            with accounts.exclusive_activity(root):pass

    def test_busy_reader_defers_batch_before_claim_and_does_not_count_technical_fault(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            worker=scheduler.Worker.__new__(scheduler.Worker)
            worker.source_path=root/'source.sqlite3'
            worker.state=MagicMock()
            worker.status=MagicMock(return_value={'phase':'waiting_profile_lock'})
            with accounts.exclusive_activity(root),patch.object(worker,'_run_batch') as run:
                for _ in range(3):
                    self.assertEqual(worker.run_batch(1)['phase'],'waiting_profile_lock')
                run.assert_not_called()
            worker.state.claim_batch.assert_not_called()
            worker.state.pause.assert_not_called()
            worker.state.set.assert_not_called()

    def test_cli_inspection_uses_same_activity_guard_before_opening_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with patch.object(accounts,'ROOT',root), \
                 patch.dict(accounts.PROFILE_DIRS,{1:root/'profile'}), \
                 patch.object(accounts,'sync_playwright') as browser, \
                 accounts.exclusive_activity(root):
                with self.assertRaises(app.RunAlreadyActive):
                    accounts.browser_inspect(1,False,binding_path=root/'custom.json')
                browser.assert_not_called()


if __name__=='__main__':unittest.main()
