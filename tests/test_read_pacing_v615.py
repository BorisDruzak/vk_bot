"""Offline tests for conservative read-only pacing and persisted scan cache."""
import io
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import vk_batch as engine
import vk_groups as multi


class ReadPacingTests(unittest.TestCase):
    def _db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = engine.db_open(Path(tmp.name) / 'state.sqlite3')
        self.addCleanup(db.close)
        return db

    def test_sleep_between_two_posts_in_one_session(self):
        page = MagicMock()
        page.evaluate.return_value = None
        engine.configure_navigation_pacing(page, 15.0)
        with patch.object(engine.time, 'monotonic', side_effect=[100.0, 100.0, 101.0, 115.0]):
            engine.pace_navigation(page)
            engine.pace_navigation(page)
        page.wait_for_timeout.assert_called_once_with(14001)

    def test_sleep_persisted_across_separate_sessions(self):
        db = self._db()
        first, second = MagicMock(), MagicMock()
        first.evaluate.return_value = second.evaluate.return_value = None
        engine.configure_navigation_pacing(first, 15.0, db)
        engine.configure_navigation_pacing(second, 15.0, db)
        with patch.object(engine.time, 'time', side_effect=[100.0, 100.0, 101.0, 116.0]):
            engine.pace_navigation(first)
            engine.pace_navigation(second)
        second.wait_for_timeout.assert_called_once_with(14001)
        self.assertEqual(db.execute('SELECT last_navigation_at FROM read_navigation_pacing').fetchone()[0], 116.0)

    def test_vk_http429_prevents_next_navigation_even_with_pacing(self):
        page = MagicMock()
        page.evaluate.return_value = None
        engine.watch_rate_limit_responses(page)
        callback = page.on.call_args.args[1]
        engine.configure_navigation_pacing(page, 15.0)
        engine.pace_navigation(page)
        callback(MagicMock(status=429, url='https://vk.com/al_wall.php'))
        with self.assertRaises(engine.PlatformRateLimited):
            engine.pace_navigation(page)
        page.wait_for_timeout.assert_not_called()

    def test_persistent_lock_halts_even_readonly_navigation(self):
        db = self._db()
        page = MagicMock()
        page.evaluate.return_value = None
        engine.configure_navigation_pacing(page, 15.0, db)
        engine.lock_on_platform_warning(db, 0, 'Too many requests per second')
        with self.assertRaises(engine.StopRun):
            engine.pace_navigation(page)
        self.assertIsNone(db.execute('SELECT last_navigation_at FROM read_navigation_pacing').fetchone())

    def test_config_default_without_modifying_original(self):
        cfg = multi.load_config()
        self.assertEqual(cfg['scan_groups_per_run'], 3)
        self.assertEqual(cfg['scan_groups_cap'], 1)
        self.assertEqual(cfg['scan_scroll_cap'], 2)
        self.assertEqual(cfg['read_pause_seconds'], 15.0)
        self.assertEqual(cfg['audit_posts_per_run'], 3)
        self.assertEqual(cfg['scan_min_interval_hours'], 12.0)
        self.assertEqual(len(cfg['groups']), 57)

    def test_scan_ttl_prevents_reopening_just_scanned_group(self):
        db = self._db()
        multi.init_scan_progress(db)
        groups = [{'id': 1}, {'id': 2}]
        multi.mark_group_scanned(db, 1, 5)
        self.assertEqual(multi.choose_scan_groups(db, groups, 2, min_interval_hours=12), [{'id': 2}])
        multi.mark_group_scanned(db, 2, 5)
        self.assertEqual(multi.choose_scan_groups(db, groups, 2, min_interval_hours=12), [])
        old = (datetime.now(timezone.utc)-timedelta(hours=13)).isoformat(timespec='seconds')
        db.execute('UPDATE group_scan_progress SET last_scanned_at=? WHERE group_id=1', (old,))
        db.commit()
        self.assertEqual(multi.choose_scan_groups(db, groups, 2, min_interval_hours=12), [{'id': 1}])

    def test_cli_audit_respects_cap_before_opening_vk(self):
        db = self._db()
        tmp_path = Path(db.execute('PRAGMA database_list').fetchone()[2])
        multi.init_dates(db)
        gid = 188206921
        ids = list(range(13501, 13509))
        engine.add_posts(db, gid, ids)
        for i, pid in enumerate(ids):
            multi.save_date(db, gid, pid, int(datetime.now(timezone.utc).timestamp())-i,
                            'test-timestamp', True)
        mock_pw = MagicMock()
        context = mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
        context.pages = [MagicMock()]
        with patch.object(engine, 'DB_PATH', tmp_path), patch.object(engine, 'PROFILE', tmp_path.parent/'profile'), \
             patch.object(multi, 'sync_playwright', mock_pw), patch.object(multi, 'inspect_one') as inspect, \
             redirect_stdout(io.StringIO()) as output:
            code = multi.main(['--audit', '--only', str(gid), '--max-posts', '20'])
        self.assertEqual(code, 0)
        self.assertIn('[READ CAP]', output.getvalue())
        self.assertIn('[QUEUE] Выбрано 3', output.getvalue())
        self.assertEqual(inspect.call_count, 3)

    def test_cli_scan_cache_avoids_browser_launch(self):
        db = self._db()
        tmp_path = Path(db.execute('PRAGMA database_list').fetchone()[2])
        gid = 188206921
        multi.init_scan_progress(db)
        multi.init_dates(db)
        multi.mark_group_scanned(db, gid, 4)
        mock_pw = MagicMock()
        with patch.object(engine, 'DB_PATH', tmp_path), patch.object(engine, 'PROFILE', tmp_path.parent/'profile'), \
             patch.object(multi, 'sync_playwright', mock_pw), redirect_stdout(io.StringIO()) as output:
            code = multi.main(['--scan', '--only', str(gid)])
        self.assertEqual(code, 0)
        self.assertIn('[SCAN CACHE]', output.getvalue())
        mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context.assert_not_called()


if __name__ == '__main__':
    unittest.main()

class ScanResourceCapsTests(unittest.TestCase):
    def test_old_config_three_groups_is_capped_to_one_per_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.sqlite3'
            with closing(engine.db_open(path)) as db:
                multi.init_dates(db)
            mock_pw = MagicMock()
            context = mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            context.pages = [MagicMock()]
            with patch.object(engine, 'DB_PATH', path), patch.object(engine, 'PROFILE', Path(tmp)/'profile'), \
                 patch.object(multi, 'sync_playwright', mock_pw), \
                 patch.object(multi, 'scan_group', return_value=(0,0)) as scan, \
                 redirect_stdout(io.StringIO()) as output:
                result = multi.main(['--scan', '--only', '188206921,198577444'])
            self.assertEqual(result, 0)
            self.assertEqual(scan.call_count, 1)
            self.assertIn('За запуск максимум 1 групп', output.getvalue())

    def test_old_config_five_scrolls_capped_at_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.sqlite3'
            with closing(engine.db_open(path)) as db:
                multi.init_dates(db)
                page = MagicMock()
                page.evaluate.side_effect = [[], {'ids': []}]*3
                cfg = multi.load_config()
                self.assertEqual(cfg['scan_scrolls_per_group'],5)
                self.assertEqual(cfg['scan_scroll_cap'],2)
                group={'id':188206921,'name':'Администрация','url':'https://vk.com/club188206921'}
                with patch.object(engine,'ensure_ok'), patch.object(engine,'pace_navigation'):
                    discovered, added = multi.scan_group(page, db, group, cfg)
                self.assertEqual((discovered,added), (0,0))
                self.assertEqual(page.mouse.wheel.call_count, 2)
