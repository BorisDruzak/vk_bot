"""Pre-batch reads and local overlays: temporary DBs, fake VK transport only."""
import tempfile
import unittest
from contextlib import closing, nullcontext
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from pathlib import Path

import vk_batch as engine
import vk_community as community
import vk_api_read as api
import vk_groups as groups
import vk_scheduler_state as ledger


class LocalActivityTests(unittest.TestCase):
    def test_comments_migration_preserves_old_snapshot_and_attempts(self):
        with tempfile.TemporaryDirectory() as tmp,closing(engine.db_open(Path(tmp)/'old.sqlite3')) as db:
            db.execute('''CREATE TABLE api_post_engagement(group_id INTEGER,post_id INTEGER,
                like_count INTEGER,repost_count INTEGER,observed_at TEXT,PRIMARY KEY(group_id,post_id))''')
            db.execute("INSERT INTO api_post_engagement VALUES(1,2,3,4,'2026-10-22T04:00:00+00:00')");db.commit()
            api.init_api(db);api.init_api(db)
            row=db.execute('SELECT * FROM api_post_engagement').fetchone()
            self.assertEqual((row['like_count'],row['repost_count'],row['comment_count']),(3,4,None))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)

    def test_new_actions_are_added_and_pre_snapshot_actions_not_double_counted(self):
        import vk_batch_engagement as engagement
        row={'group_id':1,'post_id':2,'like_count':10,'repost_count':20,
             'comment_count':3,'observed_at':'2026-10-22T04:00:00+00:00'}
        own={(1,2):{'like':['2026-10-22T03:59:00+00:00','2026-10-22T04:00:01+00:00'],
                    'repost':['2026-10-22T04:00:02+00:00']}}
        result=engagement.effective_counts(row,own)
        self.assertEqual((result['like_count'],result['repost_count'],result['comment_count']),(11,21,3))
        self.assertEqual((result['raw_like_count'],result['own_like_delta']),(10,1))
        own[(1,2)]['like']=['2026-10-22T03:59:00+00:00']*2
        row['like_count']=0
        self.assertEqual(engagement.effective_counts(row,own)['like_count'],2)
        row['like_count']=None
        self.assertIsNone(engagement.effective_counts(row,own)['like_count'])

    def test_verified_actions_both_accounts_and_destinations_count_once(self):
        import vk_batch_engagement as engagement
        with tempfile.TemporaryDirectory() as tmp:
            paths={a:Path(tmp)/f'{a}.sqlite3' for a in (1,2)}
            for a,path in paths.items():
                with closing(engine.db_open(path)) as db:
                    community.init_state(db,1000+a);engine.add_posts(db,1,[2])
                    db.execute("UPDATE posts SET like_status='verified',repost_status='verified'")
                    db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(1,2,'like','2026-10-22T04:00:01+00:00')")
                    # Two observations of the same actor's like do not mean two likes.
                    db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(1,2,'like','2026-10-22T04:00:02+00:00')")
                    db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(1,2,'repost','2026-10-22T04:00:03+00:00')")
                    run=community.begin_run(db,99,0,1,1)
                    attempt=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(1,2,'repost','2026-10-22T04:00:04+00:00')").lastrowid
                    db.execute('INSERT INTO community_run_attempts VALUES(?,?)',(run,attempt))
                    community.write_state(db,99,1,2,'verified',target_post_id=1)
                    community.write_state(db,98,1,2,'uncertain')
                    db.commit()
            own=engagement.local_activity(paths)[(1,2)]
            self.assertEqual((len(own['like']),len(own['repost'])),(2,4))


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=datetime(2026,10,22,5,0,tzinfo=timezone.utc)
        stamp_patch=patch.object(engine,'moment',side_effect=lambda:self.now.isoformat(timespec='seconds'))
        stamp_patch.start();self.addCleanup(stamp_patch.stop)
        self.paths={a:self.root/f'{a}.sqlite3' for a in (1,2)}
        for path in self.paths.values():
            with closing(engine.db_open(path)) as db:
                groups.init_dates(db);api.init_api(db)
        self.state=ledger.State(self.root/'schedule.sqlite3',self.paths);self.addCleanup(self.state.close)
        self.groups=[{'id':101,'enabled':True,'like_enabled':True,'repost_enabled':True}]
        self.stamp=int(self.now.timestamp())-10
        with closing(engine.db_open(self.paths[1])) as db:
            engine.add_posts(db,101,[11,12])
            for pid,likes in ((11,0),(12,10)):
                groups.save_date(db,101,pid,self.stamp,api.API_SOURCE,True)
                db.execute('INSERT INTO api_seen_posts VALUES(?,?,?,?,1)',(101,pid,self.stamp,engine.moment()))
                db.execute('''INSERT INTO api_post_engagement
                    (group_id,post_id,like_count,repost_count,comment_count,observed_at) VALUES(101,?,?,0,0,?)''',
                    (pid,likes,(self.now-timedelta(hours=1)).isoformat()))
            db.commit()
        self.calls=[];self.sleeps=[]

    def refresh(self,transport,**kw):
        import vk_batch_engagement as engagement
        def invoke(params):
            self.calls.append(params.copy());return transport(params)
        with patch.object(api,'configured_token',return_value='fake-test-token'),\
             patch.object(engagement.app,'exclusive_run',return_value=nullcontext()):
            return engagement.refresh(self.state,self.paths[1],self.groups,
                {'daily_requests':2000,'pages_per_group':10,'pause_seconds':20},
                clock=lambda:self.now,transport=invoke,sleeper=self.sleeps.append,**kw)

    def payload(self,missing_comments=False):
        items=[dict(owner_id=-101,id=11,date=self.stamp,likes={'count':0},reposts={'count':0},comments={'count':10}),
               dict(owner_id=-101,id=12,date=self.stamp,likes={'count':1},reposts={'count':0},comments={'count':0})]
        if missing_comments:items[0].pop('comments')
        return 200,{'response':{'count':2,'items':items}}

    def test_fresh_counts_and_comments_replace_stale_order_before_selection(self):
        report=self.refresh(lambda p:self.payload())
        self.assertEqual(report['state'],'ready')
        self.assertEqual(report['pool'],{(101,11),(101,12)})
        rows=self.state.candidates(self.now,1,'like',0,self.paths[1],self.groups)
        self.assertEqual([r['post_id'] for r in rows],[12,11])
        self.assertEqual(self.calls[0]['owner_id'],-101)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM scheduler_jobs').fetchone()[0],0)
        with self.state.account_db(1) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)

    def test_known_canonical_alias_of_uncertain_community_is_not_reselected(self):
        with closing(engine.db_open(self.paths[1])) as db:
            community.init_state(db,1001)
            community.remember_reference(db,101,11,-303,4)
            community.remember_reference(db,101,12,-303,4)
            community.write_state(db,99,101,11,'uncertain')
        self.assertEqual(self.state.candidates(self.now,1,'community',99,self.paths[1],self.groups),[])
        self.assertEqual(len(self.state.candidates(self.now,1,'community',98,self.paths[1],self.groups)),2)
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute('UPDATE community_source_refs SET canonical_group_id=303 WHERE post_id=12');db.commit()
        self.assertEqual([r['post_id'] for r in self.state.candidates(self.now,1,'community',99,self.paths[1],self.groups)],[12])

    def test_unknown_comments_are_excluded_from_fresh_pool(self):
        report=self.refresh(lambda p:self.payload(missing_comments=True))
        self.assertEqual(report['pool'],{(101,12)})
        self.assertEqual(report['report']['unknown_counts'],1)

    def test_missing_today_post_does_not_search_pages_older_than_day_boundary(self):
        def transport(params):
            offset=params['offset']
            items=[dict(owner_id=-101,id=1000+offset+i,date=self.stamp-86400-offset-i,
                likes={'count':0},reposts={'count':0},comments={'count':0}) for i in range(100)]
            if offset==0:items[0]=dict(owner_id=-101,id=11,date=self.stamp,
                likes={'count':0},reposts={'count':0},comments={'count':0})
            return 200,{'response':{'count':5000,'items':items}}
        report=self.refresh(transport)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(report['pool'],{(101,11)})
        self.assertEqual(report['report']['missing_posts'],1)

    def test_old_pinned_post_does_not_end_today_refresh(self):
        def transport(params):
            if params['offset']==0:
                items=[dict(owner_id=-101,id=1000+i,date=self.stamp-i,
                    likes={'count':0},reposts={'count':0},comments={'count':0}) for i in range(100)]
                items[0].update(date=self.stamp-86400,is_pinned=1)
            else:
                status,payload=self.payload();payload['response']['count']=102
                return status,payload
            return 200,{'response':{'count':102,'items':items}}
        report=self.refresh(transport)
        self.assertEqual(len(self.calls),2)
        self.assertIn((101,12),report['pool'])

    def test_unordered_page_does_not_prove_old_boundary(self):
        def transport(params):
            if params['offset']:
                status,payload=self.payload();payload['response']['count']=102;return status,payload
            items=[dict(owner_id=-101,id=1000+i,date=self.stamp-86400-i,
                likes={'count':0},reposts={'count':0},comments={'count':0}) for i in range(100)]
            items[40]['date']=self.stamp
            return 200,{'response':{'count':102,'items':items}}
        report=self.refresh(transport)
        self.assertEqual(len(self.calls),2);self.assertIn((101,12),report['pool'])

    def test_full_56_group_refresh_with_approved_read_pacing_fits_between_batches(self):
        import vk_batch_engagement as engagement
        import vk_scheduler as scheduler
        self.groups=[{'id':gid,'enabled':True,'like_enabled':True,'repost_enabled':True} for gid in range(101,157)]
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute('UPDATE api_seen_posts SET valid=0 WHERE post_id=12')
            for gid in range(102,157):
                engine.add_posts(db,gid,[11]);groups.save_date(db,gid,11,self.stamp,api.API_SOURCE,True)
                db.execute('INSERT INTO api_seen_posts VALUES(?,?,?,?,1)',(gid,11,self.stamp,engine.moment()))
            db.commit()
        path=self.root/'automation.json';path.write_text('{"scan":{"pause_seconds":5}}')
        _,settings=scheduler.load_settings(path);started=self.now
        def sleep(seconds):self.now+=timedelta(seconds=seconds)
        def transport(params):
            return 200,{'response':{'count':1,'items':[dict(owner_id=params['owner_id'],id=11,date=self.stamp,
                likes={'count':0},reposts={'count':0},comments={'count':0})]}}
        with patch.object(api,'configured_token',return_value='fake'),patch.object(engagement.app,'exclusive_run',return_value=nullcontext()):
            result=engagement.refresh(self.state,self.paths[1],self.groups,settings,clock=lambda:self.now,transport=transport,sleeper=sleep)
        self.assertEqual(result['state'],'ready');self.assertEqual(len(result['pool']),56)
        self.assertEqual(result['report']['requests'],56)
        self.assertGreaterEqual((self.now-started).total_seconds(),275)
        self.assertLess((self.now-started).total_seconds(),300)

    def test_stop_and_budget_do_not_fall_back_to_stale_counts(self):
        self.state.set('stop_requested',True)
        report=self.refresh(lambda p:self.fail('network after stop'))
        self.assertEqual(report['state'],'stopped');self.assertEqual(self.calls,[])
        self.state.set('stop_requested',False)
        with closing(engine.db_open(self.paths[1])) as db:
            db.executemany('INSERT INTO api_read_attempts(group_id,attempted_at) VALUES(101,?)',[(engine.moment(),)]*2000);db.commit()
        report=self.refresh(lambda p:self.fail('network over budget'))
        self.assertEqual(report['state'],'budget');self.assertEqual(self.calls,[])

    def test_rate_limit_propagates_without_retry_or_actions(self):
        with self.assertRaises(api.VkApiLimited):
            self.refresh(lambda p:(429,{}))
        self.assertEqual(len(self.calls),1)
        with self.state.account_db(1) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM safety_lock').fetchone()[0],1)

    def test_verified_like_from_account_one_reranks_account_two_queue(self):
        self.refresh(lambda p:self.payload())
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute('UPDATE api_post_engagement SET like_count=0,repost_count=0,comment_count=0')
            db.execute('UPDATE api_seen_posts SET published_at=? WHERE post_id=12',(self.stamp-1,))
            db.execute('UPDATE post_publication SET published_at=? WHERE post_id=12',(self.stamp-1,));db.commit()
        before=self.state.candidates(self.now,2,'community',242479129,self.paths[1],self.groups)
        self.assertEqual(before[0]['post_id'],11)
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute("UPDATE posts SET like_status='verified' WHERE post_id=11")
            db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,11,'like',?)",((self.now+timedelta(seconds=2)).isoformat(),));db.commit()
        self.now+=timedelta(seconds=3)
        after=self.state.candidates(self.now,2,'community',242479129,self.paths[1],self.groups)
        self.assertEqual(after[0]['post_id'],12)
        self.assertEqual(after[1]['like_count'],1)

    def test_window_end_during_refresh_never_authorizes_actions(self):
        self.now=self.now.replace(hour=13,minute=59,second=50)
        def transport(params):
            self.now+=timedelta(seconds=11)
            return self.payload()
        result=self.refresh(transport)
        self.assertEqual(result['state'],'stopped')
        self.assertEqual(result['pool'],set())

    def test_missing_sources_and_counts_never_reuse_previous_zero(self):
        status,payload=self.payload()
        payload['response']['items']=payload['response']['items'][1:]
        payload['response']['count']=1
        result=self.refresh(lambda p:(status,payload))
        self.assertEqual(result['pool'],{(101,12)})
        self.assertEqual(result['report']['missing_posts'],1)

    def test_same_second_old_observation_does_not_count_as_a_new_response(self):
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute('UPDATE api_post_engagement SET observed_at=?',(engine.moment(),));db.commit()
        status,payload=self.payload();payload['response']['items']=payload['response']['items'][1:]
        payload['response']['count']=1
        result=self.refresh(lambda p:(status,payload))
        self.assertEqual(result['pool'],{(101,12)})

    def test_account_two_platform_warning_stops_before_api(self):
        with closing(engine.db_open(self.paths[2])) as db:
            engine.lock_on_platform_warning(db,0,'offline warning')
        with self.assertRaises(engine.StopRun):
            self.refresh(lambda p:self.fail('network after warning'))
        self.assertEqual(self.calls,[])

    def test_stop_during_required_api_pause_prevents_network(self):
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute('INSERT INTO read_navigation_pacing VALUES(0,?)',(self.now.timestamp(),));db.commit()
        self.sleeps=type('StoppingSleeper',(),{'append':lambda _self,seconds:self.state.set('stop_requested',True)})()
        result=self.refresh(lambda p:self.fail('network after stop during pause'))
        self.assertEqual(result['state'],'stopped')
        self.assertEqual(self.calls,[])

    def test_counter_refresh_preserves_background_catalog_pagination(self):
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute('''INSERT INTO api_group_scan_progress
                (group_id,last_scanned_at,discovered,status,next_offset,reason)
                VALUES(101,?,100,'partial',100,'more-pages')''',(engine.moment(),))
            db.execute('INSERT INTO api_scan_seen VALUES(101,999)');db.commit()
            before=tuple(db.execute('SELECT * FROM api_group_scan_progress').fetchone())
        self.refresh(lambda p:self.payload())
        with self.state.account_db(1) as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM api_group_scan_progress').fetchone()),before)
            self.assertEqual(db.execute('SELECT * FROM api_scan_seen').fetchall(),[(101,999)])

    def test_continuation_pages_keep_twenty_second_api_pacing(self):
        def transport(params):
            if params['offset']==0:
                items=[dict(owner_id=-101,id=1000+i,date=self.stamp,likes={'count':2},reposts={'count':0},comments={'count':0}) for i in range(100)]
            else:items=self.payload()[1]['response']['items']
            return 200,{'response':{'count':102,'items':items}}
        result=self.refresh(transport)
        self.assertEqual([p['offset'] for p in self.calls],[0,100])
        self.assertEqual(self.sleeps,[20])
        self.assertTrue({(101,11),(101,12)}<=result['pool'])

    def test_already_completed_today_posts_cannot_starve_reserve(self):
        self.stamp-=86400
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute('UPDATE api_seen_posts SET published_at=?',(self.stamp,))
            db.execute('UPDATE post_publication SET published_at=?',(self.stamp,))
            engine.add_posts(db,101,[13]);db.execute("UPDATE posts SET like_status='verified' WHERE post_id=13")
            db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,13,'like',?)",(engine.moment(),));db.commit()
        status,payload=self.payload()
        payload['response']['count']=3
        payload['response']['items'].insert(0,dict(owner_id=-101,id=13,date=int(self.now.timestamp())-1,
            likes={'count':0},reposts={'count':0},comments={'count':0}))
        with patch.object(self.state,'remaining',return_value={(1,'like',0):49}):
            result=self.refresh(lambda p:(status,payload))
        self.assertEqual(result['report']['selected_band'],'old')
        self.assertEqual(result['pool'],{(101,11),(101,12)})

    def test_denied_read_preserves_cursor_and_uses_separate_cooldown(self):
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute('''INSERT INTO api_group_scan_progress
                (group_id,last_scanned_at,discovered,status,next_offset,snapshot_count,scan_cutoff,reason)
                VALUES(101,?,100,'partial',100,300,123,'more-pages')''',(engine.moment(),));db.commit()
            before=tuple(db.execute('SELECT * FROM api_group_scan_progress').fetchone())
        result=self.refresh(lambda p:(200,{'error':{'error_code':7}}))
        self.assertEqual(result['state'],'empty')
        self.assertEqual(len(self.calls),1)
        result=self.refresh(lambda p:self.fail('permission denied group retried immediately'))
        self.assertEqual(result['state'],'empty');self.assertEqual(len(self.calls),1)
        with self.state.account_db(1) as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM api_group_scan_progress').fetchone()),before)

    def pending_retry(self,account=1,pid=11):
        import vk_action_retry as retry
        self.state.set('action_retry_policy',1)
        row={'group_id':101,'post_id':pid,'published_at':self.stamp}
        job=self.state.create_job(self.now,0,account,'like',0,row)
        book=retry.RetryLedger(self.state);book.begin(job,self.now)
        book.before_attempt(job,self.now,0)
        with closing(engine.db_open(self.paths[account])) as db:
            engine.add_posts(db,101,[pid])
            aid=db.execute("INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(101,?,'like',?)",
                           (pid,engine.moment())).lastrowid
            db.execute("UPDATE posts SET like_status='uncertain' WHERE group_id=101 AND post_id=?",(pid,));db.commit()
        book.record_attempt(job,aid);book.fail_pass(job,'like_confirmation_timeout',self.now);book.pending(job,self.now)
        return job

    def test_pending_retry_both_accounts_refresh_without_any_ordinary_candidates_or_quota(self):
        import vk_batch_engagement as engagement
        self.pending_retry(1);self.pending_retry(2)
        with patch.object(self.state,'remaining',return_value={(1,'like',0):0,(2,'like',0):0}):
            self.assertTrue(engagement._eligible(self.state,self.paths[1],self.groups,self.now)[(101,11)]['retry_pending'])
            result=self.refresh(lambda params:self.payload())
        self.assertEqual(result['state'],'ready');self.assertEqual(result['pool'],{(101,11)})
        self.assertEqual(len(self.calls),1)
        for account in (1,2):
            with self.state.account_db(account) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT like_status FROM posts WHERE post_id=11').fetchone()[0],'uncertain')
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM scheduler_action_recovery WHERE state='pending'").fetchone()[0],2)

    def test_pending_retry_unknown_counts_and_budget_never_reuse_stale_pool(self):
        self.pending_retry()
        with patch.object(self.state,'remaining',return_value={(1,'like',0):0}):
            unknown=self.refresh(lambda params:self.payload(missing_comments=True))
            self.assertEqual(unknown['pool'],set());self.assertEqual(unknown['report']['unknown_counts'],1)
            with closing(engine.db_open(self.paths[1])) as db:
                db.executemany('INSERT INTO api_read_attempts(group_id,attempted_at) VALUES(101,?)',[(engine.moment(),)]*2000);db.commit()
            result=self.refresh(lambda params:self.fail('network over API budget'))
        self.assertEqual(result['state'],'budget');self.assertEqual(result['pool'],set())

    def test_old_pending_retry_is_requested_past_new_band_boundary_without_old_ordinary_pool(self):
        old=self.stamp-86400
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute('UPDATE api_seen_posts SET published_at=? WHERE post_id=11',(old,))
            db.execute('UPDATE post_publication SET published_at=? WHERE post_id=11',(old,));db.commit()
        new=self.stamp;self.stamp=old;self.pending_retry();self.stamp=new
        def transport(params):
            if params['offset']==0:
                items=[dict(owner_id=-101,id=1000+i,date=old+1000-i,
                            likes={'count':0},reposts={'count':0},comments={'count':0}) for i in range(100)]
                items[0].update(id=12,date=new)
            else:
                items=[dict(owner_id=-101,id=11,date=old,likes={'count':0},reposts={'count':0},comments={'count':0}),
                       dict(owner_id=-101,id=13,date=old-1,likes={'count':0},reposts={'count':0},comments={'count':0})]
            return 200,{'response':{'count':102,'items':items}}
        with patch.object(self.state,'remaining',return_value={(1,'like',0):49}):result=self.refresh(transport)
        self.assertEqual([p['offset'] for p in self.calls],[0,100])
        self.assertEqual(result['report']['selected_band'],'new')
        self.assertEqual(result['pool'],{(101,11),(101,12)})

    def test_pending_refresh_excludes_disabled_source_actor_and_changed_identity(self):
        import vk_batch_engagement as engagement
        job=self.pending_retry()
        with patch.object(self.state,'remaining',return_value={}):
            with patch.object(self.state,'account_enabled',return_value=False):
                self.assertEqual(engagement._eligible(self.state,self.paths[1],self.groups,self.now),{})
            self.assertEqual(engagement._eligible(self.state,self.paths[1],[dict(self.groups[0],enabled=False)],self.now),{})
            self.state.db.execute('UPDATE scheduler_jobs SET published_at=published_at-1 WHERE id=?',(job,));self.state.db.commit()
            self.assertEqual(engagement._eligible(self.state,self.paths[1],self.groups,self.now),{})

    def test_pre_intent_failed_retry_and_deferred_second_pass_are_read_only_eligible(self):
        import vk_action_retry as retry
        import vk_batch_engagement as engagement
        self.state.set('action_retry_policy',1)
        job=self.state.create_job(self.now,0,1,'like',0,{'group_id':101,'post_id':11,'published_at':self.stamp})
        book=retry.RetryLedger(self.state);book.begin(job,self.now)
        book.fail_pass(job,'action_stopped_browser_open',self.now);book.pending(job,self.now)
        with patch.object(self.state,'remaining',return_value={}):
            for deferred in (False,True):
                if deferred:
                    book.begin_retry(job,self.now);book.defer_unreserved(job,self.now)
                changes=self.state.db.total_changes
                self.assertTrue(engagement._eligible(self.state,self.paths[1],self.groups,self.now)[(101,11)]['retry_pending'])
                self.assertEqual(self.state.db.total_changes,changes)
            result=self.refresh(lambda params:self.payload())
        self.assertEqual(result['pool'],{(101,11)})
        with self.state.account_db(1) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)

    def test_pending_refresh_requires_enabled_policy_current_day_and_valid_publication(self):
        import vk_batch_engagement as engagement
        self.pending_retry()
        with patch.object(self.state,'remaining',return_value={}):
            self.state.set('action_retry_policy',0)
            self.assertEqual(engagement._eligible(self.state,self.paths[1],self.groups,self.now),{})
            self.state.set('action_retry_policy',1)
            self.assertEqual(engagement._eligible(self.state,self.paths[1],self.groups,self.now+timedelta(days=1)),{})
            with closing(engine.db_open(self.paths[1])) as db:
                db.execute('UPDATE api_seen_posts SET valid=0 WHERE post_id=11');db.commit()
            self.assertEqual(engagement._eligible(self.state,self.paths[1],self.groups,self.now),{})

    def test_today_pending_retry_does_not_hide_ordinary_reserve_band(self):
        self.pending_retry()
        old=self.stamp-86400
        with closing(engine.db_open(self.paths[1])) as db:
            db.execute('UPDATE api_seen_posts SET published_at=? WHERE post_id=12',(old,))
            db.execute('UPDATE post_publication SET published_at=? WHERE post_id=12',(old,));db.commit()
        status,payload=self.payload();payload['response']['items'][1]['date']=old
        with patch.object(self.state,'remaining',return_value={(1,'like',0):49}):
            result=self.refresh(lambda params:(status,payload))
        self.assertEqual(result['report']['selected_band'],'old')
        self.assertEqual(result['pool'],{(101,11),(101,12)})

    def test_pending_only_today_pool_still_readable_without_ordinary_quota(self):
        self.pending_retry()
        with patch.object(self.state,'remaining',return_value={}):result=self.refresh(lambda params:self.payload())
        self.assertEqual(result['pool'],{(101,11)})
        self.assertEqual(result['report']['selected_band'],'new')


if __name__=='__main__':unittest.main()
