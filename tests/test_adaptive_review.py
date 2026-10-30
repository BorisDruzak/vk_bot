"""Independent review regressions; only temporary ledgers and fake public reads."""
import tempfile
import unittest
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import vk_api_read as api
import vk_batch as engine
import vk_batch_engagement as engagement
import vk_groups as groups
import vk_schedule_policy as policy
from vk_adaptive_state import AdaptiveState
from vk_adaptive_worker import AdaptiveWorker


class AdaptiveDiscoveryReviewTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.source=self.root/'actor.sqlite3'
        self.now=datetime(2026,10,30,5,tzinfo=timezone.utc)
        with closing(engine.db_open(self.source)) as db:
            api.init_api(db);groups.init_dates(db)
        cfg=policy.validate_config({'schedule_mode':'adaptive','start_hour':0,'end_hour':24})
        self.state=AdaptiveState(self.root/'scheduler.sqlite3',{1:self.source},cfg)
        self.state.enabled_provider=lambda actor:actor==1
        self.addCleanup(self.state.close)
        self.groups=[{'id':gid,'enabled':True,'like_enabled':True,'repost_enabled':True} for gid in (101,102)]
        self.groups.append({'id':103,'enabled':False,'like_enabled':True,'repost_enabled':True})
        self.calls=[]
        self.worker=AdaptiveWorker(self.state,self.source,{'groups':self.groups},clock=lambda:self.now)
        self.addCleanup(patch.stopall)
        patch.object(engine,'moment',side_effect=lambda:self.now.isoformat()).start()
    def payload(self,gid):
        return {'count':1,'items':[{'owner_id':-gid,'id':11,'date':int(self.now.timestamp())-1,
            'likes':{'count':0},'comments':{'count':0},'reposts':{'count':0}}]}
    def refresh(self):
        def transport(params,**kw):
            self.calls.append(params.copy())
            return 200,{'response':self.payload(-params['owner_id'])}
        with patch.object(api,'configured_token',return_value='offline-test-token'),\
             patch.object(engagement.app,'exclusive_run',return_value=nullcontext()),\
             patch.object(api,'bounded_transport',side_effect=transport):
            return self.worker.refresh_batch()
    def test_positive_quota_discovers_posts_when_local_catalog_is_empty(self):
        self.assertGreater(sum(self.state.remaining(self.now).values()),0)
        result=self.refresh()
        self.assertEqual(result['state'],'ready','Positive quota must trigger guarded pre-batch discovery without relying on idle scans')
        self.assertEqual(result['pool'],{(101,11),(102,11)})
        self.assertEqual({call['owner_id'] for call in self.calls},{-101,-102})
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],0)
    def test_prebatch_discovery_includes_group_without_existing_candidates(self):
        with closing(engine.db_open(self.source)) as db:
            api.project_public_page(db,101,self.payload(101),now=int(self.now.timestamp()))
            groups.save_date(db,101,11,int(self.now.timestamp())-1,api.API_SOURCE,True)
            db.commit()
        result=self.refresh()
        self.assertEqual(result['state'],'ready')
        self.assertIn((102,11),result['pool'],'An exhausted group must still be read for newly published posts')

    def test_discovery_cannot_read_when_shared_rolling_budget_is_exhausted(self):
        with closing(engine.db_open(self.source)) as db:
            db.executemany('INSERT INTO api_read_attempts(group_id,attempted_at) VALUES(101,?)',[(self.now.isoformat(),)]*5000)
            db.commit()
        result=self.refresh()
        self.assertEqual(result['state'],'budget')
        self.assertEqual(result['pool'],set())
        self.assertEqual(self.calls,[])

    def test_stop_requested_prevents_discovery_and_job_creation(self):
        self.state.set('stop_requested',True)
        result=self.refresh()
        self.assertEqual(result['state'],'stopped')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],0)


if __name__=='__main__':unittest.main()
