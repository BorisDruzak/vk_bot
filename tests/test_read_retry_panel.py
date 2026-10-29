import json
from pathlib import Path
import shutil
import subprocess
import unittest

import vk_diagnostics as diagnostics


class ReadRetryDiagnosticTests(unittest.TestCase):
    def test_retry_event_preserves_bounded_progress_without_raw_details(self):
        event=diagnostics.make_record('public_read_retry',read_attempt=2,read_attempt_limit=3,
            retry_delay_seconds=5,reason='VkApiReadDeadline',token='synthetic-private',url='synthetic-private')
        self.assertEqual(event['event'],'public_read_retry')
        self.assertEqual((event['read_attempt'],event['read_attempt_limit'],event['retry_delay_seconds']),(2,3,5))
        self.assertNotIn('synthetic-private',json.dumps(event))


@unittest.skipUnless(shutil.which('node'),'Node required for read retry panel')
class ReadRetryPanelTests(unittest.TestCase):
    def test_counter_retry_is_distinct_from_action_retry_and_stale_wait(self):
        script=r"""
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const context={};vm.createContext(context);
vm.runInContext(source.match(/\/\/ BEGIN heartbeat helpers([\s\S]*?)\/\/ END heartbeat helpers/)[1],context);
const now=Date.parse('2026-10-28T09:00:00Z');
const base={at:'2026-10-28T09:00:00Z',phase:'refreshing_counters',counter_refresh:{
 read_attempt:2,read_attempt_limit:3,read_retry_wait_until:'2026-10-28T09:00:05Z'}};
const get=(v,t=now)=>context.workerHeartbeat(v,t).phaseText;
console.log(JSON.stringify({wait:get(base),aged:get(base,now+3000),
 request:get({...base,counter_refresh:{read_attempt:3,read_attempt_limit:3}}),
 first:get({...base,counter_refresh:{read_attempt:1,read_attempt_limit:3}}),
 stale:get(base,now+181000),
 action:get({...base,phase:'executing_actions'}),
 invalid:get({...base,counter_refresh:{read_attempt:4,read_attempt_limit:3}})}));
"""
        result=subprocess.run([shutil.which('node'),'-e',script,str(Path(__file__).resolve().parents[1]/'web/app.js')],
                              capture_output=True,text=True,encoding='utf8')
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads(result.stdout)
        self.assertIn('Повтор чтения 2/3',data['wait'])
        self.assertIn('5 с',data['wait']);self.assertIn('2 с',data['aged'])
        self.assertIn('Повтор чтения 3/3',data['request'])
        for name in ('first','stale','action','invalid'):self.assertNotIn('Повтор чтения',data[name])
