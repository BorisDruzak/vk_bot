"""Action progress renews liveness; missing liveness is not an uncertain send."""
import json
import shutil
import subprocess
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import vk_scheduler as scheduler
from tests import test_action_retry_state as fixtures


class ActionHeartbeatTests(unittest.TestCase):
    setUp=fixtures.RetryStateTests.setUp
    reserve=fixtures.RetryStateTests.reserve

    def test_real_action_callbacks_refresh_liveness_without_recounting_usage(self):
        from tests.batch_fixtures import save_plan
        save_plan(self.state,self.now)
        self.state.set('heartbeat',{'at':(self.now-timedelta(seconds=232)).isoformat(),
            'phase':'refreshing_counters','active_slot':1,'mode':'live'})
        observed=[]
        choice=(1,'like',0,dict(group_id=101,post_id=1,published_at=int(self.now.timestamp())-1))
        def execute(account,kind,row,**options):
            options['diagnostic']('action_stage',stage='source_navigation')
            observed.append(self.state.get('heartbeat'))
            return {'status':'already','attempt_id':None}
        with patch.object(self.worker,'pick',side_effect=[choice,None]), \
             patch.object(scheduler.actions,'execute_action',side_effect=execute):
            self.worker.run_batch(1)
        self.assertEqual(observed[0]['at'],self.now.isoformat())
        self.assertEqual(observed[0]['phase'],'executing_actions')
        self.assertEqual(observed[0]['active_slot'],1)
        self.assertEqual(observed[0]['stage'],'source_navigation')
        self.assertEqual(sum(self.state.usage(self.now).values()),0)

    def test_liveness_never_commits_an_active_intent_transaction(self):
        self.assertTrue(hasattr(self.worker,'pulse_action'))
        self.worker._active_slot=1
        self.state.set('heartbeat',{'at':self.now.isoformat(),'phase':'refreshing_counters'})
        self.state.db.execute("INSERT INTO scheduler_meta VALUES('fixture_uncommitted','true')")
        self.worker.pulse_action('action_stage',job=1,stage='attempt_reservation')
        self.assertTrue(self.state.db.in_transaction)
        self.state.db.rollback()
        self.assertIsNone(self.state.get('fixture_uncommitted'))
        self.assertEqual(self.state.get('heartbeat')['phase'],'refreshing_counters')


@unittest.skipUnless(shutil.which('node'),'Node required')
class TimelineLivenessTests(unittest.TestCase):
    def test_action_signal_stale_signal_and_protective_attention_are_distinct(self):
        script=r"""
const fs=require('fs'),vm=require('vm');
const src=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(src.split('// BEGIN timeline helpers')[1].split('// END timeline helpers')[0]);
const at=Date.parse('2026-10-26T07:00:00Z'),slot=new Date(at).toISOString();
const batch={slot:1,status:'running',finished_at:null};
const hb={at:slot,phase:'executing_actions',active_slot:1};
console.log(JSON.stringify({fresh:timelineState(slot,batch,at+1000,hb),
 stale:timelineState(slot,batch,at+232000,hb),
 guard:timelineState(slot,{...batch,status:'attention'},at+232000,hb),
 done:timelineState(slot,{...batch,status:'batch_complete',finished_at:slot},at+232000,hb)}));
"""
        result=subprocess.run([shutil.which('node'),'-e',script,str(Path(__file__).resolve().parents[1]/'web/app.js')],
                              capture_output=True,text=True,encoding='utf8',check=True)
        data=json.loads(result.stdout)
        self.assertEqual(data['fresh']['css'],'running')
        self.assertEqual(data['stale']['css'],'stale')
        self.assertNotIn('Требует проверки',data['stale']['label'])
        self.assertEqual(data['guard']['css'],'uncertain')
        self.assertEqual(data['done']['css'],'verified')
