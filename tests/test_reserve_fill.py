"""Mixed-age batches on temporary SQLite and fake HTTP/action boundaries only."""
import json
import unittest
from contextlib import closing, nullcontext
from datetime import timedelta
from unittest.mock import patch

import vk_api_read as api
import vk_batch as engine
import vk_batch_engagement as engagement
import vk_groups as groups
import vk_scheduler as scheduler
from tests import test_scheduler_runner as fixtures


class ReserveFillTests(unittest.TestCase):
    setUp = fixtures.RunnerTests.setUp

    def setup_worker(self, *, limit=50):
        self.paths[5] = self.root/'five.sqlite3'
        with closing(engine.db_open(self.paths[5])) as db:
            api.init_api(db); groups.init_dates(db)
        self.state.paths = self.paths.copy()
        self.state.config['accounts'] = {
            str(a):dict(likes=limit, personal_reposts=0, community_reposts=0, communities=[])
            for a in self.paths}
        self.cfg['groups'].append(dict(id=102, enabled=True, like_enabled=True, repost_enabled=True))
        self.dates = {(101,11):int(self.now.timestamp())-10,
                      (101,12):int(self.now.timestamp())-86400,
                      (102,21):int(self.now.timestamp())-86410}
        with closing(engine.db_open(self.paths[1])) as db:
            for (gid,pid),stamp in self.dates.items():
                engagement._observe_page(db,gid,[self.item(gid,pid,0)],int(self.now.timestamp()),
                    observed_at=(self.now-timedelta(hours=1)).isoformat())
        self.worker = scheduler.Worker(self.state,self.paths[1],self.cfg,clock=lambda:self.now,
            scan_settings=dict(daily_requests=5000,pages_per_group=10,pause_seconds=5))
        self.sent=[]; self.reads=[]; self.sleeps=[]; self.on_read=None; self.on_action=None
        self.counts={(101,11):0,(101,12):99,(102,21):1}
        self.unknown=False; self.budget=False
        # Preserve an actual saved plan, including its exact serialized bytes.
        self.plan=self.state.day_plan(self.now)
        # slot0 in these runner tests is claimed now, not an hour after the
        # randomized plan's real slot0. Keep a valid future next-slot boundary.
        self.state.db.execute('UPDATE schedule_days SET slots=?',
            (json.dumps([self.now.isoformat(),(self.now+timedelta(seconds=585)).isoformat()]),));self.state.db.commit()
        self.plan_before=[tuple(r) for r in self.state.db.execute('SELECT * FROM schedule_days')]

    def item(self,gid,pid,likes):
        return dict(owner_id=-gid,id=pid,date=self.dates[(gid,pid)],
                    likes={'count':likes},reposts={'count':0},comments={'count':0})

    def transport(self,params):
        self.reads.append((dict(params),self.now))
        if self.on_read:
            value=self.on_read(params)
            if value is not None:return value
        gid=-params['owner_id']
        items=[self.item(g,p,self.counts[(g,p)]) for g,p in self.dates if g==gid]
        if self.unknown:
            for item in items:
                if item['date']<int(self.now.timestamp())-1000:item.pop('comments')
        return 200,{'response':dict(count=len(items),items=items)}

    def sleep(self,seconds):
        self.sleeps.append(seconds);self.now+=timedelta(seconds=seconds)

    def execute(self,account,kind,row,*,before_attempt,record_attempt,before_click,**kw):
        before_attempt()
        with closing(engine.db_open(self.paths[account])) as db:
            engine.add_posts(db,row['group_id'],[row['post_id']])
            aid=db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                (row['group_id'],row['post_id'],'like',self.now.isoformat())).lastrowid;db.commit()
            record_attempt(aid);before_click()
            db.execute("UPDATE posts SET like_status='verified' WHERE group_id=? AND post_id=?",
                       (row['group_id'],row['post_id']));db.commit()
        self.sent.append((account,row['group_id'],row['post_id'],self.now))
        self.sleep(20)
        if self.on_action:self.on_action()
        return dict(status='verified',attempt_id=aid)

    def run_batch(self):
        real_refresh=engagement.refresh
        def refresh(*args,**kw):
            return real_refresh(*args,**kw,transport=self.transport,sleeper=self.sleep)
        with patch.object(api,'configured_token',return_value='offline'), \
             patch.object(engagement.app,'exclusive_run',return_value=nullcontext()), \
             patch.object(engagement,'refresh',side_effect=refresh), \
             patch.object(scheduler.actions,'execute_action',side_effect=self.execute):
            result=self.worker.run_batch(0)
        for original in self.plan_before:
            self.assertEqual(tuple(self.state.db.execute('SELECT * FROM schedule_days WHERE day=?',(original[0],)).fetchone()),original)
        return result

    def test_three_new_likes_then_fresh_reserve_fourth_in_same_claim(self):
        self.setup_worker();self.state.set('action_retry_policy',1)
        result=self.run_batch()
        self.assertEqual(result['attempted'],4)
        self.assertEqual(result['phase'],'batch_complete')
        self.assertEqual([r[2] for r in self.sent],[11,11,11,21])
        self.assertEqual(len(self.reads),3)  # one new-layer read, then both reserve groups
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_batches').fetchone()[0],1)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],4)
        evidence=json.loads(self.state.db.execute('SELECT snapshot FROM scheduler_priority_evidence ORDER BY job_id DESC LIMIT 1').fetchone()[0])
        self.assertGreater(evidence['refresh_started_at'],self.sent[2][3].isoformat())
        self.assertGreaterEqual(evidence['observed_at'],evidence['refresh_started_at'])
        self.assertEqual(evidence['raw_like_count'],1)  # reserve old zero was replaced
        self.assertEqual(self.sleeps.count(20),4)
        for earlier,later in zip(self.sent,self.sent[1:]):
            self.assertGreaterEqual((later[3]-earlier[3]).total_seconds(),20)
        last=self.state.get('last_action_batch')
        self.assertEqual(last['slot'],0);self.assertEqual(last['at'],self.sent[0][3].isoformat())
        self.assertFalse(self.state.claim_batch(self.now,0))

    def test_reserve_refresh_is_bounded_once_when_counts_are_unknown(self):
        self.setup_worker();self.unknown=True
        result=self.run_batch()
        self.assertEqual(result['attempted'],3);self.assertEqual(len(self.reads),3)
        self.assertEqual(len(self.sent),3)

    def test_no_extra_read_when_batch_already_full(self):
        self.setup_worker()
        self.dates[(101,13)]=self.dates[(101,11)]-1;self.counts[(101,13)]=10
        result=self.run_batch()
        self.assertEqual(result['attempted'],4);self.assertEqual(len(self.reads),1)

    def test_retry_uses_fourth_place_before_any_reserve_refresh(self):
        self.setup_worker();self.state.set('action_retry_policy',1)
        execute=self.execute;failed=False
        def retry_once(account,kind,row,**kw):
            nonlocal failed
            result=execute(account,kind,row,**kw)
            if not failed:
                failed=True
                with closing(engine.db_open(self.paths[account])) as db:
                    db.execute("UPDATE posts SET like_status='uncertain' WHERE group_id=? AND post_id=?",
                               (row['group_id'],row['post_id']));db.commit()
                raise engine.ActionConfirmationError('like',{'liked':False,'main_post_liked':False})
            return result
        self.execute=retry_once
        result=self.run_batch()
        self.assertEqual(result['attempted'],4);self.assertEqual(len(self.sent),4)
        self.assertEqual([r[2] for r in self.sent],[11,11,11,11])
        self.assertEqual(len(self.reads),1)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_action_passes WHERE job_id=1').fetchone()[0],2)

    def test_quota_exhaustion_does_not_refresh_or_send_reserve(self):
        self.setup_worker(limit=1)
        result=self.run_batch()
        self.assertEqual(result['attempted'],3);self.assertEqual(len(self.reads),1)
        self.assertFalse(any(self.state.remaining(self.now).values()))

    def test_partial_reserve_budget_failure_never_uses_old_or_partial_pool(self):
        self.setup_worker();self.worker.scan_settings['daily_requests']=2
        result=self.run_batch()
        self.assertEqual(result['phase'],'reserve_counter_refresh_budget')
        self.assertEqual(result['attempted'],3);self.assertEqual(len(self.sent),3)
        self.assertEqual(len(self.reads),2)

    def test_rate_limit_on_reserve_stops_without_new_action_or_retry(self):
        self.setup_worker()
        self.on_read=lambda p:(429,{}) if len(self.reads)>1 else None
        result=self.run_batch()
        self.assertEqual(result['phase'],'attention');self.assertEqual(len(self.sent),3)
        self.assertEqual(result['attempted'],3);self.assertEqual(len(self.reads),2)
        self.assertEqual(self.state.get('paused'),'VkApiLimited')

    def test_stop_during_reserve_read_cancels_remaining_action(self):
        self.setup_worker()
        def stop(p):
            if len(self.reads)>1:self.state.set('stop_requested',True)
        self.on_read=stop
        result=self.run_batch()
        self.assertEqual(result['phase'],'reserve_counter_refresh_stopped')
        self.assertEqual(result['attempted'],3);self.assertEqual(len(self.sent),3)

    def test_window_end_during_reserve_read_cancels_remaining_action(self):
        self.setup_worker()
        def close_window(p):
            if len(self.reads)>1:self.now=self.now.replace(hour=14)
        self.on_read=close_window
        result=self.run_batch()
        self.assertEqual(result['phase'],'reserve_counter_refresh_stopped');self.assertEqual(len(self.sent),3)

    def test_day_jump_after_fresh_actions_never_refreshes_or_creates_next_day_job(self):
        self.setup_worker()
        def jump():
            if len(self.sent)==3:self.now+=timedelta(days=1)
        self.on_action=jump
        self.run_batch()
        self.assertEqual(len(self.sent),3);self.assertEqual(len(self.reads),1)
        batch=self.state.db.execute('SELECT * FROM scheduler_batches WHERE day=?',(self.plan_before[0][0],)).fetchone()
        self.assertIsNotNone(batch['finished_at'])
        self.assertEqual(batch['status'],'batch_day_changed')

    def test_new_post_discovered_during_reserve_refresh_keeps_priority(self):
        self.setup_worker()
        def discover(p):
            if len(self.reads)>1:
                self.dates[(102,22)]=int(self.now.timestamp())-1;self.counts[(102,22)]=999
        self.on_read=discover
        result=self.run_batch()
        self.assertEqual(result['attempted'],4)
        self.assertEqual(self.sent[-1][2],22)

    def test_missing_fresh_source_does_not_starve_refreshed_reserve(self):
        self.setup_worker()
        def missing(p):
            gid=-p['owner_id']
            return 200,{'response':dict(count=1,items=[self.item(gid,12 if gid==101 else 21,2)])}
        self.on_read=missing
        result=self.run_batch()
        self.assertEqual(result['attempted'],4)
        self.assertNotIn(11,[r[2] for r in self.sent])
        self.assertEqual(len(self.reads),3)

    def test_existing_safety_lock_during_reserve_read_is_not_cleared(self):
        self.setup_worker()
        def warning(p):
            if len(self.reads)>1:
                with closing(engine.db_open(self.paths[2])) as db:
                    engine.lock_on_platform_warning(db,0,'offline test')
        self.on_read=warning
        result=self.run_batch()
        self.assertEqual(result['phase'],'attention');self.assertEqual(len(self.sent),3)
        self.assertEqual(self.state.get('paused'),'StopRun')

    def test_reserve_read_crossing_final_thirty_second_margin_cannot_reserve(self):
        self.setup_worker()
        def near_close(p):
            if len(self.reads)>1:self.now=self.now.replace(hour=13,minute=59,second=40)
        self.on_read=near_close
        self.run_batch()
        self.assertEqual(len(self.sent),3)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],3)


if __name__=='__main__':unittest.main()
