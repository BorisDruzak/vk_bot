"""Isolated browser-script regressions for the optional add-any account flow."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js required for frontend regression')
class ConnectUITests(unittest.TestCase):
    def run_ui(self, scenario):
        bootstrap = r"""
const fs=require('node:fs'),vm=require('node:vm');
class Element {
 constructor(){this.ownText='';this.children=[];this.value='';this.classList={toggle(){}};}
 set textContent(value){this.ownText=String(value);this.children=[];}
 get textContent(){return this.ownText+this.children.map(child=>child.textContent||'').join(' ');}
 append(...children){this.children.push(...children);} replaceChildren(...children){this.ownText='';this.children=[...children];}
 add(child){this.children.push(child);} setAttribute(){} showModal(){this.open=true;} close(){this.open=false;}
}
const elements=new Map(),document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement(){return new Element();},querySelectorAll(){return [];}};
const proof={state:'matched',expected_vk_id:123,observed_vk_id:123,fresh:true,checked_at:'2026-10-23T06:00:00Z'};
const row={account:3,name:'Мой профиль',vk_id:null,enabled:false,login:{state:'waiting_browser',flow:'add_any'},policy:{likes:0,personal_reposts:0,community_reposts:0,communities:[]}};
const fixture={account_flow:'add_any',scheduler:{},accounts:[row],jobs:[],posts:[],groups:[]};
const calls=[],context={document,Node:Element,Option:Element,fixture,proof,row,calls,setInterval(){}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8').split('\n(async()=>')[0],context);
vm.runInContext("snapshot=fixture;csrf='qa';",context);
"""
        result = subprocess.run(
            [shutil.which('node'), '-e', bootstrap + scenario, str(Path(__file__).resolve().parents[1] / 'web' / 'app.js')],
            capture_output=True, text=True, encoding='utf-8',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_flexible_cards_hide_internal_numbers_and_manual_binding_controls(self):
        data = self.run_ui(r"""
vm.runInContext('renderAccounts();editAccount(row);',context);
const editing={hidden:elements.get('name-label').hidden,required:elements.get('account-name').required,value:elements.get('account-name').value};
const pending=elements.get('account-list').textContent;
const pendingButtons=elements.get('account-list').children[0].children.flatMap(child=>child.children).filter(child=>child.type==='button');
const busyActions=pendingButtons.filter(button=>['Начать вход','Проверить вход'].includes(button.textContent)).every(button=>button.disabled);
vm.runInContext("row.vk_id=123;row.authentication=proof;row.login={state:'complete',flow:'add_any'};renderAccounts();",context);
const connected=elements.get('account-list').textContent;
const guards=vm.runInContext("Object.fromEntries(['opening','waiting_browser','awaiting_login','complete'].map(state=>[state,accountAuthentication({...row,login:{state}}).canEnable]))",context);
vm.runInContext('editAccount(null);',context);
console.log(JSON.stringify({pending,connected,editing,optional:!elements.get('account-name').required,guards,busyActions}));
""")
        for content in (data['pending'], data['connected']):
            self.assertIn('Мой профиль', content)
            for old in ('№3', 'Ожидался', 'Сохранённая привязка', 'Завершить вход'):
                self.assertNotIn(old, content)
            self.assertIn('профиль сохранится автоматически', content)
        self.assertIn('VK ID: 123', data['connected'])
        self.assertEqual(data['editing'], {'hidden':False, 'required':False, 'value':'Мой профиль'})
        self.assertTrue(data['optional'])
        self.assertTrue(data['busyActions'])
        self.assertEqual(data['guards'], {'opening':False,'waiting_browser':False,'awaiting_login':False,'complete':True})

    def test_waiting_browser_polls_and_redirect_completion_refreshes_once(self):
        data = self.run_ui(r"""
vm.runInContext(`rpc=async(operation,fields={})=>{
 calls.push({operation,...fields});
 if(operation==='login_status')return {state:'complete',flow:'add_any',canonical_account:1,authentication:proof};
 if(operation==='snapshot')return {...fixture,accounts:[{...row,account:1,name:'Подключённый профиль',vk_id:123,authentication:proof,login:{state:'complete',flow:'add_any'}}]};
 throw new Error('unexpected operation '+operation);
};`,context);
(async()=>{await vm.runInContext('pollLoginStatuses()',context);await vm.runInContext('pollLoginStatuses()',context);
 console.log(JSON.stringify({calls,notice:document.getElementById('notice').textContent,accounts:document.getElementById('account-list').textContent}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
        self.assertEqual(data['calls'], [{'operation':'login_status','account':3}, {'operation':'snapshot'}])
        self.assertIn('Аккаунт подключён', data['notice'])
        self.assertIn('Подключённый профиль', data['accounts'])
        self.assertNotIn('Мой профиль', data['accounts'])

    def test_optional_add_name_and_existing_name_are_sent_without_auto_enable(self):
        data = self.run_ui(r"""
vm.runInContext(`rpc=async(operation,fields={})=>{calls.push({operation,...fields});if(operation==='add_account')return {account:3};if(operation==='login_start')return {state:'opening'};if(operation==='snapshot')return fixture;return {};};editAccount(null);`,context);
(async()=>{
 await elements.get('account-form').onsubmit({preventDefault(){},submitter:new Element()});
 vm.runInContext("row.login={state:'complete',flow:'add_any'};row.vk_id=123;row.authentication=proof;editAccount(row);$('account-name').value='Рабочий VK';$('account-enabled').checked=false;",context);
 await elements.get('account-form').onsubmit({preventDefault(){},submitter:new Element()});
 console.log(JSON.stringify({calls,notice:elements.get('notice').textContent}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
        mutations = [call for call in data['calls'] if call['operation'] != 'snapshot']
        self.assertEqual(mutations[:2], [{'operation':'add_account','name':'Новый аккаунт'}, {'operation':'login_start','account':3}])
        self.assertEqual(mutations[2]['operation'], 'configure_account')
        self.assertEqual(mutations[2]['name'], 'Рабочий VK')
        self.assertFalse(mutations[2]['enabled'])


if __name__ == '__main__':
    unittest.main()
