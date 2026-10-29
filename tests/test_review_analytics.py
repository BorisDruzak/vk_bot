"""Saved observations cannot acquire coverage as the reporting clock advances."""
import json
import tempfile
import unittest
from datetime import datetime as DateTime, timezone
from pathlib import Path
from unittest.mock import patch

import vk_analytics as calc
import vk_analytics_collect as collect
import vk_analytics_store as store
import vk_analytics_web as web
import vk_analytics_import as official


class AnalyticsReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = store.open_db(self.root/'vk_analytics.sqlite3')
        self.addCleanup(self.db.close)

    def saved(self, *, empty=False, started='2026-10-25T15:00:00+00:00',
              finished='2026-10-25T15:01:00+00:00', observed='2026-10-25T15:00:00+00:00'):
        run = store.new_run(self.db, '2026-10-19', '2026-10-26', [{'id':1}])
        posts = [] if empty else [dict(owner_id=-1, id=i, date=calc.stamp('2026-10-20')+i,
            likes={'count':1}, comments={'count':0}, reposts={'count':0}, views={'count':100})
            for i in (3,2,1)]
        collect.collect_run(self.db, run, lambda *args:dict(count=len(posts), items=posts, _observed_at=observed))
        with self.db:
            self.db.execute('UPDATE analytics_runs SET started_at=?,finished_at=? WHERE id=?',
                            (started, finished, run))
        return run

    def test_report_does_not_promote_old_run_after_midnight_or_freeze(self):
        run = self.saved()
        class LateClock(DateTime):
            @classmethod
            def now(cls, tz=None):return DateTime(2026,10,27,tzinfo=timezone.utc)
        with patch.object(calc, 'datetime', LateClock):
            result = calc.report(self.db, run, weeks='period')
            frozen = store.freeze(self.db, result)
        saved = json.loads(self.db.execute('SELECT payload FROM analytics_reports WHERE id=?',(frozen,)).fetchone()[0])
        for report in (result, saved):
            self.assertEqual(report['district']['completeness'], 'partial')
            for key in ('er','p33','p32'):self.assertIsNone(report['district'][key])

    def test_late_finish_cannot_complete_an_early_collection_in_report_or_web(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                run = self.saved(empty=empty, finished='2026-10-26T03:00:00+00:00')
                self.assertEqual(calc.report(self.db,run,weeks='period')['district']['completeness'],'partial')
                self.assertEqual(web.read_analytics(self.root,run_id=run)['district']['completeness'],'partial')

    def test_cached_old_observations_do_not_prove_period_closed(self):
        run = self.saved(started='2026-10-26T03:00:00+00:00', finished='2026-10-26T03:01:00+00:00')
        self.assertEqual(calc.report(self.db,run,weeks='period')['district']['completeness'],'partial')
        self.assertEqual(web.read_analytics(self.root,run_id=run)['district']['completeness'],'partial')

    def test_closed_period_has_final_metrics_and_unfinished_run_does_not(self):
        run = self.saved(started='2026-10-26T03:00:00+00:00', finished='2026-10-26T03:01:00+00:00',
                         observed='2026-10-26T03:00:00+00:00')
        result = calc.report(self.db,run,weeks='period')['district']
        self.assertEqual((result['completeness'], result['er'], result['p32']), ('complete',1,100))
        with self.db:self.db.execute('UPDATE analytics_runs SET finished_at=NULL WHERE id=?',(run,))
        self.assertEqual(calc.report(self.db,run,weeks='period')['district']['completeness'],'partial')

    def test_negative_p3_roundtrip_csv_and_invalid_counters_still_rejected(self):
        path = self.root/'official.csv'
        path.write_text('p3;likes\n-22;0\n',encoding='utf8')
        rows = official.read_official(path)
        try:
            identity = store.import_official(self.db,rows,source='fixture')
        except ValueError as exc:self.fail(f'Legitimate signed P3 rejected: {exc}')
        saved = json.loads(self.db.execute('SELECT payload FROM analytics_official WHERE import_id=?',(identity,)).fetchone()[0])
        self.assertEqual(saved['p3'],-22)
        for row in ({'likes':-1},{'p33':-1},{'p31_ratio':-1},{'p3':True},{'p3':float('nan')},{'p3':float('inf')}):
            with self.subTest(row=row), self.assertRaises(ValueError):store.import_official(self.db,[row],source='fixture')

    def test_missing_views_preserve_other_fully_known_period_totals(self):
        run=self.saved(started='2026-10-26T03:00:00+00:00',finished='2026-10-26T03:01:00+00:00',
                       observed='2026-10-26T03:00:00+00:00')
        with self.db:
            self.db.execute("UPDATE analytics_snapshots SET views=NULL,status='missing_counters' WHERE run_id=?",(run,))
        result=calc.report(self.db,run,weeks='period')['district']
        self.assertEqual(result['likes'],3)
        self.assertEqual(result['comments'],0)
        self.assertIsNone(result['views'])
        self.assertIsNone(result['er'])

    def test_pre_midnight_cached_empty_or_outside_page_cannot_finalize_later_run(self):
        for outside in (False,True):
            with self.subTest(outside=outside):
                run=store.new_run(self.db,'2026-10-19','2026-10-26',[{'id':1}])
                items=[dict(owner_id=-1,id=50,date=calc.stamp('2026-10-18'))] if outside else []
                collect.collect_run(self.db,run,lambda *args:dict(count=len(items),items=items,
                    _observed_at='2026-10-25T18:59:00+00:00'))
                with self.db:
                    self.db.execute('UPDATE analytics_runs SET started_at=?,finished_at=? WHERE id=?',
                        ('2026-10-25T19:01:00+00:00','2026-10-25T19:01:30+00:00',run))
                self.assertEqual(calc.report(self.db,run,weeks='period')['district']['completeness'],'partial')
                self.assertEqual(web.read_analytics(self.root,run_id=run)['district']['completeness'],'partial')

    def test_legacy_empty_archive_without_saved_page_time_is_unknown(self):
        run=store.new_run(self.db,'2026-10-19','2026-10-26',[{'id':1}])
        with self.db:
            self.db.execute("UPDATE analytics_run_groups SET status='complete' WHERE run_id=?",(run,))
        store.finish(self.db,run)
        self.assertEqual(calc.report(self.db,run,weeks='period')['district']['completeness'],'partial')

    def test_fresh_empty_closed_period_is_complete_with_zero_regularity(self):
        run=self.saved(empty=True,started='2026-10-26T03:00:00+00:00',finished='2026-10-26T03:01:00+00:00',
                       observed='2026-10-26T03:00:00+00:00')
        result=calc.report(self.db,run,weeks='period')['district']
        self.assertEqual((result['completeness'],result['posts'],result['p32']),('complete',0,0))

    def test_legacy_readonly_view_and_additive_migration_preserve_frozen_data(self):
        run=self.saved(started='2026-10-26T03:00:00+00:00',finished='2026-10-26T03:01:00+00:00',
                       observed='2026-10-26T03:00:00+00:00')
        frozen=store.freeze(self.db,calc.report(self.db,run,weeks='period'))
        payload=self.db.execute('SELECT payload FROM analytics_reports WHERE id=?',(frozen,)).fetchone()[0]
        snapshots=[tuple(p) for p in self.db.execute('SELECT * FROM analytics_snapshots')]
        with self.db:self.db.execute('ALTER TABLE analytics_run_groups DROP COLUMN observed_from')
        self.assertEqual(web.read_analytics(self.root,run_id=run)['district']['completeness'],'complete')
        other=store.open_db(self.root/'vk_analytics.sqlite3')
        other.close()
        self.assertEqual(self.db.execute('SELECT payload FROM analytics_reports WHERE id=?',(frozen,)).fetchone()[0],payload)
        self.assertEqual([tuple(p) for p in self.db.execute('SELECT * FROM analytics_snapshots')],snapshots)
