import json
from pathlib import Path
import shutil,subprocess,unittest


@unittest.skipUnless(shutil.which('node'),'Node required for timeline regression')
class TimelineSpacingTests(unittest.TestCase):
    def test_cached_status_ages_without_network_or_resetting_form_fields(self):
        script=r"""
const fs=require('node:fs'),vm=require('node:vm');
class Element {
 constructor(){this.ownText='';this.children=[];this.value='';this.classList={toggle(){}};}
 set textContent(v){this.ownText=String(v);this.children=[];}get textContent(){return this.ownText+this.children.map(x=>x.textContent||'').join(' ');}
 append(...v){this.children.push(...v)}replaceChildren(...v){this.children=v;this.ownText=''}add(v){this.children.push(v)}setAttribute(){}removeAttribute(){}
}
const elements=new Map(),document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id)},createElement(){return new Element()},querySelectorAll(){return []}};
const intervals=new Map();let now=Date.parse('2026-10-24T07:00:00Z'),requests=0;
class Clock extends Date{static now(){return now}}
const fixture={now:'2026-10-24T07:00:00Z',accounts:[],jobs:[],posts:[],groups:[],scheduler:{
heartbeat:{at:'2026-10-24T07:00:00Z',phase:'refreshing_counters',active_slot:0},
slots:['2026-10-24T07:00:00Z','2026-10-24T07:10:00Z'],batches:[{slot:0,status:'running',finished_at:null}]}};
const context={document,Node:Element,Option:Element,fixture,Date:Clock,
setInterval(fn,ms){intervals.set(ms,fn)},fetch:async()=>{requests++;return {ok:true,json:async()=>({authenticated:false})}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
vm.runInContext("snapshot=fixture;csrf='fixture';render();",context);document.getElementById('likes').value='17';
const first=elements.get('timeline').children[0].children[1].children[0].className;
now+=3600000;
if(typeof intervals.get(30000)!=='function')throw new Error('cached status timer missing');
intervals.get(30000)();
console.log(JSON.stringify({first,last:elements.get('timeline').children[0].children[1].children[0].className,
 future:elements.get('timeline').children[0].children[1].children[1].className,
 state:elements.get('worker-state').textContent,unsaved:elements.get('likes').value,requests}));
"""
        r=subprocess.run([shutil.which('node'),'-e',script,str(Path(__file__).resolve().parents[1]/'web/app.js')],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(r.returncode,0,r.stderr);data=json.loads(r.stdout)
        self.assertEqual(data['first'],'slot running');self.assertEqual(data['last'],'slot stale')
        self.assertEqual(data['future'],'slot pending');self.assertIn('старше 3 минут',data['state'])
        self.assertEqual(data['unsaved'],'17');self.assertEqual(data['requests'],1)

    def test_finished_spacing_is_skipped_and_waiting_is_running(self):
        script=r"""
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const helper=source.match(/\/\/ BEGIN timeline helpers([\s\S]*?)\/\/ END timeline helpers/);
if(!helper)throw new Error('timeline helper missing');
const context={};vm.createContext(context);vm.runInContext(helper[1],context);
const heartbeatHelper=source.match(/\/\/ BEGIN heartbeat helpers([\s\S]*?)\/\/ END heartbeat helpers/);
vm.runInContext(heartbeatHelper[1],context);
const now=Date.parse('2026-10-24T07:00:00Z'),slot='2026-10-24T04:10:04Z';
const get=(batch,time=slot,heartbeat={at:'2026-10-24T06:59:59Z',phase:'batch_spacing_wait',active_slot:1})=>context.timelineState(time,batch,now,heartbeat);
console.log(JSON.stringify({
 skip:get({status:'batch_spacing_wait',finished_at:'2026-10-24T04:11:28Z'}),
 wait:get({slot:1,status:'running',finished_at:null}),future:get(null,'2026-10-24T08:00:00Z'),
 missing:get(null),grace:get(null,'2026-10-24T06:59:00Z'),
 complete:get({status:'batch_complete',finished_at:'2026-10-24T04:11:28Z'}),
 unknown:get({status:'unexpected_partial',finished_at:'2026-10-24T04:11:28Z'}),
 stale:get({slot:1,status:'running',finished_at:null},slot,{at:'2026-10-24T04:00:00Z',phase:'batch_spacing_wait',active_slot:1}),
 wrongSlot:get({slot:1,status:'running',finished_at:null},slot,{at:'2026-10-24T06:59:59Z',phase:'refreshing_counters',active_slot:2}),
 partial:get({status:'queue_empty',finished_at:'2026-10-24T04:11:28Z'}),
 reserveBudget:get({status:'reserve_counter_refresh_budget',finished_at:'2026-10-24T04:11:28Z'}),
 reserveEmpty:get({status:'reserve_counter_refresh_empty',finished_at:'2026-10-24T04:11:28Z'}),
 reserveStopped:get({status:'reserve_counter_refresh_stopped',finished_at:'2026-10-24T04:11:28Z'}),
 reserveLock:get({status:'reserve_waiting_profile_lock',finished_at:'2026-10-24T04:11:28Z'}),
 reserveDeadline:get({status:'reserve_counter_refresh_deadline',finished_at:'2026-10-24T04:11:28Z'}),
 emptyDeadline:get({status:'counter_refresh_deadline',finished_at:'2026-10-24T04:11:28Z'}),
 reserveHeartbeat:['reserve_counter_refresh_budget','reserve_counter_refresh_empty','reserve_counter_refresh_stopped','reserve_waiting_profile_lock','reserve_counter_refresh_deadline'].map(phase=>context.workerHeartbeat({at:'2026-10-24T06:59:59Z',phase},now).phaseText),
 staleSnapshot:context.timelineState(slot,{slot:1,status:'running'},now,{at:'2026-10-24T06:59:59Z',phase:'refreshing_counters',active_slot:1},now+3600000)}));
"""
        r=subprocess.run([shutil.which('node'),'-e',script,str(Path(__file__).resolve().parents[1]/'web/app.js')],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(r.returncode,0,r.stderr)
        data=json.loads(r.stdout)
        self.assertEqual(data['skip']['css'],'skipped');self.assertIn('9',data['skip']['label']);self.assertIn('Пропущено',data['skip']['label'])
        self.assertEqual(data['wait']['css'],'running');self.assertEqual(data['future']['css'],'pending')
        self.assertEqual(data['missing']['css'],'skipped');self.assertEqual(data['grace']['css'],'pending')
        self.assertEqual(data['complete']['css'],'verified');self.assertEqual(data['unknown']['css'],'uncertain')
        self.assertEqual(data['stale']['css'],'stale');self.assertEqual(data['wrongSlot']['css'],'stale')
        self.assertEqual(data['partial']['css'],'partial');self.assertNotIn('Пропущено',data['partial']['label'])
        for key in ('reserveBudget','reserveEmpty','reserveStopped','reserveLock','reserveDeadline'):
            self.assertEqual(data[key]['css'],'partial');self.assertNotIn('Пропущено',data[key]['label'])
        for label in data['reserveHeartbeat']:self.assertIn('частично',label)
        self.assertEqual(data['emptyDeadline']['css'],'skipped')
        self.assertEqual(data['staleSnapshot']['css'],'stale')


if __name__=='__main__':unittest.main()
