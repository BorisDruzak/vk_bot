import csv
import tempfile
import unittest
from pathlib import Path

import vk_analytics as calc
import vk_analytics_collect as collect
import vk_analytics_store as store


def ordinary(pid,day='2026-10-20'):
    return dict(owner_id=-1,id=pid,date=calc.stamp(day)+3600,post_type='post',
                likes={'count':1},comments={'count':2},reposts={'count':3},views={'count':100})


def joint(local=50,canonical=7,day='2026-10-20'):
    return {**ordinary(canonical,day), 'owner_id':-2,'inner_type':'wall_wallpost','type':'post',
            'coowners':dict(coowner_post_id=dict(owner_id=-1,post_id=local),
                            list=[dict(owner_id=-2),dict(owner_id=-1)],requests=[])}


class CollaborationCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=store.open_db(Path(self.tmp.name)/'analytics.sqlite3');self.addCleanup(self.db.close)
        self.run=store.new_run(self.db,'2026-10-14','2026-10-22',[dict(id=1,name='one')])

    def test_exact_coowner_mapping_collects_local_id_with_canonical_counters(self):
        collect.collect_run(self.db,self.run,lambda *a:dict(count=1,items=[joint()]))
        group=self.db.execute('SELECT status FROM analytics_run_groups').fetchone()[0]
        self.assertEqual(group,'complete')
        row=self.db.execute('SELECT * FROM analytics_snapshots').fetchone()
        self.assertEqual((row['post_id'],row['likes'],row['views']),(50,1,100))

    def test_old_joint_post_proves_boundary_without_stopping_valid_period_posts(self):
        collect.collect_run(self.db,self.run,lambda *a:dict(count=100,items=[ordinary(1),joint(day='2026-10-01'),ordinary(2,'2026-09-30')]),page_size=3)
        row=self.db.execute('SELECT status,reason FROM analytics_run_groups').fetchone()
        self.assertEqual(tuple(row),('complete','date_boundary'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_snapshots').fetchone()[0],1)

    def test_delayed_old_joint_post_is_not_a_boundary_or_owned_order_violation(self):
        calls=[]
        def fetch(gid,offset,count):
            calls.append(offset)
            return dict(count=4,items=[ordinary(4),joint(day='2026-10-01')] if offset==0
                        else [ordinary(3,'2026-10-19'),ordinary(2,'2026-10-01')])
        collect.collect_run(self.db,self.run,fetch,page_size=2)
        self.assertEqual(calls,[0,2])
        self.assertEqual(self.db.execute('SELECT status FROM analytics_run_groups').fetchone()[0],'complete')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_snapshots').fetchone()[0],2)

    def test_foreign_date_newer_than_preceding_owned_post_stays_partial(self):
        collect.collect_run(self.db,self.run,lambda *a:dict(count=2,items=[ordinary(1,'2026-10-19'),joint(day='2026-10-20')]))
        self.assertEqual(self.db.execute('SELECT status FROM analytics_run_groups').fetchone()[0],'partial')

    def test_local_joint_id_resolves_historical_reference_without_fake_missing(self):
        store.capture_catalog_references(self.db,self.run,[dict(group_id=1,post_id=50,
            published_at=joint()['date'],seen_at='2026-10-22T07:00:00+00:00')])
        collect.collect_run(self.db,self.run,lambda *a:dict(count=1,items=[joint()]))
        self.assertEqual(self.db.execute('SELECT status FROM analytics_run_groups').fetchone()[0],'complete')
        self.assertEqual(self.db.execute('SELECT status FROM analytics_snapshots').fetchone()[0],'complete')

    def test_foreign_owner_without_exact_local_mapping_remains_partial(self):
        item=joint();item['coowners'].pop('coowner_post_id')
        collect.collect_run(self.db,self.run,lambda *a:dict(count=1,items=[item]))
        self.assertEqual(self.db.execute('SELECT status FROM analytics_run_groups').fetchone()[0],'partial')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_snapshots').fetchone()[0],0)

    def test_two_aliases_of_same_canonical_on_one_wall_are_not_double_counted(self):
        collect.collect_run(self.db,self.run,lambda *a:dict(count=2,items=[joint(local=50),joint(local=51)]))
        self.assertEqual(self.db.execute('SELECT status FROM analytics_run_groups').fetchone()[0],'partial')
        self.assertLessEqual(self.db.execute('SELECT COUNT(*) FROM analytics_snapshots').fetchone()[0],1)

    def test_posts_csv_preserves_local_and_canonical_identity(self):
        from vk_analytics_scheduled import export_posts
        collect.collect_run(self.db,self.run,lambda *a:dict(count=1,items=[joint()]))
        export_posts(self.db,self.run,Path(self.tmp.name))
        with (Path(self.tmp.name)/'posts.csv').open(encoding='utf-8-sig',newline='') as stream:
            row=next(csv.DictReader(stream,delimiter=';'))
        self.assertEqual((row['group_id'],row['post_id']),('1','50'))
        self.assertEqual((row['canonical_owner_id'],row['canonical_post_id']),('-2','7'))
        self.assertEqual(row['canonical_url'],'https://vk.com/wall-2_7')

    def test_outside_period_canonical_date_conflict_survives_collection(self):
        rid=store.new_run(self.db,'2026-10-14','2026-10-22',[dict(id=1),dict(id=2)])
        alias=joint(local=70,canonical=7,day='2026-10-01')
        alias['owner_id']=-1
        alias['coowners']['coowner_post_id']['owner_id']=-2
        collect.collect_run(self.db,rid,lambda gid,*a:dict(count=1,items=[ordinary(7) if gid==1 else alias]))
        result=calc.report(self.db,rid,weeks='period')
        self.assertEqual(result['district']['completeness'],'partial')
        self.assertEqual(result['district']['canonical_date_conflicts'],1)
        self.assertIsNone(result['district']['er'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analytics_snapshots WHERE run_id=?',(rid,)).fetchone()[0],1)
        collect.collect_run(self.db,rid,lambda *a:self.fail('completed groups must not be replayed'))
        self.assertIsNone(calc.report(self.db,rid,weeks='period')['district']['er'])


if __name__=='__main__':unittest.main()
