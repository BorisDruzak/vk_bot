"""Panel distinguishes saved batch reasons and ages wait times without writes."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node required for panel regression')
class BatchPanelReasonTests(unittest.TestCase):
    def run_js(self, body):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const context={};vm.createContext(context);
for(const name of ['heartbeat','timeline']){
 const helper=source.match(new RegExp('// BEGIN '+name+' helpers([\\s\\S]*?)// END '+name+' helpers'));
 if(!helper)throw new Error(name+' helpers missing');
 vm.runInContext(helper[1],context);
}
const now=Date.parse('2026-10-27T07:00:00Z');
""" + body
        result = subprocess.run([shutil.which('node'), '-e', script,
            str(Path(__file__).resolve().parents[1] / 'web/app.js')],
            capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_saved_reasons_do_not_claim_quota_or_success_for_other_cutoffs(self):
        data = self.run_js(r"""
const codes=['batch_action_deadline','batch_work_window_closed','batch_daily_quota_reached',
 'batch_stop_requested','batch_account_disabled','batch_day_changed','batch_spacing_deadline',
 'batch_guard_rejected','batch_window_or_budget_closed','unrecognized_cutoff'];
console.log(JSON.stringify(Object.fromEntries(codes.map(code=>[code,{
 timeline:context.timelineState('2026-10-27T06:50:00Z',{status:code,finished_at:'2026-10-27T06:59:00Z'},now),
 heartbeat:context.workerHeartbeat({at:'2026-10-27T06:59:59Z',phase:code},now).phaseText}]))));
""")
        reasons = {
            'batch_action_deadline': 'следующ',
            'batch_work_window_closed': 'рабочее окно',
            'batch_daily_quota_reached': 'квот',
            'batch_stop_requested': 'останов',
            'batch_account_disabled': 'отключ',
            'batch_day_changed': 'день',
            'batch_spacing_deadline': '9 мин',
            'batch_guard_rejected': 'причина не уточнена',
            'batch_window_or_budget_closed': 'старый код не различал причину',
        }
        for code, reason in reasons.items():
            with self.subTest(code=code):
                row = data[code]
                self.assertIn(reason, row['timeline']['label'])
                self.assertIn(reason, row['heartbeat'])
                self.assertNotIn(row['timeline']['css'], ('verified', 'pending', 'running'))
                self.assertNotIn('Неизвестный этап', row['heartbeat'])
                if code != 'batch_daily_quota_reached':
                    self.assertNotIn('квота закрыт', row['timeline']['label'])
        self.assertEqual(data['batch_spacing_deadline']['timeline']['css'], 'skipped')
        self.assertEqual(data['unrecognized_cutoff']['timeline']['css'], 'uncertain')
        self.assertIn('Неизвестный этап', data['unrecognized_cutoff']['heartbeat'])

    def test_fresh_wait_shows_yekt_ready_time_and_ages_remaining_window(self):
        data = self.run_js(r"""
const hb={at:'2026-10-27T07:00:00Z',phase:'batch_spacing_wait',active_slot:1,
 spacing_ready_at:'2026-10-27T07:02:10Z',spacing_remaining_seconds:130,
 batch_deadline_at:'2026-10-27T07:05:00Z'};
const get=at=>context.workerHeartbeat(hb,at);
console.log(JSON.stringify({first:get(now),later:get(now+30000),elapsed:get(now+135000),
 stale:get(now+181000),timeline:context.timelineState('2026-10-27T06:59:00Z',
 {slot:1,status:'running'},now,hb),wrongSlot:context.timelineState('2026-10-27T06:59:00Z',
 {slot:2,status:'running'},now,hb)}));
""")
        self.assertIn('12:02:10 YEKT', data['first']['phaseText'])
        self.assertIn('2 мин 10 с', data['first']['phaseText'])
        self.assertIn('1 мин 40 с', data['later']['phaseText'])
        self.assertIn('12:05:00 YEKT', data['first']['phaseText'])
        self.assertIn('5 мин 00 с', data['first']['phaseText'])
        self.assertIn('4 мин 30 с', data['later']['phaseText'])
        self.assertIn('интервал истёк', data['elapsed']['phaseText'])
        self.assertNotIn('осталось', data['stale']['phaseText'])
        self.assertEqual(data['stale']['state'], 'stale')
        self.assertIn('12:02:10 YEKT', data['timeline']['label'])
        self.assertEqual(data['wrongSlot']['css'], 'stale')

    def test_invalid_timing_cannot_invent_a_live_countdown(self):
        data = self.run_js(r"""
const base={at:'2026-10-27T07:00:00Z',phase:'batch_spacing_wait'};
console.log(JSON.stringify({missing:context.workerHeartbeat(base,now),
 invalid:context.workerHeartbeat({...base,spacing_ready_at:'bad',spacing_remaining_seconds:-1,batch_deadline_at:{}},now),
 fallback:context.workerHeartbeat({...base,spacing_remaining_seconds:100},now+30000),
 future:context.workerHeartbeat({...base,at:'2026-10-27T07:10:00Z',spacing_remaining_seconds:100},now)}));
""")
        for key in ('missing', 'invalid', 'future'):
            self.assertNotIn('осталось', data[key]['phaseText'])
            self.assertNotIn('NaN', data[key]['phaseText'])
        self.assertIn('1 мин 10 с', data['fallback']['phaseText'])


if __name__ == '__main__':
    unittest.main()
