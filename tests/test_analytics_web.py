import hashlib,json,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path

import vk_analytics as calc
import vk_analytics_store as store
from vk_analytics_web import read_analytics


class AnalyticsWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        with closing(store.open_db(self.root/'vk_analytics.sqlite3')) as d:
            self.groups=[dict(id=1,name='Education'),dict(id=2,name='Partial'),dict(id=3,name='Card',business_card=True)]
            self.run=store.new_run(d,'2026-10-14','2026-10-25',self.groups)
            for gid,days in ((1,('2026-10-19','2026-10-23')),(2,('2026-10-19',)*3)):
                for i,day in enumerate(days,1):
                    p=dict(owner_id=-gid,id=i,date=calc.stamp(day)+3600,likes={'count':2},comments={'count':0},reposts={'count':0},views={'count':100})
                    if gid==1 and i==1:p.pop('views')
                    if gid==2 and i==3:p['copy_history']=[dict(id=999)]
                    store.observe(d,self.run,gid,p,'fixture')
            d.execute("UPDATE analytics_run_groups SET status='complete' WHERE run_id=?",(self.run,))
            d.execute("UPDATE analytics_run_groups SET status='partial',reason='historical_posts_not_returned' WHERE run_id=? AND group_id=2",(self.run,))
            store.finish(d,self.run)
            d.execute('UPDATE analytics_runs SET started_at=?,finished_at=? WHERE id=?',('2026-10-24T03:00:00+00:00','2026-10-24T03:05:00+00:00',self.run));d.commit()

    def tearDown(self):self.tmp.cleanup()

    def read(self,**kwargs):return read_analytics(self.root,**kwargs)

    def test_calendar_week_education_two_exact_ids_open_not_failed(self):
        r=self.read();g=r['groups'][0]
        w=next(w for w in g['weeks'] if w['start']=='2026-10-19')
        self.assertEqual(w['posts'],2);self.assertEqual(w['status'],'open')
        self.assertEqual([p['post_id'] for p in g['publications']],[1,2])
        self.assertIsNone(r['district']['p33']);self.assertIsNotNone(g['observed_er'])
        self.assertEqual(g['coverage']['views'],1);self.assertIsNone(g['views'])

    def test_period_week_is_not_calendar_and_reposts_are_configurable(self):
        r=self.read(week_mode='period');g=r['groups'][0]
        self.assertEqual(next(w for w in g['weeks'] if w['start']=='2026-10-21')['posts'],1)
        self.assertEqual(g['weeks'][-1]['end_exclusive'],'2026-10-28')
        r=self.read(include_reposts=False);g=r['groups'][1]
        self.assertEqual(next(w for w in g['weeks'] if w['start']=='2026-10-19')['posts'],2)

    def test_historical_midweek_boundary_is_unknown_not_failure(self):
        with closing(sqlite3.connect(self.root/'vk_analytics.sqlite3')) as d:
            d.execute('UPDATE analytics_runs SET started_at=?,finished_at=? WHERE id=?',
                      ('2026-10-29T03:00:00+00:00','2026-10-29T03:05:00+00:00',self.run))
            d.execute('UPDATE analytics_snapshots SET measured_at=? WHERE run_id=?',
                      ('2026-10-29T03:00:00+00:00',self.run));d.commit()
        w=self.read(week_mode='period')['groups'][0]['weeks'][-1]
        self.assertEqual(w['status'],'unknown');self.assertEqual(w['end_exclusive'],'2026-10-28')

    def test_partial_archive_positive_minimum_is_proven_card_excluded(self):
        r=self.read();week=next(w for w in r['weekly_summary'] if w['start']=='2026-10-19')
        self.assertEqual(week['met'],1);self.assertEqual(week['eligible'],2);self.assertIsNone(week['p32'])
        self.assertEqual(r['groups'][1]['weeks'][1]['status'],'met')
        self.assertTrue(all(w['status']=='excluded' for w in r['groups'][2]['weeks']))

    def test_old_measurement_never_marks_its_open_week_failed_today(self):
        with closing(sqlite3.connect(self.root/'vk_analytics.sqlite3')) as d:
            d.execute('UPDATE analytics_runs SET finished_at=? WHERE id=?',('2026-10-21T03:00:00+00:00',self.run));d.commit()
        g=self.read()['groups'][0]
        self.assertEqual(next(w for w in g['weeks'] if w['start']=='2026-10-19')['status'],'open')

    def test_missing_database_is_not_created(self):
        missing=self.root/'absent'
        with self.assertRaises(OSError):read_analytics(missing)
        self.assertFalse(missing.exists())

    def test_running_run_is_not_selected_and_frozen_rows_are_unchanged(self):
        with closing(store.open_db(self.root/'vk_analytics.sqlite3')) as d:
            store.new_run(d,'2026-10-14','2026-10-25',self.groups)
            before=list(d.execute('SELECT * FROM analytics_snapshots'))
        self.assertEqual(self.read()['run']['id'],self.run)
        with closing(sqlite3.connect(self.root/'vk_analytics.sqlite3')) as d:
            self.assertEqual([tuple(x) for x in before],list(d.execute('SELECT * FROM analytics_snapshots')))

    def test_invalid_selection_rejected(self):
        for args in (dict(run_id='../db'),dict(week_mode='other'),dict(include_reposts='false')):
            with self.assertRaises(ValueError):self.read(**args)


if __name__=='__main__':unittest.main()
