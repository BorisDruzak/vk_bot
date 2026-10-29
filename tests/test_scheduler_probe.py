"""Read-only runtime probe, isolated SQLite and a mocked Chromium context."""
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, closing, nullcontext, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import vk_accounts as accounts
import vk_account_smoke as smoke
import vk_api_read as api
import vk_batch as engine
import vk_community as community
import vk_groups as groups
import vk_unified as app


class SchedulerProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.first=self.root/'first.sqlite3';self.second=self.root/'second.sqlite3'
        self.profile=self.root/'profile';self.profile.mkdir()
        self.stamp=int(datetime.now(timezone.utc).timestamp())-100
        self.row=dict(group_id=101,post_id=11,published_at=self.stamp,community_id=501)
        cfg=json.loads((Path(app.__file__).parent/'vk_groups.json').read_text(encoding='utf-8'))
        cfg['groups']=[dict(id=101,name='Source',url='https://vk.com/club101',enabled=True,like_enabled=True,repost_enabled=True)]
        self.config=self.root/'config.json';self.config.write_text(json.dumps(cfg),encoding='utf-8')
        with closing(engine.db_open(self.first)) as db:
            groups.init_dates(db);api.init_api(db);engine.add_posts(db,101,[11])
            groups.save_date(db,101,11,self.stamp,api.API_SOURCE,True)
            db.execute('INSERT INTO api_seen_posts VALUES(?,?,?,?,1)',(101,11,self.stamp,engine.moment()));db.commit()
        with closing(engine.db_open(self.second)):pass
        self.before=[hashlib.sha256(p.read_bytes()).digest() for p in (self.first,self.second)]

    def patches(self,module):
        stack=ExitStack()
        stack.enter_context(patch.object(engine,'DB_PATH',self.first));stack.enter_context(patch.object(smoke,'ACCOUNT2_DB',self.second))
        stack.enter_context(patch.object(app,'exclusive_run',return_value=nullcontext()))
        stack.enter_context(patch.object(accounts,'exclusive_profile',return_value=nullcontext()))
        stack.enter_context(patch.object(accounts,'profile_for_account',return_value=self.profile))
        stack.enter_context(patch.object(community,'pinned_identity',side_effect=lambda n:1001 if n==1 else 2002))
        stack.enter_context(patch.object(community,'live_identity'))
        stack.enter_context(patch.object(engine,'go_to_post'));stack.enter_context(patch.object(engine,'ensure_ok'))
        stack.enter_context(patch.object(engine,'configure_navigation_pacing'));stack.enter_context(patch.object(engine,'watch_rate_limit_responses'))
        stack.enter_context(patch.object(groups,'exact_post_date_info',return_value={'publishedAt':self.stamp}))
        stack.enter_context(patch.object(engine,'wait_post_footer_info',return_value={'ok':True,'liked':True,'visibleLikes':1}))
        stack.enter_context(patch.object(engine,'post_footer_button',return_value=MagicMock()))
        stack.enter_context(patch.object(engine,'wait_share_destination',return_value={'ok':True,'state':{'wall':True,'direct_message':False}}))
        stack.enter_context(patch.object(community.ui,'prepare',return_value={'community_id':501}))
        for owner,name in ((engine,'click_like'),(engine,'click_share_then_publish'),(community.ui,'submit'),(engine,'db_open'),(community,'init_state')):
            stack.enter_context(patch.object(owner,name,side_effect=AssertionError('Forbidden probe call: '+name)))
        self.page=MagicMock();self.page.is_closed.return_value=False
        self.final=MagicMock();self.final.count.return_value=1;self.final.is_visible.return_value=True;self.final.is_enabled.return_value=True
        self.page.locator.return_value=self.final
        self.blocked=0
        def evaluate(script,*args):
            if script==engine.SHARE_DIALOG_JS:return {'ok':True,'state':{'wall':True,'direct_message':False}}
            if script==engine.PUBLISH_CONTROLS_JS:return {'ok':True,'candidates':[{'disabled':False}]}
            return self.blocked
        self.page.evaluate.side_effect=evaluate
        self.context=MagicMock();self.context.new_page.return_value=self.page
        pw=MagicMock();pw.chromium.launch_persistent_context.return_value=self.context
        cm=MagicMock();cm.__enter__.return_value=pw;cm.__exit__.return_value=False
        stack.enter_context(patch.object(module,'sync_playwright',return_value=cm))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return stack

    def assert_unchanged(self):
        self.assertEqual(self.before,[hashlib.sha256(p.read_bytes()).digest() for p in (self.first,self.second)])

    def test_all_three_kinds_for_both_accounts_never_write_or_call_actions(self):
        import vk_scheduler_probe as probe
        for account in (1,2):
            for kind in ('like','personal','community'):
                with self.subTest(account=account,kind=kind),self.patches(probe):
                    result=probe.probe(account,kind,self.row,config_path=self.config)
                self.assertTrue(result['source_confirmed']);self.assertTrue(result['date_confirmed']);self.assertTrue(result['account_confirmed'])
                self.assertEqual(result['like_state'],True);self.assertEqual(result['blocked_clicks'],0)
                self.assertTrue(result['finalbutton_visible']);self.assertTrue(result['finalbutton_enabled'])
                self.final.click.assert_not_called();self.page.close.assert_called_once();self.context.close.assert_called_once()
                self.assert_unchanged()

    def test_hard_guards_installed_before_new_page(self):
        import vk_scheduler_probe as probe
        events=[]
        with self.patches(probe):
            self.context.add_init_script.side_effect=lambda *a,**kw:events.append('guard')
            self.context.new_page.side_effect=lambda:events.append('page') or self.page
            probe.probe(1,'like',self.row,config_path=self.config)
        self.assertEqual(events,['guard','page'])
        script=self.context.add_init_script.call_args.args[0]
        for marker in ('post_footer_action_like','data-vk-batch-footer-action','share_tab_submit','data-vk-poc-publish','submit','stopImmediatePropagation'):
            self.assertIn(marker,script)

    def test_active_batch_defers_probe_without_browser_or_state_change(self):
        import vk_scheduler_probe as probe
        with self.patches(probe),accounts.exclusive_activity(self.root), \
             patch.object(probe,'sync_playwright') as browser:
            with self.assertRaises(app.RunAlreadyActive):
                probe.probe(1,'like',self.row,config_path=self.config)
            browser.assert_not_called()
        self.assert_unchanged()

    def test_any_blocked_engagement_click_invalidates_probe(self):
        import vk_scheduler_probe as probe
        with self.patches(probe):
            self.blocked=1
            with self.assertRaisesRegex(engine.StopRun,'запрещён'):
                probe.probe(1,'like',self.row,config_path=self.config)
        self.assert_unchanged();self.context.close.assert_called_once()

    def test_wrong_account_is_not_reported_as_success(self):
        import vk_scheduler_probe as probe
        with self.patches(probe),patch.object(community,'live_identity',side_effect=engine.StopRun('wrong account')):
            with self.assertRaisesRegex(engine.StopRun,'wrong account'):
                probe.probe(1,'community',self.row,config_path=self.config)
        self.assert_unchanged()

    def test_source_date_change_stops_before_share_chooser(self):
        import vk_scheduler_probe as probe
        with self.patches(probe),patch.object(groups,'exact_post_date_info',return_value={'publishedAt':self.stamp-60}),\
             patch.object(community.ui,'prepare') as prepare:
            with self.assertRaises(engine.StopRun):probe.probe(1,'community',self.row,config_path=self.config)
        prepare.assert_not_called();self.assert_unchanged()

    def test_personal_chooser_can_only_pick_wall_radio(self):
        import vk_scheduler_probe as probe
        with self.patches(probe),patch.object(engine,'wait_share_destination',return_value={'ok':True,'method':'element','state':{'wall':False,'direct_message':True}}):
            radio=MagicMock()
            self.page.locator.side_effect=lambda selector:radio if selector=='[data-vk-poc-own="true"]' else self.final
            result=probe.probe(1,'personal',self.row,config_path=self.config)
        self.assertEqual(result['destination'],'personal');radio.click.assert_called_once();self.final.click.assert_not_called()
        self.assert_unchanged()

    def test_unknown_or_disabled_final_button_is_failure_without_submit(self):
        import vk_scheduler_probe as probe
        with self.patches(probe):
            self.final.is_enabled.return_value=False
            with self.assertRaises(engine.StopRun):probe.probe(2,'community',self.row,config_path=self.config)
        self.final.click.assert_not_called();self.assert_unchanged()


if __name__=='__main__':unittest.main()
