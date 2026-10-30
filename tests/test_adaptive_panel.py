"""Render adaptive snapshots offline: forecasts are never historical missed slots."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js required for frontend regression')
class AdaptivePanelRenderingTests(unittest.TestCase):
    def render(self, scheduler, accounts=None, edit=False):
        fixture = {'now': '2026-10-30T10:00:00Z', 'account_flow': 'add_any',
                   'scheduler': scheduler, 'accounts': accounts or [],
                   'posts': [], 'groups': [], 'jobs': []}
        script = r"""
const fs=require('node:fs'),vm=require('node:vm');
class Element {
 constructor(tag){this.tag=tag;this.ownText='';this.children=[];this.value='';this.classList={toggle(){}};}
 set textContent(value){this.ownText=String(value);this.children=[];}
 get textContent(){return this.ownText+this.children.map(c=>c.textContent||'').join(' ');}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.ownText='';this.children=[...children];}
 add(child){this.children.push(child);} setAttribute(){} showModal(){}
}
const elements=new Map(),document={
 getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
 createElement(tag){return new Element(tag);},querySelectorAll(){return [];}
};
const fixture=JSON.parse(process.argv[2]);
const context={document,Node:Element,Option:Element,fixture,setInterval(){},fetch(){throw Error('Unexpected request');}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').split('\n(async()=>')[0],context);
vm.runInContext('Date.now=()=>Date.parse(fixture.now);snapshot=fixture;render();',context);
if(process.argv[3]==='edit')vm.runInContext('editAccount(fixture.accounts[0]);',context);
function descendants(node){return [node,...node.children.flatMap(descendants)];}
console.log(JSON.stringify({text:Object.fromEntries([...elements].map(([id,e])=>[id,e.textContent])),
 marks:descendants(elements.get('timeline')).filter(e=>e.className?.split(' ').includes('slot')).map(e=>({cls:e.className,title:e.title})),
 personal:elements.get('personal-reposts')?.value}));
"""
        result = subprocess.run([shutil.which('node'), '-e', script,
                                 str(Path(__file__).resolve().parents[1] / 'web' / 'app.js'),
                                 json.dumps(fixture), 'edit' if edit else ''],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def scheduler(self, **overrides):
        adaptive = {'day': '2026-10-30', 'state': 'ready', 'rest_seconds': 540,
                    'rest_until': None, 'active_batch': None,
                    'forecast': {'feasible': True, 'reason': 'fits', 'remaining_attempts': 245,
                                 'planned_attempts': 245, 'shortfall': 0, 'batch_count': 17,
                                 'next_target': 15, 'next_start': '2026-10-30T10:00:00Z',
                                 'completion_at': '2026-10-30T18:00:00Z', 'api_required': 100,
                                 'api_available': 120, 'maximum_attempts_fit': 245,
                                 'projected_seconds': 28800, 'available_seconds': 32400,
                                 'assumptions': {'action_seconds': {'like': 40}}, 'revision': 4,
                                 'generated_at': '2026-10-30T09:59:00Z',
                                 'batches': [{'start': '2026-10-30T09:00:00Z',
                                              'end': '2026-10-30T09:10:00Z', 'target': 15}]}}
        adaptive.update(overrides)
        return {'schedule_mode': 'adaptive', 'policy_effective_scope': 'next_batch',
                'work_window': {'start_hour': 0, 'end_hour': 24}, 'adaptive': adaptive,
                'slots': ['2026-10-30T04:00:00Z'], 'batches': []}

    def test_forecast_is_changeable_and_old_future_slots_are_not_missed(self):
        data = self.render(self.scheduler())
        self.assertEqual(data['text']['work-window'], '00:00–24:00 YEKT')
        self.assertIn('17', data['text']['batch-policy'])
        self.assertIn('15', data['text']['batch-policy'])
        self.assertIn('изменяемый прогноз', data['text']['adaptive-forecast'])
        self.assertIn('245', data['text']['adaptive-forecast'])
        self.assertEqual(len(data['marks']), 1)
        self.assertIn('Прогноз', data['marks'][0]['title'])
        self.assertNotIn('skipped', data['marks'][0]['cls'])

    def test_rest_uses_actual_finish_deadline_and_counts_down(self):
        data = self.render(self.scheduler(state='resting', rest_until='2026-10-30T10:04:00Z'))
        self.assertIn('после фактического завершения', data['text']['adaptive-state'])
        self.assertIn('4 мин', data['text']['adaptive-state'])
        self.assertIn('9 мин', data['text']['batch-policy'])

    def test_active_claim_target_is_separate_from_forecast_target(self):
        data = self.render(self.scheduler(state='working', active_batch={
            'slot': 80, 'target_attempts': 12, 'started_at': '2026-10-30T09:58:00Z',
            'estimated_end': '2026-10-30T10:12:00Z'}))
        self.assertIn('12', data['text']['adaptive-state'])
        self.assertIn('зафиксирована', data['text']['adaptive-state'])
        self.assertIn('15', data['text']['adaptive-forecast'])
        self.assertIn('полночь', data['text']['adaptive-state'])

    def test_shortfall_explains_time_and_api_without_promising_verified(self):
        scheduler = self.scheduler(state='infeasible')
        scheduler['adaptive']['forecast'].update(feasible=False, reason='insufficient_time_or_api_budget',
                                                  shortfall=45, planned_attempts=200,
                                                  maximum_attempts_fit=200)
        data = self.render(scheduler)['text']
        self.assertIn('Недобор: 45', data['adaptive-forecast'])
        self.assertIn('API', data['adaptive-forecast'])
        self.assertIn('времени', data['adaptive-forecast'])
        self.assertIn('не гарантирует', data['adaptive-forecast'])
        self.assertIn('uncertain', data['adaptive-forecast'])
        self.assertNotIn('insufficient_time_or_api_budget', data['adaptive-forecast'])

    def test_history_keeps_legacy_cutoff_and_adaptive_midnight_reason(self):
        scheduler = self.scheduler()
        scheduler['batches'] = [
            {'slot': 0, 'planning_mode': 'legacy', 'started_at': '2026-10-30T04:00:00Z',
             'finished_at': '2026-10-30T04:05:00Z', 'status': 'batch_action_deadline'},
            {'slot': 81, 'planning_mode': 'adaptive', 'target_attempts': 8,
             'started_at': '2026-10-30T18:40:00Z', 'finished_at': '2026-10-30T19:00:00Z',
             'status': 'day_deadline'}]
        data = self.render(scheduler)
        self.assertEqual(len(data['marks']), 3)
        self.assertIn('до следующей пачки', data['marks'][0]['title'])
        self.assertIn('полночь', data['marks'][1]['title'])
        self.assertNotIn('до следующей пачки', data['marks'][1]['title'])

    def test_policy_next_batch_and_personal_zero_are_preserved(self):
        account = {'account': 5, 'name': 'Account', 'enabled': False,
                   'policy': {'likes': 50, 'personal_reposts': 0, 'community_reposts': 20,
                              'communities': [101, 102]}}
        data = self.render(self.scheduler(), [account], edit=True)
        self.assertEqual(data['personal'], 0)
        self.assertIn('следующей пачки', data['text']['policy-day'])
        self.assertIn('следующей пачки', data['text']['account-flow-hint'])
        self.assertIn('следующей пачки', data['text']['account-policy-hint'])

    def test_day_complete_and_waiting_api_are_not_worker_shutdown_proof(self):
        for state, label in [('day_complete', 'Дневная очередь завершена'), ('waiting_api', 'Ожидание API')]:
            data = self.render(self.scheduler(state=state))['text']
            self.assertIn(label, data['adaptive-state'])
            self.assertIn('неизвест', data['worker-state'].lower())

    def test_missing_mode_preserves_legacy_schedule(self):
        data = self.render({'work_window': {'start_hour': 9, 'end_hour': 22},
                            'batch_size': 6, 'batch_count': 67,
                            'slots': ['2026-10-30T04:00:00Z'], 'batches': []})
        self.assertEqual(data['text']['work-window'], '09:00–22:00 YEKT')
        self.assertIn('до 6 попыток', data['text']['batch-policy'])
        self.assertIn('skipped', data['marks'][0]['cls'])


if __name__ == '__main__':
    unittest.main()
