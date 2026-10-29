import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'),'Node required for work window panel')
class WorkWindowPanelTests(unittest.TestCase):
    def test_panel_distinguishes_saved_today_from_future_window(self):
        script=r"""
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');const context={};vm.createContext(context);
const block=source.match(/\/\/ BEGIN schedule window helpers([\s\S]*?)\/\/ END schedule window helpers/);
if(!block)throw Error('schedule window rendering missing');
vm.runInContext(block[1],context);
const today={start_hour:9,end_hour:19},next={start_hour:9,end_hour:22};
console.log(JSON.stringify({pending:context.scheduleWindowText({work_window:today,pending_work_window:next,policy_effective_day:'2026-10-29'}),
 current:context.scheduleWindowText({work_window:next,pending_work_window:next}),
 unknown:context.scheduleWindowText({}),invalid:context.workWindowLabel({start_hour:9,end_hour:23})}));
"""
        result=subprocess.run([shutil.which('node'),'-e',script,str(Path(__file__).resolve().parents[1]/'web/app.js')],
                              capture_output=True,text=True,encoding='utf8')
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads(result.stdout)
        self.assertIn('09:00–19:00',data['pending'])
        self.assertIn('09:00–22:00',data['pending'])
        self.assertIn('2026-10-29',data['pending'])
        self.assertIn('09:00–22:00',data['current'])
        self.assertNotIn('19:00',data['current'])
        self.assertNotIn('19:00',data['unknown'])
        self.assertIsNone(data['invalid'])
