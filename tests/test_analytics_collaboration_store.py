import copy
import csv
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import vk_analytics_store as store
import vk_analytics as calc
import vk_analytics_scheduled as scheduled


GID=195911397
OWNER=-225494425


def collaboration():
    return dict(id=570,owner_id=OWNER,date=int(datetime.fromisoformat('2026-10-15T12:00:00+05:00').timestamp()),
                inner_type='wall_wallpost',type='post',post_type='post',
                likes={'count':3},comments={'count':0},reposts={'count':2},views={'count':100},
                coowners=dict(coowner_post_id=dict(owner_id=-GID,post_id=4509),
                              list=[dict(owner_id=OWNER,is_current_user=False,can_subscribe=True,is_subscribed=False),dict(owner_id=-GID)],
                              requests=[],is_owner=False,is_coowner=False))


class CollaborationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'analytics.sqlite3'
        self.db=store.open_db(self.path);self.addCleanup(self.db.close)
        self.groups=[dict(id=GID,name='selected')]
        self.run=store.new_run(self.db,'2026-10-14','2026-10-22',self.groups)

    def ordinary(self,pid=4509):
        item=collaboration();item.update(id=pid,owner_id=-GID)
        item.pop('coowners');item.pop('inner_type');item.pop('type')
        return item

    def rows(self,table):return [tuple(row) for row in self.db.execute('SELECT * FROM '+table+' ORDER BY rowid')]

    def test_explicit_coowner_identity_uses_local_key_and_canonical_url(self):
        item=collaboration()
        try:store.observe(self.db,self.run,GID,item,'wall.get')
        except ValueError:self.fail('Explicit coowner witness was rejected')
        self.assertEqual(store.post_identity(GID,item),(4509,OWNER,570))
        publication=self.db.execute('SELECT * FROM analytics_publications').fetchone()
        snapshot=self.db.execute('SELECT * FROM analytics_snapshots').fetchone()
        identity=self.db.execute('SELECT * FROM analytics_post_identities').fetchone()
        self.assertEqual((publication['group_id'],publication['post_id'],publication['url']),(GID,4509,'https://vk.com/wall-225494425_570'))
        self.assertEqual((snapshot['post_id'],snapshot['likes'],snapshot['views']),(4509,3,100))
        self.assertEqual(tuple(identity),(self.run,GID,4509,OWNER,570,'coowner'))

    def test_ordinary_identity_and_idempotent_snapshots_remain_unchanged(self):
        item=self.ordinary()
        self.assertEqual(store.post_identity(GID,item),(4509,-GID,4509))
        store.observe(self.db,self.run,GID,item,'wall.get',at='2026-10-15T07:00:00+00:00')
        before={table:self.rows(table) for table in ('analytics_publications','analytics_snapshots','analytics_post_identities')}
        item['likes']['count']=999
        store.observe(self.db,self.run,GID,item,'wall.get',at='2026-10-16T07:00:00+00:00')
        self.assertEqual({table:self.rows(table) for table in before},before)
        self.assertEqual(before['analytics_post_identities'][0],(self.run,GID,4509,-GID,4509,'owner'))

    def test_repeated_coowner_observation_retains_missing_counters_and_first_time(self):
        item=collaboration();item.pop('views')
        store.observe(self.db,self.run,GID,item,'wall.get',at='2026-10-15T07:00:00+00:00')
        item['views']={'count':777};store.observe(self.db,self.run,GID,item,'wall.get')
        snapshot=self.db.execute('SELECT * FROM analytics_snapshots').fetchone()
        self.assertEqual((snapshot['post_id'],snapshot['views'],snapshot['status'],snapshot['measured_at']),
                         (4509,None,'missing_counters','2026-10-15T07:00:00+00:00'))
        self.assertEqual(len(self.rows('analytics_post_identities')),1)

    def test_conflicting_explicit_mapping_rejected_before_any_writes_same_or_new_run(self):
        store.observe(self.db,self.run,GID,collaboration(),'wall.get');self.db.commit()
        run2=store.new_run(self.db,'2026-10-14','2026-10-22',self.groups)
        for run in (self.run,run2):
            with self.subTest(run=run):
                before={table:self.rows(table) for table in ('analytics_publications','analytics_snapshots','analytics_post_identities')}
                changed=collaboration();changed['id']=571
                with self.assertRaises(ValueError):store.observe(self.db,run,GID,changed,'wall.get')
                self.assertEqual({table:self.rows(table) for table in before},before)
        with self.assertRaises(ValueError):store.observe(self.db,run2,GID,self.ordinary(),'wall.get')

    def test_legacy_missing_rows_remain_immutable_and_gain_only_new_identity(self):
        item=self.ordinary();store.observe(self.db,self.run,GID,item,'wall.get');self.db.commit()
        self.db.execute('UPDATE analytics_runs SET rules_version=? WHERE id=?',('omsu-111-2026-09-14/v1.1',self.run))
        self.db.execute('''INSERT INTO analytics_reports(id,run_id,report_month,variant,fixed_at,completeness,rules_version,payload)
          VALUES(?,?,?,?,?,?,?,?)''',('legacy-frozen',self.run,'2026-10','period','2026-10-15T07:00:00+00:00','partial',
                                      'omsu-111-2026-09-14/v1.1','{"legacy":true}'))
        self.db.execute('DROP TABLE analytics_post_identities');self.db.commit()
        self.db.execute('DROP TABLE analytics_identity_witnesses');self.db.commit()
        old={table:self.rows(table) for table in ('analytics_publications','analytics_snapshots','analytics_runs','analytics_reports')}
        columns={table:[tuple(row) for row in self.db.execute('PRAGMA table_info('+table+')')] for table in old}
        self.db.close();self.db=store.open_db(self.path);self.addCleanup(self.db.close)
        self.assertEqual(self.rows('analytics_post_identities'),[])
        self.assertEqual(self.rows('analytics_identity_witnesses'),[])
        self.assertEqual({table:[tuple(row) for row in self.db.execute('PRAGMA table_info('+table+')')] for table in old},columns)
        store.observe(self.db,self.run,GID,collaboration(),'wall.get')
        self.assertEqual({table:self.rows(table) for table in old},old)
        self.assertEqual(self.rows('analytics_post_identities'),[(self.run,GID,4509,OWNER,570,'coowner')])

    def test_missing_receipt_does_not_invent_identity(self):
        store.observe(self.db,self.run,GID,self.ordinary(),'wall.get')
        self.db.execute('DELETE FROM analytics_post_identities');self.db.commit()
        run2=store.new_run(self.db,'2026-10-14','2026-10-22',self.groups)
        publication=self.db.execute('SELECT * FROM analytics_publications').fetchone()
        store.record_missing(self.db,run2,publication,'wall.get',status='historical_not_returned')
        self.assertEqual(self.rows('analytics_post_identities'),[])
        self.assertIsNone(self.db.execute('SELECT views FROM analytics_snapshots WHERE run_id=?',(run2,)).fetchone()[0])

    def test_missing_alias_carries_proven_identity_for_report_and_csv_without_counters(self):
        groups=self.groups+[dict(id=-OWNER,name='original wall')]
        prior=store.new_run(self.db,'2026-10-14','2026-10-22',groups)
        item=collaboration();original={k:v for k,v in item.items() if k!='coowners'}
        store.observe(self.db,prior,GID,item,'wall.get')
        store.observe(self.db,prior,-OWNER,original,'wall.get');self.db.commit()
        current=store.new_run(self.db,'2026-10-14','2026-10-22',groups)
        store.observe(self.db,current,-OWNER,original,'wall.get',at='2026-10-16T00:00:00+00:00')
        publication=self.db.execute('SELECT * FROM analytics_publications WHERE group_id=?',(GID,)).fetchone()
        store.record_missing(self.db,current,publication,'wall.get',status='historical_not_returned')
        result=calc.report(self.db,current,weeks='period')
        self.assertEqual((result['district']['posts'],result['district']['shared_publication_duplicates']),(1,1))
        self.assertEqual(result['district']['coverage']['views'],0)
        self.assertIsNone(result['district']['views'])
        identity=self.db.execute('SELECT * FROM analytics_post_identities WHERE run_id=? AND group_id=?',(current,GID)).fetchone()
        self.assertEqual(tuple(identity),(current,GID,4509,OWNER,570,'historical_coowner'))
        snapshot=self.db.execute('SELECT * FROM analytics_snapshots WHERE run_id=? AND group_id=?',(current,GID)).fetchone()
        self.assertEqual(snapshot['status'],'historical_not_returned')
        self.assertTrue(all(snapshot[key] is None for key in store.COUNTERS))
        out=Path(self.tmp.name)/'export';out.mkdir()
        scheduled.export_posts(self.db,current,out)
        with (out/'posts.csv').open(encoding='utf-8-sig') as stream:rows=list(csv.DictReader(stream,delimiter=';'))
        alias=next(row for row in rows if row['group_id']==str(GID))
        self.assertEqual(alias['canonical_url'],'https://vk.com/wall-225494425_570')
        self.assertEqual(alias['views'],'')

    def test_missing_identity_carry_is_idempotent_and_preserves_prior_rows(self):
        store.observe(self.db,self.run,GID,collaboration(),'wall.get');self.db.commit()
        previous={table:self.rows(table) for table in ('analytics_publications','analytics_snapshots','analytics_post_identities')}
        current=store.new_run(self.db,'2026-10-14','2026-10-22',self.groups)
        publication=self.db.execute('SELECT * FROM analytics_publications').fetchone()
        store.record_missing(self.db,current,publication,'wall.get')
        once={table:self.rows(table) for table in previous}
        store.record_missing(self.db,current,publication,'wall.get')
        self.assertEqual({table:self.rows(table) for table in previous},once)
        self.assertEqual(once['analytics_publications'],previous['analytics_publications'])
        self.assertEqual(once['analytics_post_identities'][-1],(current,GID,4509,OWNER,570,'historical_coowner'))
        self.assertEqual(once['analytics_snapshots'][:1],previous['analytics_snapshots'])

    def test_missing_alias_rejects_conflicting_prior_or_current_mapping_before_writes(self):
        store.observe(self.db,self.run,GID,collaboration(),'wall.get');self.db.commit()
        prior2=store.new_run(self.db,'2026-10-14','2026-10-22',self.groups)
        current=store.new_run(self.db,'2026-10-14','2026-10-22',self.groups)
        publication=self.db.execute('SELECT * FROM analytics_publications').fetchone()
        for conflict_run in (prior2,current):
            with self.subTest(conflict_run=conflict_run):
                self.db.execute('INSERT INTO analytics_post_identities VALUES(?,?,?,?,?,?)',(conflict_run,GID,4509,OWNER,571,'coowner'))
                before={table:self.rows(table) for table in ('analytics_snapshots','analytics_post_identities')}
                with self.assertRaises(ValueError):store.record_missing(self.db,current,publication,'wall.get')
                self.assertEqual({table:self.rows(table) for table in before},before)
                self.db.execute('DELETE FROM analytics_post_identities WHERE run_id=?',(conflict_run,))

    def test_missing_alias_does_not_borrow_future_identity(self):
        current=store.new_run(self.db,'2026-10-14','2026-10-22',self.groups)
        future=store.new_run(self.db,'2026-10-14','2026-10-22',self.groups)
        store.observe(self.db,future,GID,collaboration(),'wall.get')
        publication=self.db.execute('SELECT * FROM analytics_publications').fetchone()
        store.record_missing(self.db,current,publication,'wall.get')
        self.assertIsNone(self.db.execute('SELECT * FROM analytics_post_identities WHERE run_id=?',(current,)).fetchone())
        self.assertEqual(self.db.execute('SELECT status FROM analytics_snapshots WHERE run_id=?',(current,)).fetchone()[0],'not_returned')

    def test_identity_witnesses_preserve_each_date_idempotently_without_counters(self):
        item=collaboration()
        store.record_identity_witness(self.db,self.run,GID,item,'2026-10-16T00:00:00+00:00')
        store.record_identity_witness(self.db,self.run,GID,item,'2026-10-17T00:00:00+00:00')
        outside=copy.deepcopy(item);outside['date']=int(datetime.fromisoformat('2026-10-01T12:00:00+05:00').timestamp())
        store.record_identity_witness(self.db,self.run,GID,outside,'2026-10-17T00:00:00+00:00')
        self.assertEqual(self.rows('analytics_identity_witnesses'),[
            (self.run,GID,4509,OWNER,570,item['date'],'2026-10-16T00:00:00+00:00'),
            (self.run,GID,4509,OWNER,570,outside['date'],'2026-10-17T00:00:00+00:00')])
        self.assertEqual(self.rows('analytics_publications'),[])
        self.assertEqual(self.rows('analytics_snapshots'),[])
        self.assertEqual(self.rows('analytics_post_identities'),[])

    def test_identity_witness_validation_and_run_foreign_key(self):
        for field,value in (('date',True),('date',2**63),('date',0),('post_type',{}),('post_type','x'*41),('is_pinned',2)):
            with self.subTest(field=field,value=type(value).__name__):
                item=collaboration();item[field]=value
                with self.assertRaises(ValueError):store.record_identity_witness(self.db,self.run,GID,item,'2026-10-16T00:00:00+00:00')
        with self.assertRaises(ValueError):store.record_identity_witness(self.db,self.run,GID,collaboration(),'2026-10-16T00:00:00')
        self.assertEqual(self.rows('analytics_identity_witnesses'),[])
        with self.assertRaises(sqlite3.IntegrityError):
            store.record_identity_witness(self.db,'0'*32,GID,collaboration(),'2026-10-16T00:00:00+00:00')

    def test_unknown_or_conflicting_collaboration_schemas_are_rejected_without_writes(self):
        changes=[
            lambda p:p.pop('coowners'),
            lambda p:p['coowners'].update(coowner_post_id={'owner_id':-GID,'post_id':True}),
            lambda p:p['coowners'].update(coowner_post_id={'owner_id':-GID-1,'post_id':4509}),
            lambda p:p['coowners']['coowner_post_id'].update(extra='unknown'),
            lambda p:p['coowners'].update(list=[{'owner_id':OWNER},{'owner_id':OWNER}]),
            lambda p:p['coowners'].update(list=[{'owner_id':OWNER}]),
            lambda p:p['coowners'].update(list=[{'owner_id':OWNER},{'owner_id':-GID}]+[{'owner_id':n} for n in (1,2,3,4)]),
            lambda p:p['coowners']['list'][0].update(owner_id=True),
            lambda p:p['coowners']['list'][0].update(owner_id=0),
            lambda p:p['coowners']['list'][0].update(owner_id=2**63),
            lambda p:p['coowners']['list'][0].update(can_subscribe=1),
            lambda p:p['coowners']['list'][0].update(unknown=False),
            lambda p:p['coowners'].update(requests=[{'owner_id':-GID}]),
            lambda p:p['coowners'].update(requests=()),
            lambda p:p['coowners'].update(is_owner=0),
            lambda p:p['coowners'].update(unknown=False),
            lambda p:p.update(inner_type='wall_wallpost_copy'),
            lambda p:p.update(type='copy'),
            lambda p:p.update(post_type='copy'),
            lambda p:p.update(owner_id=True),
            lambda p:p.update(owner_id=0),
            lambda p:p.update(id=True),
            lambda p:p.update(id=0),
            lambda p:p.update(id=2**63),
        ]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                item=copy.deepcopy(collaboration());change(item)
                with self.assertRaises(ValueError):store.observe(self.db,self.run,GID,item,'wall.get')
                self.assertEqual(self.rows('analytics_publications'),[])
                self.assertEqual(self.rows('analytics_snapshots'),[])

    def test_identity_table_requires_existing_run(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('INSERT INTO analytics_post_identities VALUES(?,?,?,?,?,?)',('0'*32,GID,4509,OWNER,570,'coowner'))


if __name__=='__main__':unittest.main()
