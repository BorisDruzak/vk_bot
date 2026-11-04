import importlib.util
import io
import json
import socket
import sqlite3
import tempfile
import time
import unittest
from contextlib import ExitStack, closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import vk_analytics_collect as collect
import vk_api_read as api
import vk_batch as engine


SCRIPT=Path(__file__).resolve().parents[1]/'scripts/vm/probe-analytics-counts.py'
spec=importlib.util.spec_from_file_location('analytics_probe_under_test',SCRIPT)
probe=importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class AnalyticsProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        engine.db_open(self.root/'vk_batch.sqlite3').close()
        (self.root/'.vk-analytics-runtime.json').write_text(json.dumps(dict(machine=socket.gethostname(),root=str(self.root))),encoding='utf8')
        (self.root/'.vk-public-data.json').write_text(json.dumps(dict(version=1,enabled=True)),encoding='utf8')
        (self.root/'sosnovsky_registry.json').write_text(json.dumps([dict(id=i) for i in (1,2,3)]),encoding='utf8')
        self.out=self.root/'audit.json'
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.object(probe,'ROOT',self.root))
        stack.enter_context(patch.object(collect,'__file__',str(self.root/'vk_analytics_collect.py')))
        stack.enter_context(patch.object(api,'configured_token',return_value='dummy-test-token'))
        instant=[time.time()];client_cls=api.VKReadClient
        def client(*args,**kwargs):
            return client_cls(*args,**kwargs,clock=lambda:instant[0],
                              sleeper=lambda seconds:instant.__setitem__(0,instant[0]+seconds))
        stack.enter_context(patch.object(api,'VKReadClient',side_effect=client))

    def run_probe(self,groups='1',pages=1,filters='owner,all,others'):
        argv=['probe','--groups',groups,'--pages',str(pages),'--count','1','--filters',filters,'--out',str(self.out)]
        output=io.StringIO()
        with patch.object(probe.sys,'argv',argv),redirect_stdout(output):probe.main()
        return json.loads(self.out.read_text(encoding='utf8')),output.getvalue()

    @staticmethod
    def post(pid,gid=1,**extra):
        return dict(id=pid,owner_id=-gid,date=1791400000,likes={'count':1},comments={'count':2},
                    reposts={'count':3},views={'count':4},**extra)

    def test_probe_variants_bypass_and_preserve_existing_owner_cache(self):
        reader=collect.LiveReader(self.root,max_requests=1)
        with patch.object(api,'_default_transport',return_value=(200,dict(response=dict(count=1,items=[self.post(1)])))):
            reader.wall_get(1,0,1)
        variants=[]
        def network(params):
            variants.append(params['filter'])
            return 200,dict(response=dict(count=1,items=[self.post({'owner':11,'all':22,'others':33}[params['filter']])]))
        with patch.object(api,'_default_transport',side_effect=network):
            result,_=self.run_probe()
            cached=reader.wall_get(1,0,1)
        self.assertEqual([r['items'][0]['id'] for r in result['requests']],[11,22,33])
        self.assertEqual(variants,['owner','all','others'])
        self.assertEqual(result['used'],3)
        self.assertEqual(cached['items'][0]['id'],1);self.assertTrue(cached['_cache_hit'])
        with closing(sqlite3.connect(self.root/'vk_batch.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0],4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)

    def test_request_budget_is_shared_across_all_groups_and_variants(self):
        calls=[]
        def network(params):
            calls.append((params['owner_id'],params['filter'],params['offset']))
            return 200,dict(response=dict(count=100,items=[self.post(params['offset']+1,-params['owner_id'])]))
        with patch.object(api,'_default_transport',side_effect=network):result,_=self.run_probe('1,2,3',pages=10)
        self.assertEqual(len(calls),60)
        self.assertEqual((result['used'],len(result['requests']),result['complete']),(60,60,False))
        self.assertTrue(result['budget_exhausted'])
        self.assertEqual({owner for owner,_,_ in calls},{-1,-2})
        with closing(sqlite3.connect(self.root/'vk_batch.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0],60)

    def test_coowners_metadata_is_bounded_and_excludes_payload_text(self):
        item=self.post(7,coowners=[21,-12]*13,type=0,inner_type=3,text='PRIVATE_BODY',hash='PRIVATE_HASH',attachments=['PRIVATE_MEDIA'])
        safe=probe.safe(item,1)
        self.assertEqual(len(safe['coowners']),20)
        self.assertTrue(safe['coowners_truncated'])
        self.assertEqual(safe['coowners'],[21,-12]*10)
        self.assertEqual((safe['type'],safe['inner_type']),(0,3))
        self.assertNotIn('PRIVATE',json.dumps(safe))

    def test_unknown_coowner_values_and_error_messages_are_not_exported(self):
        item=self.post(7,coowners=[dict(id='PRIVATE_ID',type='PRIVATE_TYPE',inner_type={'text':'PRIVATE_INNER'}),True,None],type='PRIVATE_TYPE',inner_type='PRIVATE_INNER')
        safe=probe.safe(item,1)
        self.assertNotIn('coowners',safe)
        self.assertEqual((safe['coowners_shape'],safe['coowners_count']),('list',3))
        self.assertEqual((safe['type'],safe['inner_type']),(None,None))
        self.assertNotIn('PRIVATE',json.dumps(safe))

    def test_error_export_excludes_message_and_non_numeric_code(self):
        exc=RuntimeError('PRIVATE_MESSAGE');exc.code='PRIVATE_CODE'
        with patch.object(api,'_default_transport',side_effect=exc):result,output=self.run_probe()
        self.assertNotIn('PRIVATE',json.dumps(result)+output)
        self.assertEqual(result['requests'][0]['error_type'],'RuntimeError')
        self.assertIsNone(result['requests'][0]['error_code'])
        self.assertEqual((result['used'],len(result['requests']),result['complete']),(1,1,False))

    def test_invalid_total_is_sanitized_and_stops_probe_without_retry(self):
        for total in ('PRIVATE_URL_HASH_SENTINEL',True,-1,2**100,None,{'text':'PRIVATE_URL_HASH_SENTINEL'}):
            with self.subTest(total=type(total).__name__),patch.object(api,'_default_transport',
                    return_value=(200,dict(response=dict(count=total,items=[])))) as network:
                result,output=self.run_probe()
                self.assertNotIn('PRIVATE_URL_HASH_SENTINEL',json.dumps(result)+output)
                self.assertEqual((result['used'],len(result['requests']),result['complete']),(1,1,False))
                self.assertIsNone(result['requests'][0]['total'])
                self.assertEqual(result['requests'][0]['error_type'],'invalid_envelope')
                self.assertEqual(network.call_count,1)

    def test_date_summary_uses_only_sanitized_bounded_timestamps(self):
        for date in (2**100,-1,True,'PRIVATE_DATE_SENTINEL'):
            with self.subTest(date=type(date).__name__):
                item=self.post(7);item['date']=date
                with patch.object(api,'_default_transport',return_value=(200,dict(response=dict(count=1,items=[item])))):
                    result,output=self.run_probe(filters='owner')
                self.assertIsNone(result['requests'][0]['items'][0]['date'])
                summary=json.loads(output.splitlines()[-1])
                self.assertEqual((summary['oldest'],summary['period_posts']),(None,0))
                self.assertNotIn('PRIVATE_DATE_SENTINEL',json.dumps(result)+output)


if __name__=='__main__':unittest.main()
