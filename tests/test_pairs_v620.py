"""Offline regression tests for user-approved two-action trial."""
from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import vk_api_read as api
import vk_batch as engine
import vk_groups as multi
import vk_unified as u


class ApprovedPairsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.db=engine.db_open(self.root/'state.sqlite')
        multi.init_dates(self.db)
        api.init_api(self.db)
        self.g={'id':101,'name':'Тестовая администрация','enabled':True,
                'like_enabled':True,'repost_enabled':True}
        self.now=int(datetime.now(timezone.utc).timestamp())
        self.cfg={'lookback_days':7,'read_pause_seconds':20,'action_pause_seconds':0,
                  'limits':{'actions_per_run':3,'actions_per_24h':25,
                            'likes_per_run':2,'likes_per_24h':20,
                            'reposts_per_run':1,'reposts_per_24h':5}}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add(self,pid, seconds=100, like='pending', repost='pending', api_provenance=True):
        engine.add_posts(self.db,101,[pid])
        multi.save_date(self.db,101,pid,self.now-seconds,
                        api.API_SOURCE if api_provenance else 'vk-post-json',True)
        self.db.execute('UPDATE posts SET like_status=?,repost_status=? WHERE group_id=101 AND post_id=?',
                        (like,repost,pid))
        if api_provenance:
            self.db.execute('INSERT INTO api_seen_posts(group_id,post_id,published_at,seen_at,valid) '
                            'VALUES(?,?,?,?,1)',(101,pid,self.now-seconds,engine.moment()))
        self.db.commit()

    def csv(self,ids,approve='да'):
        path=self.root/'approved.csv'
        with path.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=u.CSV_FIELDS,delimiter=';')
            writer.writeheader()
            for pid in ids:
                writer.writerow({'approve':approve,'group_id':101,'post_id':pid,
                                 'url':f'https://vk.com/wall-101_{pid}',
                                 'published_at_utc':datetime.fromtimestamp(self.now-100,timezone.utc).isoformat(),
                                 'group_name':'Группа','like_status':'pending','repost_status':'pending'})
        return path

    def test_export_requires_api_and_writes_blank_approval(self):
        self.add(11)
        self.add(12,api_provenance=False)
        path=self.root/'export.csv'
        with redirect_stdout(io.StringIO()):
            n=u.export_pair_review(self.db,[self.g],7,50,path)
        self.assertEqual(n,1)
        with path.open(encoding='utf-8-sig') as f:
            rows=list(csv.DictReader(f,delimiter=';'))
        self.assertEqual(rows[0]['approve'],'')
        self.assertEqual(rows[0]['url'],'https://vk.com/wall-101_11')
        self.assertEqual(len(rows),1)
        with self.assertRaises(ValueError):
            u.export_pair_review(self.db,[self.g],7,50,path)

    def test_approval_requires_minimum_count(self):
        self.add(11)
        path=self.csv([11],approve='')
        with self.assertRaisesRegex(ValueError,'утвердить'):
            u.load_approved_pairs(self.db,[self.g],7,path,1)
        self.assertEqual(u.load_approved_pairs(self.db,[self.g],7,self.csv([11]),1),[(101,11)])

    def test_unknown_or_browser_only_post_is_rejected(self):
        self.add(11,api_provenance=False)
        with self.assertRaisesRegex(ValueError,'подтверждённой даты VK API'):
            u.load_approved_pairs(self.db,[self.g],7,self.csv([11]),1)
        self.add(12,seconds=8*86400)
        with self.assertRaisesRegex(ValueError,'подтверждённой даты VK API'):
            u.load_approved_pairs(self.db,[self.g],7,self.csv([12]),1)

    def test_duplicate_approval_rejected(self):
        self.add(11)
        with self.assertRaisesRegex(ValueError,'Повторно утверждена'):
            u.load_approved_pairs(self.db,[self.g],7,self.csv([11,11]),1)

    def test_changed_manifest_is_rejected_after_campaign_start(self):
        self.add(11); self.add(12); self.add(13)
        campaign=u.start_pair_campaign(self.db,'approved_pairs',2,[(101,11),(101,12)])
        self.assertEqual(campaign['target_each'],2)
        with self.assertRaisesRegex(ValueError,'изменились'):
            u.start_pair_campaign(self.db,'approved_pairs',2,[(101,11),(101,13)])
        with self.assertRaisesRegex(ValueError,'изменились'):
            u.start_pair_campaign(self.db,'approved_pairs',1,[(101,11),(101,12)])
        again=u.start_pair_campaign(self.db,'approved_pairs',2,[(101,11),(101,12)])
        self.assertEqual(again['baseline_action_id'],campaign['baseline_action_id'])

    def test_existing_attempts_not_credited_to_new_campaign(self):
        self.add(11)
        self.db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                        (101,11,'like',engine.moment()))
        self.db.commit()
        campaign=u.start_pair_campaign(self.db,'approved_pairs',1,[(101,11)])
        engine.status_write(self.db,101,11,'like','verified','existing from earlier campaign')
        done=u.paired_progress(self.db,campaign)
        self.assertEqual(done['like'],0)
        self.assertEqual(done['repost'],0)
        self.db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                        (101,11,'repost',engine.moment()))
        self.db.commit()
        engine.status_write(self.db,101,11,'repost','verified','confirmed')
        done=u.paired_progress(self.db,campaign)
        self.assertEqual(done['like'],0)
        self.assertEqual(done['repost'],1)

    def test_like_repost_limited_and_resumable(self):
        self.add(11); self.add(12)
        camp=u.start_pair_campaign(self.db,'approved_pairs',2,[(101,11),(101,12)])
        calls=[]
        def fake_process(page,db,gid,row,pause,safety,actions,already_open):
            self.assertTrue(already_open)
            for action in ('like','repost'):
                if action in actions and row[f'{action}_status']=='pending':
                    safety.reserve(row['post_id'],action)
                    engine.status_write(db,gid,row['post_id'],action,'verified','test confirmed')
                    calls.append((row['post_id'],action))
            return True
        fake_pw=MagicMock()
        fake_ctx=fake_pw.chromium.launch_persistent_context.return_value
        fake_ctx.pages=[MagicMock()]
        fake_manager=MagicMock()
        fake_manager.__enter__.return_value=fake_pw
        fake_manager.__exit__.return_value=False
        date=lambda page,gid,pid:{'publishedAt':self.now-100,'exact':True}
        with patch.object(u,'sync_playwright',return_value=fake_manager), \
             patch.object(engine,'go_to_post'), \
             patch.object(multi,'exact_post_date_info',side_effect=date), \
             patch.object(engine,'process_one',side_effect=fake_process), \
             patch.object(engine,'configure_navigation_pacing'), \
             patch.object(engine,'watch_rate_limit_responses'), \
             patch.object(engine,'require_platform_unlocked'), \
             patch.object(engine,'clear_post_access'), \
             redirect_stdout(io.StringIO()):
            u.run_approved_pairs(self.db,self.cfg,[self.g],camp,max_posts=2,pause=20)
            progress=u.paired_progress(self.db,camp)
            self.assertEqual((progress['like'],progress['repost']),(2,1))
            self.assertIsNone(self.db.execute('SELECT completed_at FROM pair_experiments').fetchone()[0])
            u.run_approved_pairs(self.db,self.cfg,[self.g],camp,max_posts=2,pause=20)
        # Second run respects rolling 24h budget, but per-run repost counter resets.
        progress=u.paired_progress(self.db,camp)
        self.assertEqual((progress['like'],progress['repost']),(2,2))
        self.assertEqual(calls,[(11,'like'),(11,'repost'),(12,'like'),(12,'repost')])
        self.assertIsNotNone(self.db.execute('SELECT completed_at FROM pair_experiments').fetchone()[0])

    def test_api_date_conflict_prevents_click(self):
        self.add(11)
        camp=u.start_pair_campaign(self.db,'approved_pairs',1,[(101,11)])
        fake_pw=MagicMock()
        fake_ctx=fake_pw.chromium.launch_persistent_context.return_value
        fake_ctx.pages=[MagicMock()]
        fake_manager=MagicMock()
        fake_manager.__enter__.return_value=fake_pw
        fake_manager.__exit__.return_value=False
        with patch.object(u,'sync_playwright',return_value=fake_manager), \
             patch.object(engine,'go_to_post'), \
             patch.object(multi,'exact_post_date_info',return_value={'publishedAt':self.now-200}), \
             patch.object(engine,'process_one') as action, \
             patch.object(engine,'configure_navigation_pacing'), \
             patch.object(engine,'watch_rate_limit_responses'), \
             patch.object(engine,'require_platform_unlocked'), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(engine.StopRun):
                u.run_approved_pairs(self.db,self.cfg,[self.g],camp,max_posts=1,pause=20)
        action.assert_not_called()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)


if __name__=='__main__':
    unittest.main()

class CampaignSafetyMigrations(unittest.TestCase):
    def test_expired_or_hidden_approved_member_does_not_block_resuming_other_members(self):
        with tempfile.TemporaryDirectory() as root:
            db=engine.db_open(Path(root)/'state.sqlite')
            try:
                multi.init_dates(db); api.init_api(db)
                now=int(datetime.now(timezone.utc).timestamp())
                g={'id':101,'name':'ТУ','enabled':True,'like_enabled':True,'repost_enabled':True}
                for pid,delta in [(11,100),(12,200)]:
                    engine.add_posts(db,101,[pid]); multi.save_date(db,101,pid,now-delta,api.API_SOURCE,True)
                    db.execute('INSERT INTO api_seen_posts(group_id,post_id,published_at,seen_at,valid) VALUES(?,?,?,?,1)',
                               (101,pid,now-delta,engine.moment()))
                db.commit()
                parent=ApprovedPairsTests()
                parent.root=Path(root)
                parent.now=now
                path=parent.csv([11,12])
                data=u.load_approved_pairs(db,[g],7,path,2)
                camp=u.start_pair_campaign(db,'approved_pairs',2,data)
                engine.record_post_access(db,101,11,'hidden','This post is hidden')
                # Campaign can be resumed from an *unchanged* approval file.
                approved=u.load_approved_pairs(db,[g],7,path,2,enforce_fresh=False)
                self.assertEqual(u.start_pair_campaign(db,'approved_pairs',2,approved)['name'],camp['name'])
                pending=u.pair_pending_rows(db,[g],7,camp)
                self.assertEqual([r['post_id'] for r in pending],[12])
                with self.assertRaisesRegex(ValueError,'подтверждённой даты VK API'):
                    u.load_approved_pairs(db,[g],7,path,2,enforce_fresh=True)
            finally:
                db.close()
