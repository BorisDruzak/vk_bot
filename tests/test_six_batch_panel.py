"""Read-only diagnostics and panel distinguish saved capacity from future policy."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import vk_accounts as accounts
import vk_control as control
import vk_schedule_policy as policy
import vk_scheduler_state as ledger
import vk_scheduler_timing as timing


def four_accounts(size):
    return policy.validate_config({'end_hour':22,'batch_size':size,'accounts':{
        str(n):{'likes':50,'personal_reposts':10,'community_reposts':20,
                'communities':[n*10+1,n*10+2]} for n in (1,2,5,6)}})


def five_accounts():
    config=four_accounts(6)
    config['accounts']['7']={'likes':50,'personal_reposts':0,'community_reposts':20,'communities':[71,72]}
    config['planned_batches']=67
    return policy.validate_config(config)


class SixBatchTimingReportTests(unittest.TestCase):
    def report(self,capacity,attempts,*,telemetry=True,config=None,batch_slot=0,slot_count=None):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.executescript('CREATE TABLE schedule_days(day,config,slots);'
            'CREATE TABLE scheduler_batches(day,slot,started_at,finished_at,status);'
            'CREATE TABLE scheduler_jobs(id,day,slot,status,attempt_id);'
            'CREATE TABLE scheduler_diagnostics(id,at,event,snapshot);')
        config=config or four_accounts(capacity)
        first=datetime(2026,10,29,4,tzinfo=timezone.utc)
        count=slot_count if slot_count is not None else config.get('planned_batches',80 if capacity==4 else 54)
        slots=[(first+timedelta(seconds=540*i)).isoformat() for i in range(count)]
        db.execute('INSERT INTO schedule_days VALUES(?,?,?)',('2026-10-29',json.dumps(config),json.dumps(slots)))
        db.execute('INSERT INTO scheduler_batches VALUES(?,?,?,?,?)',
            ('2026-10-29',batch_slot,slots[batch_slot],(first+timedelta(seconds=540*(batch_slot+1))).isoformat(),'batch_action_deadline'))
        for i in range(attempts):
            db.execute('INSERT INTO scheduler_jobs VALUES(?,?,?,?,?)',(i+1,'2026-10-29',batch_slot,
                'verified' if i==0 else 'uncertain',i+10))
        if telemetry is not False:
            snapshot={'at':slots[batch_slot],'event':'batch_timing','slot':batch_slot,
                      'effective_window':{} if telemetry=='empty' else {'attempts':attempts,'verified':int(attempts>0)}}
            db.execute('INSERT INTO scheduler_diagnostics VALUES(?,?,?,?)',(1,slots[0],'batch_timing',json.dumps(snapshot)))
        before=db.total_changes
        result=timing.report(db,'2026-10-29',first+timedelta(hours=1))
        self.assertEqual(db.total_changes,before)
        return result

    def test_saved_four_capacity_explains_unstarted_fourth_and_retains_legacy_field(self):
        row=self.report(4,3)['slots'][0]
        self.assertEqual(row['batch_capacity'],4)
        self.assertTrue(row['next_not_started_before_intent'])
        self.assertTrue(row['fourth_not_started_before_intent'])
        self.assertEqual(row['unfilled'],1)

    def test_saved_six_capacity_explains_unstarted_sixth_without_fourth_claim(self):
        row=self.report(6,5)['slots'][0]
        self.assertEqual(row['batch_capacity'],6)
        self.assertTrue(row['next_not_started_before_intent'])
        self.assertFalse(row['fourth_not_started_before_intent'])
        self.assertEqual(row['unfilled'],1)

    def test_six_place_report_separates_324_places_from_320_quota_and_verified(self):
        result=self.report(6,5)
        self.assertEqual(result['summary']['theoretical_attempts'],324)
        self.assertEqual(result['summary']['daily_quota'],320)
        self.assertEqual(result['summary']['attempts'],5)
        self.assertEqual(result['summary']['verified'],1)

    def test_empty_legacy_slot_preserves_unknown_measurements(self):
        result=self.report(6,0,telemetry=False)
        empty=result['slots'][1]
        self.assertEqual(empty['batch_capacity'],6)
        self.assertFalse(empty['telemetry_available'])
        self.assertIsNone(empty['intent_seconds_available'])
        self.assertIsNone(empty['refresh_finished_at'])
        self.assertFalse(empty['next_not_started_before_intent'])
        self.assertIsNone(result['stages']['click']['median_ms'])

    def test_empty_timing_record_does_not_claim_measured_telemetry(self):
        row=self.report(6,0,telemetry='empty')['slots'][0]
        self.assertFalse(row['telemetry_available'])
        self.assertIsNone(row['intent_seconds_available'])
        self.assertIsNone(row['refresh_finished_at'])

    def test_saved67_report_distributes390_and_separates402_maximum_places(self):
        result=self.report(6,5,config=five_accounts())
        capacities=[row['batch_capacity'] for row in result['slots']]
        self.assertEqual(capacities.count(5),12)
        self.assertEqual(capacities.count(6),55)
        self.assertEqual(result['summary']['planned_attempt_capacity'],390)
        self.assertEqual(result['summary']['theoretical_attempts'],402)
        self.assertEqual(result['summary']['daily_quota'],390)
        self.assertEqual(result['summary']['attempts'],5)
        self.assertEqual(result['summary']['verified'],1)

    def test_five_place_slot_filled_with_five_is_not_partial_or_missing_a_sixth(self):
        result=self.report(6,5,config=five_accounts())
        row=result['slots'][0]
        self.assertEqual(row['batch_capacity'],5)
        self.assertEqual(row['unfilled'],0)
        self.assertFalse(row['next_not_started_before_intent'])
        self.assertEqual(result['summary']['partial'],0)

    def test_legacy400_quota_without_explicit_distribution_keeps_67_six_place_slots(self):
        config=five_accounts();config.pop('planned_batches')
        config['accounts']['7']['personal_reposts']=10
        result=self.report(6,5,config=config,slot_count=67)
        self.assertEqual([row['batch_capacity'] for row in result['slots']],[6]*67)
        self.assertEqual(result['summary']['theoretical_attempts'],402)
        self.assertEqual(result['summary']['planned_attempt_capacity'],402)
        self.assertEqual(result['summary']['daily_quota'],400)

    def test_six_place_slot_keeps_sixth_and_five_place_slot_keeps_fifth_explanation(self):
        for slot,attempts,capacity in ((0,4,5),(1,5,6)):
            with self.subTest(slot=slot):
                result=self.report(6,attempts,config=five_accounts(),batch_slot=slot)
                row=result['slots'][slot]
                self.assertEqual(row['batch_capacity'],capacity)
                self.assertEqual(row['unfilled'],1)
                self.assertTrue(row['next_not_started_before_intent'])
                self.assertEqual(result['summary']['partial'],1)


class SixBatchSnapshotTests(unittest.TestCase):
    def snapshot(self,pending):
        current=four_accounts(4)
        # Base template has two actors; the registry supplies the actual actors.
        base=policy.validate_config({'end_hour':22,'batch_size':6,
            **({'planned_batches':pending['planned_batches']} if 'planned_batches' in pending else {})})
        numbers=[int(n) for n in pending['accounts']]
        now=datetime.now(timezone.utc);day=str(policy.local_day(now))
        first=now.astimezone(policy.LOCAL_TZ).replace(hour=9,minute=0,second=0,microsecond=0)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);db_path=root/'vk_scheduler.sqlite3'
            state=ledger.State(db_path,{},config=current)
            state.db.execute('INSERT INTO schedule_days VALUES(?,?,?)',(day,json.dumps(current),
                json.dumps([(first+timedelta(seconds=540*i)).isoformat() for i in range(80)])))
            state.db.commit();state.close();before=db_path.read_bytes()
            registry={'version':1,'accounts':{str(n):{'account':n,'name':f'Actor {n}',
                'enabled':True,'policy':pending['accounts'][str(n)]} for n in numbers}}
            with ExitStack() as stack:
                replacements=[(accounts,'ROOT',root),(accounts,'account_numbers',lambda:numbers),
                    (accounts,'database_for_account',lambda n:root/f'absent-{n}.sqlite3'),
                    (accounts,'load_bindings',lambda *args:{'accounts':{}}),
                    (accounts,'load_registry',lambda:registry),
                    (accounts,'account_record',lambda n:registry['accounts'][str(n)]),
                    (accounts,'profile_for_account',lambda n:root/f'profile-{n}'),
                    (control,'login_status',lambda n:{'state':'idle'})]
                for module,name,replacement in replacements:stack.enter_context(patch.object(module,name,replacement))
                stack.enter_context(patch('vk_authentication.status',return_value={'state':'unverified'}))
                stack.enter_context(patch('vk_connections.enabled',return_value=False))
                stack.enter_context(patch('vk_statistics_job.status',return_value={}))
                stack.enter_context(patch('vk_scheduler.load_settings',return_value=(base,{})))
                result=control.snapshot()
            self.assertEqual(db_path.read_bytes(),before)
        scheduler=result['scheduler']
        self.assertEqual((scheduler['batch_size'],scheduler['batch_count']),(4,80))
        self.assertEqual(scheduler['policy_effective_day'],str(policy.local_day(now)+timedelta(days=1)))
        self.assertEqual(scheduler['work_window'],{'start_hour':9,'end_hour':22})
        self.assertEqual(scheduler['pending_work_window'],scheduler['work_window'])
        self.assertEqual(len(scheduler['slots']),80)
        return scheduler

    def test_snapshot_preserves_saved_four_and_derives_six_future_from_registry(self):
        scheduler=self.snapshot(four_accounts(6))
        self.assertEqual((scheduler['pending_batch_size'],scheduler['pending_batch_count']),(6,54))

    def test_snapshot_preserves_today80_and_exposes_five_actor_67_distribution(self):
        scheduler=self.snapshot(five_accounts())
        self.assertEqual((scheduler['pending_batch_size'],scheduler['pending_batch_count']),(6,67))
        self.assertEqual(scheduler['batch_attempt_distribution'],{'4':80})
        self.assertEqual(scheduler['pending_batch_attempt_distribution'],{'5':12,'6':55})


@unittest.skipUnless(shutil.which('node'),'Node required for six-place panel regression')
class SixBatchPanelTests(unittest.TestCase):
    def render(self,expression):
        script=r"""
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8'),nodes={};
const ctx={nodes,snapshot:null,$:id=>nodes[id]||(nodes[id]={textContent:''}),
  value:n=>n??'—',date:()=>'',text:()=>({append(){}}),table:()=>{}};vm.createContext(ctx);
for(const name of ['timing helpers','schedule batch helpers']){
  const body=source.match(new RegExp('// BEGIN '+name+'([\\s\\S]*?)// END '+name));
  if(!body)throw Error(name+' missing');vm.runInContext(body[1],ctx);
}
const start=source.indexOf('function renderTiming(){');
vm.runInContext(source.slice(start,source.indexOf('function action(',start)),ctx);
console.log(JSON.stringify(vm.runInContext(process.argv[2],ctx)));
"""
        run=subprocess.run([shutil.which('node'),'-e',script,
            str(Path(__file__).resolve().parents[1]/'web/app.js'),expression],
            capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(run.returncode,0,run.stderr)
        return json.loads(run.stdout)

    def test_reasons_label_saved_sixth_and_legacy_fourth_without_inventing_measurements(self):
        data=self.render("({six:timingReason({causes:['action_deadline'],batch_capacity:6,attempts:5,next_not_started_before_intent:true,telemetry_available:true}),four:timingReason({causes:[],fourth_not_started_before_intent:true}),unknown:timingReason({causes:[],telemetry_available:false})})")
        self.assertIn('шестая',data['six']);self.assertNotIn('четвёртая',data['six'])
        self.assertIn('без intent',data['six']);self.assertIn('четвёртая',data['four'])
        self.assertIn('не измерялись',data['unknown'])
        self.assertNotIn('не запускалась',data['unknown'])

    def test_plan_text_distinguishes_saved_today_and_future_capacity(self):
        data=self.render("({plan:scheduleBatchText({batch_size:4,batch_count:80,pending_batch_size:6,pending_batch_count:54,policy_effective_day:'2026-10-30'}),unknown:scheduleBatchText({})})")
        self.assertIn('Сегодня',data['plan']);self.assertIn('4',data['plan']);self.assertIn('80',data['plan'])
        self.assertIn('2026-10-30',data['plan']);self.assertIn('6',data['plan']);self.assertIn('54',data['plan'])
        self.assertNotIn('подтвержден',data['plan'])
        self.assertIn('уточняется',data['unknown']);self.assertNotIn('4',data['unknown'])

    def test_future67_label_shows_exact_mix_and_five_place_row_never_claims_a_sixth(self):
        data=self.render("({plan:scheduleBatchText({batch_size:4,batch_count:80,pending_batch_size:6,pending_batch_count:67,pending_batch_attempt_distribution:{5:12,6:55},policy_effective_day:'2026-10-30'}),filled:timingReason({batch_capacity:5,attempts:5,next_not_started_before_intent:false,telemetry_available:true}),fifth:timingReason({batch_capacity:5,attempts:4,next_not_started_before_intent:true,telemetry_available:true})})")
        self.assertIn('67',data['plan']);self.assertIn('55 × 6',data['plan']);self.assertIn('12 × 5',data['plan'])
        self.assertNotIn('шестая',data['filled']);self.assertNotIn('не запускалась',data['filled'])
        self.assertIn('пятая',data['fifth'])

    def test_rendered_summary_keeps_planned390_maximum402_and_confirmed1_separate(self):
        rendered=self.render("snapshot={scheduler:{timing:{slots:[{batch_capacity:5,attempts:5,verified:1,causes:[],telemetry_available:true}],summary:{theoretical_attempts:402,planned_attempt_capacity:390,daily_quota:390,attempts:5,verified:1,empty:0,partial:0,missed:0},stages:{}}}};renderTiming();nodes['timing-summary'].textContent")
        self.assertIn('402',rendered)
        self.assertIn('По плану: 390',rendered)
        self.assertIn('Дневная квота: 390',rendered)
        self.assertIn('Зарегистрировано: 5',rendered)
        self.assertIn('verified: 1',rendered)


if __name__=='__main__':unittest.main()
