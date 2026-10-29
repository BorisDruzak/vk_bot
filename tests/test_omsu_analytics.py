import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from datetime import datetime
from pathlib import Path

import vk_analytics as calc
import vk_analytics_store as store
import vk_analytics_collect as collect
import vk_analytics_import as imp


def post(pid, day='2026-10-08', **extra):
    return dict(owner_id=-1, id=pid, date=int(datetime.fromisoformat(day+'T12:00:00+05:00').timestamp()),
                likes={'count':180}, comments={'count':50}, reposts={'count':40},
                views={'count':10000}, post_type='post', **extra)


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=store.open_db(Path(self.tmp.name)/'a.sqlite3')
        self.groups=[dict(id=1,name='one',included=True,business_card=False)]
        self.run=store.new_run(self.db,'2026-10-08','2026-10-15',self.groups)

    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def test_example_cap_zero_missing(self):
        self.assertEqual(calc.engagement(180,50,40,10000),(2.7,75.0))
        self.assertEqual(calc.engagement(360,0,0,10000),(3.6,100.0))
        self.assertEqual(calc.engagement(500,0,0,10000),(5.0,100.0))
        self.assertEqual(calc.engagement(0,0,0,0),(None,None))
        self.assertEqual(calc.engagement(0,0,0,None),(None,None))
        with self.assertRaises(ValueError):calc.engagement(-1,0,0,1)

    def test_snapshot_append_idempotent_outer_counters(self):
        p=post(1,copy_history=[{'likes':{'count':999999}}])
        store.observe(self.db,self.run,1,p,'wall.get')
        store.observe(self.db,self.run,1,{**p,'likes':{'count':999}},'wall.get')
        row=self.db.execute('SELECT * FROM analytics_snapshots').fetchone()
        self.assertEqual(row['likes'],180)
        run2=store.new_run(self.db,'2026-10-08','2026-10-15',self.groups)
        store.observe(self.db,run2,1,p,'wall.get')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_snapshots').fetchone()[0],2)

    def test_missing_views_and_zero_are_distinct(self):
        p=post(1);p.pop('views')
        store.observe(self.db,self.run,1,p,'wall.get')
        self.db.execute("UPDATE analytics_run_groups SET status='complete'");self.db.commit()
        r=calc.report(self.db,self.run,weeks='period')
        self.assertIsNone(r['district']['er']);self.assertIsNone(r['district']['views'])
        self.assertEqual(r['groups'][0]['known_sums']['likes'],180)

    def test_weeks_failure_and_repost_switch(self):
        dates=[post(1)['date'],post(2)['date'],post(3)['date']]
        a=calc.regularity(dates,'2026-10-08','2026-10-22','period')
        self.assertFalse(a['passed']);self.assertEqual(a['failed_weeks'][0]['start'],'2026-10-15')
        self.assertEqual(len(calc.week_windows('2026-10-01','2026-11-01','calendar_complete')),3)
        for i in range(3):store.observe(self.db,self.run,1,post(i+1,copy_history=[{'id':9}]),'wall.get')
        self.db.execute("UPDATE analytics_run_groups SET status='complete'");self.db.commit()
        store.finish(self.db,self.run)
        self.assertTrue(calc.report(self.db,self.run,weeks='period')['groups'][0]['regularity']['passed'])
        self.assertFalse(calc.report(self.db,self.run,weeks='period',include_reposts=False)['groups'][0]['regularity']['passed'])

    def test_pagination_pinned_and_retry(self):
        old=post(100,'2026-09-01',is_pinned=1)
        pages={0:dict(count=4,items=[old,post(3)]),2:dict(count=4,items=[post(2),post(1,'2026-10-01')])}
        calls=[]
        def fetch(gid,offset,count):calls.append(offset);return pages[offset]
        collect.collect_run(self.db,self.run,fetch,page_size=2)
        self.assertEqual(calls,[0,2])
        self.assertEqual(calc.report(self.db,self.run,weeks='period')['district']['posts'],2)
        collect.collect_run(self.db,self.run,fetch,page_size=2)
        self.assertEqual(calls,[0,2])

    def test_resume_after_error_no_duplicate(self):
        def broken(gid,offset,count):
            if offset:raise RuntimeError('secret must not be stored')
            return dict(count=4,items=[post(4),post(3)])
        collect.collect_run(self.db,self.run,broken,page_size=2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_snapshots').fetchone()[0],2)
        def ok(gid,offset,count):return dict(count=4,items=[post(4),post(3)] if not offset else [post(2),post(1)])
        collect.collect_run(self.db,self.run,ok,page_size=2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_snapshots').fetchone()[0],4)
        self.assertNotIn('secret',str([tuple(x) for x in self.db.execute('SELECT * FROM analytics_errors')]))

    def test_total_change_invalid_and_budget_remain_partial(self):
        calls=[]
        def fetch(gid,offset,count):
            calls.append(offset);return dict(count=4 if not offset else 5,items=[post(4),post(3)] if not offset else [post(2),post(1)])
        collect.collect_run(self.db,self.run,fetch,page_size=2)
        self.assertNotEqual(self.db.execute('SELECT status FROM analytics_run_groups').fetchone()[0],'complete')

    def test_partial_csv_and_manual_validation(self):
        p=Path(self.tmp.name)/'o.csv';p.write_text('p33;period_start;period_end\n77,03;2026-10-08;2026-10-15\n',encoding='utf8')
        rows=imp.read_official(p)
        self.assertEqual(rows[0]['p33'],77.03)
        store.import_official(self.db,rows,source='test')
        c=calc.compare(calc.report(self.db,self.run),rows)
        self.assertTrue(c['limitations'])
        with self.assertRaises(ValueError):store.import_official(self.db,[{'er':float('nan')}],source='manual')

    def test_freeze_and_registry_immutability(self):
        store.observe(self.db,self.run,1,post(1),'wall.get')
        self.db.execute("UPDATE analytics_run_groups SET status='complete'");self.db.commit()
        r=calc.report(self.db,self.run,weeks='period')
        rid=store.freeze(self.db,r)
        self.groups[0]['included']=False
        self.assertEqual(json.loads(self.db.execute('SELECT payload FROM analytics_reports WHERE id=?',(rid,)).fetchone()[0])['district']['posts'],1)

    def test_resume_detects_changed_total(self):
        def broken(gid,offset,count):
            if offset:raise RuntimeError('interruption')
            return dict(count=4,items=[post(4),post(3)])
        collect.collect_run(self.db,self.run,broken,page_size=2)
        collect.collect_run(self.db,self.run,lambda *a:dict(count=2,items=[post(4),post(3)]),page_size=2)
        self.assertNotEqual(self.db.execute('SELECT status FROM analytics_run_groups').fetchone()[0],'complete')

    def test_csv_scoped_by_exact_municipality(self):
        p=Path(self.tmp.name)/'mixed.csv'
        p.write_text('Муниципальное образование;ID сообщества;П3.3\nСосновский МО;1;75\nУйский МО;2;60\n',encoding='utf8')
        self.assertEqual(len(imp.read_official(p)),1)

    def test_refuse_actor_database(self):
        p=Path(self.tmp.name)/'actor.sqlite3'
        db=sqlite3.connect(p);db.execute('CREATE TABLE actions(id INTEGER)');db.commit();db.close()
        with self.assertRaises(ValueError):store.open_db(p)

    def test_live_reader_refuses_unbound_local_runtime(self):
        with self.assertRaises(ValueError):collect.LiveReader(Path(self.tmp.name))

    def test_district_weighting_exclusion_business_card(self):
        groups=self.groups+[dict(id=2,name='two',business_card=True),dict(id=3,name='excluded',included=False)]
        rid=store.new_run(self.db,'2026-10-08','2026-10-15',groups)
        for pid in range(1,4):store.observe(self.db,rid,1,post(pid),'wall.get')
        p=post(1);p.update(owner_id=-2,likes={'count':0},comments={'count':0},reposts={'count':0},views={'count':1000})
        store.observe(self.db,rid,2,p,'wall.get')
        self.db.execute("UPDATE analytics_run_groups SET status='complete' WHERE run_id=?",(rid,));self.db.commit()
        store.finish(self.db,rid)
        d=calc.report(self.db,rid,weeks='period')['district']
        self.assertAlmostEqual(d['er'],810/31000*100)
        self.assertAlmostEqual(d['diagnostic_mean_community_er'],1.35)
        self.assertEqual((d['p32'],d['p32_eligible']),(100,1))

    def test_period_mismatch_prevents_numeric_comparison(self):
        r=calc.report(self.db,self.run)
        c=calc.compare(r,[dict(er=2,period_start='2026-09-01',period_end='2026-10-01')])
        self.assertIsNone(c['rows'][0]['ours']);self.assertFalse(c['rows'][0]['period_matches'])

    def test_not_returned_known_post_stays_unknown(self):
        store.observe(self.db,self.run,1,post(1),'wall.get');self.db.commit()
        rid=store.new_run(self.db,'2026-10-08','2026-10-15',self.groups)
        collect.collect_run(self.db,rid,lambda *a:dict(count=0,items=[]))
        r=calc.report(self.db,rid,weeks='period')
        self.assertIsNone(r['district']['er']);self.assertEqual(r['district']['posts'],1)

    def test_fully_missing_count_page_not_complete(self):
        collect.collect_run(self.db,self.run,lambda *a:dict(items=[post(1)]))
        self.assertEqual(self.db.execute('SELECT status FROM analytics_runs WHERE id=?',(self.run,)).fetchone()[0],'partial')

    def test_by_id_refresh_missing_ids_is_unknown(self):
        store.observe(self.db,self.run,1,post(1),'wall.get');self.db.commit()
        rid=store.new_run(self.db,'2026-10-08','2026-10-15',self.groups)
        class Reader:
            def wall_get_by_id(self,*args,**kwargs):return []
        collect.refresh_known(self.db,rid,self.run,Reader(),batch_size=2)
        self.assertEqual(self.db.execute('SELECT status FROM analytics_snapshots WHERE run_id=?',(rid,)).fetchone()[0],'not_returned')
        self.assertIsNone(calc.report(self.db,rid)['district']['er'])

    def test_by_id_unsupported_keeps_snapshots(self):
        store.observe(self.db,self.run,1,post(1),'wall.get');self.db.commit()
        rid=store.new_run(self.db,'2026-10-08','2026-10-15',self.groups)
        class Unsupported(Exception):code=1051
        class Reader:
            def wall_get_by_id(self,*args,**kwargs):raise Unsupported()
        self.assertEqual(collect.refresh_known(self.db,rid,self.run,Reader()),'unsupported')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_snapshots WHERE run_id=?',(rid,)).fetchone()[0],0)

    def test_p31_thresholds(self):
        for ratio,penalty in ((1,0),(.99,0),(.96,-2),(.93,-4),(.69,-20),(.68,-22)):
            self.assertEqual(calc.p31_penalty(ratio),penalty)

    def test_by_id_tombstone_keeps_history_and_marks_unavailable(self):
        store.observe(self.db,self.run,1,post(1),'wall.get');self.db.commit()
        rid=store.new_run(self.db,'2026-10-08','2026-10-15',self.groups)
        class Reader:
            def wall_get_by_id(self,*args,**kwargs):return [{'owner_id':-1,'id':1,'is_deleted':1}]
        collect.refresh_known(self.db,rid,self.run,Reader(),batch_size=2)
        row=self.db.execute('SELECT * FROM analytics_snapshots WHERE run_id=?',(rid,)).fetchone()
        self.assertEqual(row['status'],'deleted');self.assertIsNone(row['views'])
        self.assertEqual(self.db.execute('SELECT status FROM analytics_runs WHERE id=?',(rid,)).fetchone()[0],'partial')

    def test_official_layout_csv_institution_identity(self):
        p=Path(self.tmp.name)/'official.csv'
        p.write_text('Муниципальное образование;Наименование учреждения;Ссылка;ER (Вовлеченность)\nСосновский МО;Test;https://vk.com/club1;2,7\n',encoding='utf8')
        r=imp.read_official(p)[0]
        self.assertEqual(r['group_id'],1);self.assertEqual(r['name'],'Test')

    def test_same_total_changed_ids_on_resume_is_partial(self):
        def broken(gid,offset,count):
            if offset:raise RuntimeError('interruption')
            return dict(count=4,items=[post(4),post(3)])
        collect.collect_run(self.db,self.run,broken,page_size=2)
        def changed(gid,offset,count):return dict(count=4,items=[post(5),post(4)] if not offset else [post(2),post(1)])
        collect.collect_run(self.db,self.run,changed,page_size=2)
        self.assertEqual(self.db.execute('SELECT status FROM analytics_runs WHERE id=?',(self.run,)).fetchone()[0],'partial')

    def _live_fixture(self):
        import socket
        import vk_batch as engine
        root=Path(self.tmp.name)
        ledger=engine.db_open(root/'vk_batch.sqlite3');ledger.close()
        (root/'.vk-analytics-runtime.json').write_text(json.dumps(dict(machine=socket.gethostname(),root=str(root))),encoding='utf8')
        return root

    def test_http429_shared_lock_and_no_action_history_change(self):
        import vk_api_read as api
        import vk_batch as engine
        root=self._live_fixture()
        with patch.object(collect,'__file__',str(root/'vk_analytics_collect.py')),patch.object(api,'configured_token',return_value='dummy-test-token'),patch.object(api,'_default_transport',return_value=(429,{})) as transport:
            reader=collect.LiveReader(root)
            with self.assertRaises(api.VkApiLimited):reader.wall_get(1,0,100)
            with self.assertRaises(engine.StopRun):reader.wall_get(1,0,100)
            self.assertEqual(transport.call_count,1)
        with closing(sqlite3.connect(root/'vk_batch.sqlite3')) as ledger:
            self.assertEqual(ledger.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
            self.assertEqual(ledger.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0],1)

    def test_worker_busy_defers_before_api_or_budget(self):
        import vk_api_read as api
        import vk_accounts as accounts
        root=self._live_fixture()
        with patch.object(collect,'__file__',str(root/'vk_analytics_collect.py')),patch.object(api,'configured_token',return_value='dummy-test-token'),patch.object(api,'_default_transport') as transport:
            reader=collect.LiveReader(root)
            with accounts.exclusive_activity(root):
                with self.assertRaises(collect.Deferred):reader.wall_get(1,0,100)
            transport.assert_not_called()
        with closing(sqlite3.connect(root/'vk_batch.sqlite3')) as ledger:
            self.assertNotIn('api_read_attempts',[r[0] for r in ledger.execute("SELECT name FROM sqlite_master WHERE type='table'")])

    def test_live_deadline_rechecked_after_api_pacing_before_transport(self):
        import vk_api_read as api
        from datetime import timedelta
        root=self._live_fixture()
        instant=[datetime.fromisoformat('2026-10-23T08:39:58+05:00')]
        deadline=datetime.fromisoformat('2026-10-23T08:40:00+05:00')
        with closing(sqlite3.connect(root/'vk_batch.sqlite3')) as ledger:
            ledger.row_factory=sqlite3.Row
            api.init_api(ledger)
            ledger.execute('INSERT INTO read_navigation_pacing VALUES(0,?)',(instant[0].timestamp(),));ledger.commit()
        cls=api.VKReadClient
        def client(*args,**kwargs):
            return cls(*args,**kwargs,clock=lambda:instant[0].timestamp(),
                sleeper=lambda seconds:instant.__setitem__(0,instant[0]+timedelta(seconds=seconds)))
        def guard():
            if instant[0]>=deadline:raise collect.Deferred('deadline')
        with patch.object(collect,'__file__',str(root/'vk_analytics_collect.py')),patch.object(api,'configured_token',return_value='dummy-test-token'),\
             patch.object(api,'VKReadClient',side_effect=client),patch.object(api,'_default_transport') as transport:
            reader=collect.LiveReader(root,before_request=guard)
            with self.assertRaises(collect.Deferred):reader.wall_get(1,0,20)
            transport.assert_not_called()
        self.assertFalse((root/'.vk-unified-running.lock').exists())

    def test_explicit_run_identity_is_idempotent_and_rejects_conflicting_selection(self):
        rid='a'*32
        self.assertEqual(store.new_run(self.db,'2026-10-08','2026-10-15',self.groups,run_id=rid),rid)
        self.assertEqual(store.new_run(self.db,'2026-10-08','2026-10-15',self.groups,run_id=rid),rid)
        with self.assertRaises(ValueError):store.new_run(self.db,'2026-10-09','2026-10-15',self.groups,run_id=rid)

    def test_byid_payload_serializes_batch_and_preserves_null(self):
        import vk_api_read as api
        root=self._live_fixture()
        response={'items':[post(1),post(2)]}
        calls=[]
        def fake(data,params):calls.append(params);return 200,{'response':response}
        with patch.object(collect,'__file__',str(root/'vk_analytics_collect.py')),patch.object(api,'configured_token',return_value='dummy-test-token'),patch.object(collect.LiveReader,'_byid_transport',side_effect=fake):
            items=collect.LiveReader(root).wall_get_by_id([(1,1),(1,2)],batch_size=2)
            self.assertEqual(len(items),2);self.assertEqual(calls,[{'posts':'-1_1,-1_2'}])
        with closing(sqlite3.connect(root/'vk_batch.sqlite3')) as ledger:
            self.assertEqual(ledger.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0],1)

    def test_future_reporting_period_is_partial(self):
        rid=store.new_run(self.db,'2026-10-08','2099-10-15',self.groups)
        store.observe(self.db,rid,1,post(1),'wall.get')
        self.db.execute("UPDATE analytics_run_groups SET status='complete' WHERE run_id=?",(rid,));self.db.commit()
        r=calc.report(self.db,rid)
        self.assertEqual(r['district']['completeness'],'partial');self.assertIsNone(r['district']['er'])

    def test_prior_catalog_missing_post_is_not_silently_complete(self):
        refs=[dict(group_id=1,post_id=8,published_at=post(8)['date'],seen_at='2026-10-22T07:39:41+00:00')]
        store.capture_catalog_references(self.db,self.run,refs)
        collect.collect_run(self.db,self.run,lambda *a:dict(count=1,items=[post(1)]))
        rows=self.db.execute('SELECT * FROM analytics_snapshots WHERE run_id=? ORDER BY post_id',(self.run,)).fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]['status'],'historical_not_returned')
        self.assertIsNone(rows[1]['views']);self.assertIsNone(rows[1]['is_pinned'])
        self.assertEqual(self.db.execute('SELECT status FROM analytics_runs WHERE id=?',(self.run,)).fetchone()[0],'partial')
        r=calc.report(self.db,self.run,weeks='period')
        self.assertIsNone(r['district']['er']);self.assertIsNone(r['district']['p32'])
        self.assertEqual(r['district']['unavailable_known_posts'],1)

    def test_catalog_reference_is_frozen_scoped_and_does_not_invent_counters(self):
        refs=[dict(group_id=1,post_id=1,published_at=post(1)['date'],seen_at='2026-10-22T07:39:41+00:00'),
              dict(group_id=2,post_id=2,published_at=post(2)['date'],seen_at='2026-10-22T07:39:41+00:00'),
              dict(group_id=1,post_id=3,published_at=post(3,'2026-09-01')['date'],seen_at='2026-10-22T07:39:41+00:00')]
        store.capture_catalog_references(self.db,self.run,refs)
        store.capture_catalog_references(self.db,self.run,[{**refs[0],'seen_at':'2026-10-23T07:00:00+00:00'}])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_catalog_references').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT seen_at FROM analytics_catalog_references').fetchone()[0],'2026-10-22T07:39:41+00:00')
        collect.collect_run(self.db,self.run,lambda *a:dict(count=1,items=[post(1)]))
        self.assertEqual(calc.report(self.db,self.run,weeks='period')['district']['posts'],1)
        self.assertEqual(calc.report(self.db,self.run,weeks='period')['district']['completeness'],'complete')

    def test_live_catalog_references_readonly_no_actor_values(self):
        import vk_api_read as api
        root=self._live_fixture()
        with closing(sqlite3.connect(root/'vk_batch.sqlite3')) as ledger:
            ledger.row_factory=sqlite3.Row;api.init_api(ledger)
            ledger.execute('INSERT INTO api_seen_posts VALUES(?,?,?,?,?)',(1,1,post(1)['date'],'2026-10-22T07:39:41+00:00',1))
            ledger.execute('INSERT INTO api_seen_posts VALUES(?,?,?,?,?)',(1,2,post(2)['date'],'2026-10-22T07:39:41+00:00',0));ledger.commit()
        with patch.object(collect,'__file__',str(root/'vk_analytics_collect.py')),patch.object(api,'configured_token',return_value='dummy-test-token'):
            refs=collect.LiveReader(root).catalog_references('2026-10-08','2026-10-15',[1])
        self.assertEqual([r['post_id'] for r in refs],[1])
        self.assertEqual(set(refs[0]),{'group_id','post_id','published_at','seen_at'})

    def test_catalog_date_conflict_is_partial(self):
        store.capture_catalog_references(self.db,self.run,[dict(group_id=1,post_id=1,published_at=post(1)['date'],seen_at='2026-10-22T07:39:41+00:00')])
        collect.collect_run(self.db,self.run,lambda *a:dict(count=1,items=[post(1,'2026-10-09')]))
        self.assertEqual(self.db.execute('SELECT status FROM analytics_runs WHERE id=?',(self.run,)).fetchone()[0],'partial')

    def test_missing_previous_analytics_post_also_invalidates_regularity(self):
        store.observe(self.db,self.run,1,post(1),'wall.get');self.db.commit()
        rid=store.new_run(self.db,'2026-10-08','2026-10-15',self.groups)
        collect.collect_run(self.db,rid,lambda *a:dict(count=0,items=[]))
        self.assertIsNone(calc.report(self.db,rid,weeks='period')['district']['p32'])

    def test_refresh_historical_only_unknown_metadata_stays_unknown(self):
        store.capture_catalog_references(self.db,self.run,[dict(group_id=1,post_id=8,
            published_at=post(8)['date'],seen_at='2026-10-22T07:39:41+00:00')])
        collect.collect_run(self.db,self.run,lambda *a:dict(count=0,items=[]))
        for response in ([],[{'owner_id':-1,'id':8,'is_deleted':1}],
                         [{'owner_id':-1,'id':8,'date':post(8)['date'],'is_pinned':None}]):
            rid=store.new_run(self.db,'2026-10-08','2026-10-15',self.groups)
            class Reader:
                def wall_get_by_id(self,*args,**kwargs):return response
            collect.refresh_known(self.db,rid,self.run,Reader())
            row=self.db.execute('SELECT * FROM analytics_snapshots WHERE run_id=?',(rid,)).fetchone()
            self.assertIsNone(row['is_pinned']);self.assertIsNone(row['views'])
            self.assertIn(row['status'],('not_returned','deleted','invalid_metadata'))

    def test_catalog_date_conflict_with_missing_outside_publication_is_partial(self):
        store.observe(self.db,self.run,1,post(8,'2026-09-01'),'wall.get');self.db.commit()
        rid=store.new_run(self.db,'2026-10-08','2026-10-15',self.groups)
        store.capture_catalog_references(self.db,rid,[dict(group_id=1,post_id=8,
            published_at=post(8)['date'],seen_at='2026-10-22T07:39:41+00:00')])
        collect.collect_run(self.db,rid,lambda *a:dict(count=0,items=[]))
        row=self.db.execute('SELECT status,reason FROM analytics_run_groups WHERE run_id=?',(rid,)).fetchone()
        self.assertEqual(tuple(row),('partial','catalog_date_conflict'))
        self.assertIsNone(calc.report(self.db,rid,weeks='period')['district']['er'])


if __name__=='__main__':unittest.main()
