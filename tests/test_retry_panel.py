"""Read-only recovery snapshots and rendered attempt history, without VK access."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

import vk_accounts as accounts
import vk_control as control
import vk_schedule_policy as policy
import vk_scheduler_state as ledger


@unittest.skipUnless(shutil.which('node'), 'Node.js required for frontend regression')
class RetryPanelRenderingTests(unittest.TestCase):
    def render(self, retry=None, recovery=None, job_status='uncertain',block=None):
        fixture = {'scheduler':{}, 'accounts':[], 'posts':[], 'groups':[], 'jobs':[
            {'id':120, 'account':1, 'kind':'like', 'group_id':101, 'post_id':5,
             'status':job_status, 'reason':'confirmation_unknown', 'attempt_id':91}]}
        if retry is not None:
            fixture['scheduler']['action_retry'] = retry
        if block is not None:fixture['scheduler'].update(block=block,paused=block['reason'])
        if recovery is not None:
            fixture['jobs'][0]['recovery'] = recovery
        script = r"""
const fs=require('node:fs'),vm=require('node:vm');
class Element {
 constructor(tag){this.tag=tag;this.ownText='';this.children=[];this.value='';this.classList={toggle(){}};}
 set textContent(value){this.ownText=String(value);this.children=[];}
 get textContent(){return this.ownText+this.children.map(child=>child.textContent||'').join(' ');}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.ownText='';this.children=[...children];}
 add(child){this.children.push(child);} setAttribute(){}
}
const elements=new Map(),document={
 getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
 createElement(tag){return new Element(tag);},querySelectorAll(){return [];}
};
const fixture=JSON.parse(process.argv[2]);
const context={document,Node:Element,Option:Element,fixture,setInterval(){}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').split('\n(async()=>')[0],context);
vm.runInContext('snapshot=fixture;render();',context);
const jobs=elements.get('jobs-table');
function descendants(node){return [node,...node.children.flatMap(descendants)];}
console.log(JSON.stringify({safety:elements.get('safety-state').textContent,jobs:jobs.textContent,
 buttons:descendants(jobs).filter(node=>node.tag==='button').map(node=>node.textContent)}));
"""
        result = subprocess.run([shutil.which('node'), '-e', script,
                                 str(Path(__file__).resolve().parents[1] / 'web' / 'app.js'),
                                 json.dumps(fixture)], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_manual_and_automatic_block_labels_are_distinct(self):
        manual=self.render(block={'kind':'manual','reason':'manual_block'})['safety']
        automatic=self.render(block={'kind':'automatic','reason':'OSError'})['safety']
        self.assertIn('Ручной блок',manual);self.assertIn('вручную',manual)
        self.assertIn('Автоматический блок',automatic);self.assertIn('следующего запланированного дня',automatic)

    def test_held_uncertainty_shows_both_attempts_and_failure_streak(self):
        data = self.render(
            {'enabled':True, 'consecutive_failures':2, 'held':4, 'pending':1},
            {'state':'held', 'passes':2, 'attempts':[
                {'pass':1, 'attempt_id':91, 'outcome':'uncertain', 'reason':'confirmation_unknown'},
                {'pass':2, 'attempt_id':92, 'outcome':'uncertain', 'reason':'retry_unknown'}]})
        self.assertIn('2/3', data['safety'])
        self.assertIn('Удержано: 4', data['safety'])
        self.assertIn('Ожидает повтора: 1', data['safety'])
        self.assertIn('Первая попытка', data['jobs'])
        self.assertIn('Повтор', data['jobs'])
        self.assertIn('91', data['jobs'])
        self.assertIn('92', data['jobs'])
        self.assertIn('retry_unknown', data['jobs'])
        self.assertIn('Удержано', data['jobs'])
        self.assertIn('Неопределённый исход', data['jobs'])
        self.assertNotIn('Подтверждено', data['jobs'])
        self.assertNotIn('Завершено', data['jobs'])
        self.assertEqual(data['buttons'], [])

    def test_pending_retry_without_new_click_does_not_invent_attempt(self):
        data = self.render({'enabled':True, 'consecutive_failures':0, 'held':0, 'pending':1},
            {'state':'pending', 'passes':1, 'attempts':[
                {'pass':1, 'attempt_id':None, 'outcome':'failed', 'reason':'before_intent'}]}, 'failed')
        self.assertIn('Ожидает повтора', data['jobs'])
        self.assertIn('без клика', data['jobs'])
        self.assertIn('before_intent', data['jobs'])
        self.assertNotIn('Подтверждено', data['jobs'])

    def test_old_snapshot_keeps_existing_job_result_without_retry_claims(self):
        data = self.render()
        self.assertIn('Неопределённый исход', data['jobs'])
        self.assertIn('91', data['jobs'])
        self.assertNotIn('/3', data['safety'])
        self.assertNotIn('Повтор', data['jobs'])
        self.assertEqual(data['buttons'], [])


class RetryPanelSnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'vk_scheduler.sqlite3'
        state = ledger.State(self.path, {})
        state.db.execute("""INSERT INTO scheduler_jobs
            (id,day,slot,account,kind,community_id,group_id,post_id,published_at,status,created_at,reason)
            VALUES(120,'2026-10-23',0,1,'like',0,101,5,1,'failed','2026-10-23T04:00:00Z','before_intent')""")
        state.db.commit()
        state.close()

    def snapshot(self):
        cfg = policy.validate_config(None)
        with patch.object(accounts, 'ROOT', self.root), \
             patch.object(accounts, 'account_numbers', return_value=[1]), \
             patch.object(accounts, 'database_for_account', return_value=self.root / 'absent.sqlite3'), \
             patch.object(accounts, 'load_bindings', return_value={'accounts':{}}), \
             patch.object(accounts, 'effective_policy', return_value=cfg), \
             patch.object(accounts, 'account_record', return_value={'account':1, 'enabled':False}), \
             patch.object(accounts, 'profile_for_account', return_value=self.root / 'absent-profile'), \
             patch.object(control, 'login_status', return_value={'state':'idle'}), \
             patch('vk_authentication.status', return_value={'state':'unverified'}), \
             patch('vk_connections.enabled', return_value=False), \
             patch('vk_statistics_job.status', return_value={}), \
             patch('vk_scheduler.load_settings', return_value=(cfg, {})):
            return control.snapshot()

    def test_legacy_readonly_snapshot_exposes_disabled_recovery_without_migration(self):
        # A pre-v6.33 DB must stay usable, and snapshot must never initialize tables.
        import sqlite3
        with closing(sqlite3.connect(self.path)) as db:
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                if name in ('scheduler_action_recovery', 'scheduler_action_passes'):
                    db.execute('DROP TABLE "' + name + '"')
            db.commit()
        before = self.path.read_bytes()
        result = self.snapshot()
        self.assertEqual(result['scheduler'].get('action_retry'),
                         {'enabled':False, 'consecutive_failures':0, 'held':0, 'pending':0})
        self.assertIsNone(result['jobs'][0].get('recovery'))
        self.assertEqual(self.path.read_bytes(), before)

    def test_snapshot_attaches_recovery_to_the_matching_job(self):
        from types import SimpleNamespace
        import sys
        summary = {'enabled':True, 'consecutive_failures':2, 'held':1, 'pending':0}
        recovery = {'state':'held', 'passes':2, 'attempts':[
            {'pass':1, 'attempt_id':91, 'outcome':'uncertain', 'reason':'confirmation_unknown'},
            {'pass':2, 'attempt_id':None, 'outcome':'uncertain', 'reason':'not_found'}]}
        class ReadLedger:
            def __init__(self, state): self.state = state
            def summary(self): return summary
            def job_snapshot(self, job_id): return recovery if job_id == 120 else None
        before = self.path.read_bytes()
        with patch.dict(sys.modules, {'vk_action_retry':SimpleNamespace(RetryLedger=ReadLedger)}):
            result = self.snapshot()
        self.assertEqual(result['scheduler'].get('action_retry'), summary)
        self.assertEqual(result['jobs'][0].get('recovery'), recovery)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
