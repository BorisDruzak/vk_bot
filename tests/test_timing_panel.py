import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'),'Node required for timing panel regression')
class TimingPanelTests(unittest.TestCase):
    def test_missing_samples_are_unknown_and_fourth_before_intent_is_explained(self):
        script=r"""
const fs=require('fs'),vm=require('vm');
const src=fs.readFileSync(process.argv[1],'utf8');
const body=src.match(/\/\/ BEGIN timing helpers([\s\S]*?)\/\/ END timing helpers/);
if(!body)throw Error('timing helpers missing');
const ctx={};vm.createContext(ctx);vm.runInContext(body[1],ctx);
console.log(JSON.stringify({unknown:ctx.timingSeconds(null),zero:ctx.timingSeconds(0),
 info:ctx.timingReason({causes:['spacing_wait','action_deadline'],fourth_not_started_before_intent:true}),
 history:ctx.timingReason({causes:[],telemetry_available:false}),
 missed:ctx.timingReason({causes:[],unclaimed_after_grace:true,telemetry_available:false})}));
"""
        run=subprocess.run([shutil.which('node'),'-e',script,str(Path(__file__).resolve().parents[1]/'web/app.js')],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(run.returncode,0,run.stderr)
        data=json.loads(run.stdout)
        self.assertEqual(data['unknown'],'—')
        self.assertEqual(data['zero'],'0 с')
        self.assertIn('четвёртая',data['info'])
        self.assertIn('без intent',data['info'])
        self.assertIn('540',data['info'])
        self.assertIn('не измерялись',data['history'])
        self.assertIn('S + 120',data['missed'])
