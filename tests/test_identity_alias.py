"""Own-header alias proof stays bounded, contextual and ephemeral; no real HTTP."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import gc
import importlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import weakref

import vk_accounts as accounts
import vk_api_read as api
import vk_batch as engine
import vk_unified as app


class Header:
    def __init__(self,page):self.page=page
    def count(self):return self.page.header_count
    @property
    def first(self):return self
    def get_attribute(self,name,timeout=0):
        if name!='href':raise AssertionError('Only href may be read')
        return self.page.href


class Page:
    def __init__(self,href,url='https://vk.com/feed'):
        self.href=href;self.url=url;self.header_count=1;self.closed=False;self.context=None
    def is_closed(self):return self.closed
    def locator(self,selector):
        if selector!=accounts.PROFILE_LINK:raise AssertionError('Only exact own header may be read')
        return Header(self)
    def wait_for_timeout(self,n):pass


class Context:
    def __init__(self,*hrefs):
        self.pages=[Page(href) for href in hrefs]
        for page in self.pages:page.context=self


class IdentityAliasTests(unittest.TestCase):
    def setUp(self):
        self.identity=importlib.import_module('vk_identity_alias')
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.db_path=self.root/'vk_batch.sqlite3'
        with closing(engine.db_open(self.db_path)) as db:api.init_api(db)
        self.actor7=self.root/'vk_batch_account_7.sqlite3';self.actor7.write_bytes(b'actor7 history sentinel')
        self.calls=[]
        root_patch=patch.object(accounts,'ROOT',self.root);root_patch.start();self.addCleanup(root_patch.stop)
        token_patch=patch.object(api,'configured_token',return_value='fake-test-key');token_patch.start();self.addCleanup(token_patch.stop)
        transport=patch.object(api,'bounded_transport',side_effect=self.transport);transport.start();self.addCleanup(transport.stop)

    def transport(self,params,**kwargs):
        self.calls.append((params.copy(),kwargs))
        return 200,{'response':[{'id':202,'screen_name':params['user_ids']}]}

    def count(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute('SELECT COUNT(*) FROM api_read_attempts').fetchone()[0]

    def prepare(self,context,**kwargs):return accounts.prepare_context_identity(context,**kwargs)

    def test_numeric_header_fastpath_never_needs_key_database_or_primary_lock(self):
        self.db_path.unlink()
        with patch.object(api,'configured_token',side_effect=AssertionError('No key needed')),\
             patch.object(app,'exclusive_run',side_effect=AssertionError('No lock needed')):
            context=Context('/id202')
            self.assertEqual(self.prepare(context),202)
            self.assertEqual(accounts.inspect_header_identity(context.pages[0]),202)
        self.assertEqual(self.calls,[])

    def test_exact_alias_resolves_once_under_primary_lock_into_only_memory(self):
        context=Context('/a')
        self.assertIsNone(accounts.inspect_header_identity(context.pages[0]))
        lock_seen=[]
        def transport(params,**kwargs):
            lock_seen.append((self.root/'.vk-unified-running.lock').exists())
            return self.transport(params,**kwargs)
        (self.root/'.vk-public-data.json').write_text('{"version":1,"enabled":true}',encoding='utf-8')
        with patch.object(api,'bounded_transport',side_effect=transport):
            self.assertEqual(self.prepare(context),202)
            self.assertEqual(self.prepare(context),202)
        self.assertEqual(lock_seen,[True]);self.assertEqual(self.count(),1)
        self.assertEqual(self.calls[0][0]['user_ids'],'a')
        self.assertEqual(self.calls[0][0]['fields'],'screen_name')
        self.assertEqual(self.calls[0][0]['_read_method'],'users.get')
        self.assertEqual(self.calls[0][1]['timeout'],20)
        self.assertEqual(accounts.inspect_context(context,attempts=1),202)
        self.assertFalse((self.root/'vk_public_data.sqlite3').exists())
        self.assertEqual(self.actor7.read_bytes(),b'actor7 history sentinel')
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute('SELECT group_id FROM api_read_attempts').fetchall(),[(0,)])

    def test_strict_alias_uri_rejects_routes_and_ambiguous_authorities_without_http(self):
        for href in ('https://evil.test/a','http://vk.com/a','//vk.com/a','https://u@vk.com/a',
                'https://vk.com:443/a','https://vk.com:/a','https://vk.com/\na','a','/a?x=1','/a#x','/a/b','/%61','/feed','/club123',
                '/public1','/app1','/id0','/menu','/account','/profile','/a b','/а',' /a','/a/','javascript:a','/a\\b'):
            with self.subTest(href=href):
                self.assertIsNone(self.prepare(Context(href)))
        self.assertEqual(self.calls,[]);self.assertEqual(self.count(),0)

    def test_trusted_https_alias_hosts_and_one_letter_are_supported(self):
        for host in sorted(accounts.VK_HOSTS):
            with self.subTest(host=host):
                context=Context('https://'+host+'/a')
                # Suppress real pacing in this independent context.
                with patch.object(self.identity.time,'sleep'):
                    self.assertEqual(self.prepare(context),202)

    def test_conflicting_aliases_numeric_ids_and_duplicate_header_fail_closed(self):
        for context in (Context('/a','/b'),Context('/id202','/id203')):
            with self.subTest(context=context),self.assertRaises(accounts.AccountError):self.prepare(context)
        context=Context('/a');context.pages[0].header_count=2
        with self.assertRaises(accounts.AccountError):self.prepare(context)
        self.assertEqual(self.calls,[])

    def test_alias_must_resolve_to_same_numeric_identity_in_other_tabs(self):
        context=Context('/a','/id203')
        with self.assertRaises(accounts.AccountError):self.prepare(context)
        self.assertIsNone(accounts.inspect_header_identity(context.pages[0]))

    def test_header_change_after_pacing_cannot_reserve_or_send_http(self):
        context=Context('/a');now=datetime.now(timezone.utc).timestamp()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute('INSERT INTO read_navigation_pacing VALUES(0,?)',(now,));db.commit()
        def sleep(seconds):context.pages[0].href='/b'
        with patch.object(self.identity.time,'time',return_value=now),patch.object(self.identity.time,'sleep',side_effect=sleep):
            with self.assertRaises(accounts.AccountError):self.prepare(context)
        self.assertEqual(self.calls,[]);self.assertEqual(self.count(),0)

    def test_header_change_during_http_discards_response_and_prior_cache(self):
        context=Context('/a')
        def transport(params,**kwargs):
            context.pages[0].href='/b'
            return self.transport(params,**kwargs)
        with patch.object(api,'bounded_transport',side_effect=transport):
            with self.assertRaises(accounts.AccountError):self.prepare(context)
        context.pages[0].href='/a'
        self.assertIsNone(accounts.inspect_header_identity(context.pages[0]))
        self.assertEqual(self.count(),1)

    def test_prepared_context_and_ttl_cannot_be_reused_by_other_contexts_or_changed_tabs(self):
        context=Context('/a');other=Context('/a')
        with patch.object(self.identity.time,'monotonic',return_value=10):self.assertEqual(self.prepare(context),202)
        with patch.object(self.identity.time,'monotonic',return_value=11):
            self.assertEqual(accounts.inspect_header_identity(context.pages[0]),202)
            self.assertIsNone(accounts.inspect_header_identity(other.pages[0]))
        with patch.object(self.identity.time,'monotonic',return_value=311):
            self.assertIsNone(accounts.inspect_header_identity(context.pages[0]))
        self.assertEqual(self.count(),1)

    def test_changed_cross_tab_alias_does_not_fall_back_to_numeric_tab(self):
        context=Context('/a','/id202');self.assertEqual(self.prepare(context),202)
        context.pages[0].href='/b'
        self.assertIsNone(accounts.inspect_context(context,attempts=1))
        self.assertEqual(self.count(),1)

    def test_alias_never_ignores_own_headers_on_untrusted_vk_tab_urls(self):
        for url in ('http://vk.com/feed','https://operator@vk.com/feed',
                'https://vk.com:443/feed','https://vk.com:/feed'):
            with self.subTest(url=url),patch.object(self.identity.time,'sleep'):
                context=Context('/a','/id203');context.pages[1].url=url
                self.assertIsNone(self.prepare(context))
                self.assertIsNone(accounts.inspect_context(context,attempts=1))
        self.assertEqual(self.calls,[]);self.assertEqual(self.count(),0)

    def test_added_unsafe_vk_header_revokes_cache_even_when_header_later_disappears(self):
        for lose_header in (False,True):
            context=Context('/a')
            with patch.object(self.identity.time,'sleep'):self.assertEqual(self.prepare(context),202)
            other=Page('/id203',url='http://vk.com/feed');other.context=context;context.pages.append(other)
            self.assertIsNone(accounts.inspect_header_identity(context.pages[0]))
            self.assertIsNone(accounts.inspect_context(context,attempts=1))
            if lose_header:other.header_count=0
            else:context.pages.remove(other)
            self.assertIsNone(accounts.inspect_header_identity(context.pages[0]))
            self.assertIsNone(accounts.inspect_context(context,attempts=1))

    def test_numeric_only_legacy_context_still_checks_unsafe_vk_tab_conflicts(self):
        context=Context('/id202','/id203');context.pages[1].url='http://vk.com/feed'
        with self.assertRaises(accounts.AccountError):accounts.inspect_context(context,attempts=1)
        context.pages[1].href='/id202'
        self.assertEqual(accounts.inspect_context(context,attempts=1),202)
        self.assertEqual(self.calls,[])

    def test_unknown_or_conflicting_header_revokes_prior_ephemeral_proof(self):
        for replacement in ('/feed','/b'):
            context=Context('/a')
            with patch.object(self.identity.time,'sleep'):self.prepare(context)
            context.pages[0].href=replacement
            self.assertIsNone(accounts.inspect_context(context,attempts=1))
            context.pages[0].href='/a'
            self.assertIsNone(accounts.inspect_context(context,attempts=1))

    def test_nonuser_or_malformed_public_response_cannot_become_cached_identity(self):
        for response in ({'type':'group','object_id':202},{'type':'application','object_id':202},
                [{'id':True,'screen_name':'private_alias'}],[{'id':0,'screen_name':'private_alias'}],
                [{'id':202,'screen_name':'other_alias'}],[{'id':202}],
                [{'id':202,'screen_name':'private_alias'},{'id':203,'screen_name':'private_alias'}],[],None):
            context=Context('/private_alias')
            with self.subTest(response=response),patch.object(self.identity.time,'sleep'),\
                 patch.object(api,'bounded_transport',return_value=(200,{'response':response})):
                with self.assertRaises(api.VkApiFailure) as failure:self.prepare(context)
                self.assertNotIn('private_alias',str(failure.exception))
                self.assertIsNone(accounts.inspect_header_identity(context.pages[0]))

    def test_cached_proof_does_not_keep_browser_context_alive(self):
        context=Context('/a');self.prepare(context);reference=weakref.ref(context)
        del context;gc.collect()
        self.assertIsNone(reference())

    def test_deadline_before_read_and_after_user_callback_never_reserves_http(self):
        now=datetime.now(timezone.utc).timestamp()
        with patch.object(self.identity.time,'time',return_value=now):
            with self.assertRaises(accounts.IdentityPreparationDeadline):
                self.prepare(Context('/a'),deadline=datetime.fromtimestamp(now+19,timezone.utc))
        tick=[now]
        def guard():tick[0]+=15
        with patch.object(self.identity.time,'time',side_effect=lambda:tick[0]):
            with self.assertRaises(accounts.IdentityPreparationDeadline):
                self.prepare(Context('/a'),deadline=datetime.fromtimestamp(now+30,timezone.utc),before_request=guard)
        self.assertEqual(self.calls,[]);self.assertEqual(self.count(),0)

    def test_pacing_that_cannot_leave_twenty_seconds_never_reserves_http(self):
        now=datetime.now(timezone.utc).timestamp()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute('INSERT INTO read_navigation_pacing VALUES(0,?)',(now,));db.commit()
        with patch.object(self.identity.time,'time',return_value=now):
            with self.assertRaises(accounts.IdentityPreparationDeadline):
                self.prepare(Context('/a'),deadline=datetime.fromtimestamp(now+23,timezone.utc))
        self.assertEqual(self.calls,[]);self.assertEqual(self.count(),0)

    def test_existing_primary_lock_is_respected_and_held_lease_is_not_reacquired(self):
        with app.exclusive_run(self.root/'.vk-unified-running.lock'):
            with self.assertRaises(app.RunAlreadyActive):self.prepare(Context('/a'))
            self.assertEqual(self.prepare(Context('/a'),primary_lock_held=True),202)
        self.assertEqual(self.count(),1)

    def test_missing_catalog_or_key_never_creates_catalog_or_http(self):
        with patch.object(api,'configured_token',return_value=None):
            with self.assertRaises(accounts.AccountError):self.prepare(Context('/a'))
        self.db_path.unlink()
        with self.assertRaises(accounts.AccountError):self.prepare(Context('/a'))
        self.assertFalse(self.db_path.exists());self.assertEqual(self.calls,[])

    def test_budget_exhaustion_cannot_send_or_cache_and_rate_limit_latches(self):
        stamp=datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executemany('INSERT INTO api_read_attempts(group_id,attempted_at) VALUES(0,?)',[(stamp,)]*5000);db.commit()
        context=Context('/a')
        with self.assertRaises(api.VkApiFailure):self.prepare(context)
        self.assertEqual(self.calls,[]);self.assertIsNone(accounts.inspect_header_identity(context.pages[0]))
        with closing(sqlite3.connect(self.db_path)) as db:db.execute('DELETE FROM api_read_attempts');db.commit()
        with patch.object(api,'bounded_transport',return_value=(429,{})):
            with self.assertRaises(api.VkApiLimited):self.prepare(context)
        with closing(engine.db_open(self.db_path)) as db:
            with self.assertRaises(engine.StopRun):engine.require_platform_unlocked(db)
        self.assertIsNone(accounts.inspect_header_identity(context.pages[0]))


if __name__=='__main__':unittest.main()
