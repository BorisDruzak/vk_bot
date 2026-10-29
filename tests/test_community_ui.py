"""Real Chromium fixtures, with every HTTP request fulfilled locally or blocked."""
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

import vk_batch as engine


SOURCE = "https://vk.com/wall-10_20"
FIXTURE = """<!doctype html><meta charset=utf-8>
<style>button,label,input{padding:8px;margin:4px}svg{width:22px;height:22px}
#modal{border:1px solid;width:550px;padding:20px}</style>
<div id=page-single-post><article><div data-testid=post data-post-id="-10_20" data-post-nesting-lvl=0>
<button data-testid=post_footer_action_like data-user-likes=false>Like</button>
<button aria-label=Share onclick="document.querySelector('#modal').hidden=false;window.openers++">Share</button>
<div data-testid=wall_comments_comment_root><button aria-label=Share onclick="window.comments++">Comment share</button></div>
</div></article></div>
<section id=modal role=dialog hidden>
<label><input type=radio name=shareOption value=wall>Wall</label>
<label><input type=radio name=shareOption value=message checked>Message</label>
<label data-testid=club_share_option><input type=radio name=shareOption value=club>Community</label>
<label><input type=radio name=shareOption value=story>Story</label>
<div data-testid=share_club_select><input role=combobox readonly>
<svg class=vkuiIcon--dropdown_20 onclick="document.querySelector('#options').hidden=false"><rect width=22 height=22 /></svg></div>
<div id=options hidden>
<button data-testid=share_club_option_98 value=98 label="Other club" onclick="selectClub(this)">Other club</button>
<button data-testid=share_club_option_99 value=99 label="Requested club" onclick="selectClub(this)">Requested club</button>
</div>
<button data-testid=share_tab_submit onclick="window.sent++;window.events.push('click')">Publish</button>
</section>
<script>window.sent=0;window.openers=0;window.comments=0;window.events=[];
function selectClub(el){document.querySelector('[role=combobox]').value=el.getAttribute('label');
document.querySelector('#options').hidden=true;}</script>"""


def target_post(pid, body, *, owner=99, date=None, level=0):
    stamp = (f'<a data-testid="post_date_block_preview" href="/wall-{owner}_{pid}" '
             f'data-timestamp="{date}">Date</a>') if date else ''
    return (f'<article data-testid="post" data-post-id="-{owner}_{pid}" '
            f'data-post-nesting-lvl="{level}">{stamp}{body}</article>')


def nested_source(gid=10, pid=20):
    return (f'<div data-testid="post" data-post-id="-{gid}_{pid}" '
            'data-post-nesting-lvl="1">Reposted source</div>')


class CommunityBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        import vk_community_ui as ui
        self.ui = ui
        self.page = self.browser.new_page()
        self.page.route('**/*', lambda route: route.abort())
        self.page.route(SOURCE, lambda route: route.fulfill(body=FIXTURE, content_type='text/html'))
        self.page.goto(SOURCE)

    def tearDown(self):
        self.page.close()

    def prepare(self):
        return self.ui.prepare(self.page, 10, 20, 99)

    def test_option_timeout_rechecks_identity_and_source_without_send(self):
        self.page.locator(self.ui._CLUB).evaluate('(el)=>el.remove()')
        original=self.ui._wait
        def wait(page,predicate,description,**kw):
            if description=='выбор сообщества':kw['timeout_ms']=0
            return original(page,predicate,description,**kw)
        identity=Mock()
        with patch.object(self.ui,'_wait',side_effect=wait):
            with self.assertRaises(self.ui.CommunityPreparationTimeout):
                self.ui.prepare(self.page,10,20,99,identity_guard=identity)
        self.assertEqual(identity.call_count,2)
        self.assertEqual(self.page.evaluate('[sent,openers]'),[0,1])

    def test_identity_failure_at_option_timeout_remains_a_guard(self):
        self.page.locator(self.ui._CLUB).evaluate('(el)=>el.remove()')
        original=self.ui._wait
        def wait(page,predicate,description,**kw):
            if description=='выбор сообщества':kw['timeout_ms']=0
            return original(page,predicate,description,**kw)
        identity=Mock(side_effect=[None,engine.StopRun('wrong account')])
        with patch.object(self.ui,'_wait',side_effect=wait):
            with self.assertRaises(engine.StopRun):self.ui.prepare(self.page,10,20,99,identity_guard=identity)
        self.assertEqual(self.page.evaluate('sent'),0)

    def test_source_change_at_option_timeout_remains_a_guard(self):
        self.page.locator(self.ui._CLUB).evaluate('(el)=>el.remove()')
        original=self.ui._wait
        def wait(page,predicate,description,**kw):
            if description=='выбор сообщества':kw['timeout_ms']=0
            return original(page,predicate,description,**kw)
        calls=[]
        def identity():
            calls.append(True)
            if len(calls)==2:self.page.locator('[data-post-id="-10_20"]').evaluate('(el)=>el.dataset.postId="-10_21"')
        with patch.object(self.ui,'_wait',side_effect=wait):
            with self.assertRaises(engine.StopRun):self.ui.prepare(self.page,10,20,99,identity_guard=identity)
        self.assertEqual(self.page.evaluate('sent'),0)

    def test_prepare_selects_exact_requested_community_from_default_message(self):
        identity = Mock()
        prepared = self.ui.prepare(self.page, 10, 20, 99, identity_guard=identity)
        self.assertEqual(prepared, dict(source_group_id=10, source_post_id=20,
                                       community_id=99, community_label='Requested club'))
        identity.assert_called()
        self.assertEqual(self.page.locator('[role=combobox]').input_value(), 'Requested club')
        self.assertTrue(self.page.locator('[value=club]').is_checked())
        self.assertFalse(self.page.locator('[value=message]').is_checked())
        self.assertEqual(self.page.evaluate('[sent,openers,comments]'), [0, 1, 0])

    def test_community_option_delayed_beyond_nine_seconds_uses_one_opener_no_send(self):
        self.page.evaluate("""() => {
          const option=document.querySelector('[data-testid=club_share_option]');
          option.style.visibility='hidden';setTimeout(()=>option.style.visibility='visible',11000);
        }""")
        self.prepare()
        self.assertEqual(self.page.evaluate('[sent,openers,comments]'),[0,1,0])

    def test_duplicate_community_options_fail_closed_without_publish(self):
        self.page.evaluate("const el=document.querySelector('[data-testid=club_share_option]');el.parentElement.appendChild(el.cloneNode(true))")
        with self.assertRaises(engine.StopRun):self.prepare()
        self.assertEqual(self.page.evaluate('[sent,openers,comments]'),[0,1,0])

    def test_missing_or_mismatched_community_refuses_prepare_without_send(self):
        for mutation in ["document.querySelector('[value=\"99\"]').remove()",
                         "document.querySelector('[value=\"99\"]').value='98'",
                         "document.querySelector('[value=\"99\"]').setAttribute('label','')"]:
            with self.subTest(mutation=mutation):
                self.page.reload()
                self.page.evaluate(mutation)
                with self.assertRaises(engine.StopRun):
                    self.prepare()
                self.assertEqual(self.page.evaluate('sent'), 0)

    def test_changed_destination_refuses_before_callback(self):
        prepared = self.prepare()
        self.page.locator('[role=combobox]').evaluate("e=>e.value='Other club'")
        before = Mock()
        with self.assertRaises(engine.StopRun):
            self.ui.submit(self.page, prepared, before_publish=before)
        before.assert_not_called()
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_submit_reserves_immediately_before_fake_publish(self):
        prepared = self.prepare()
        self.ui.submit(self.page, prepared,
                       before_publish=lambda: self.page.evaluate("events.push('reserve')"))
        self.assertEqual(self.page.evaluate('events'), ['reserve', 'click'])
        with self.assertRaises(engine.StopRun):
            self.ui.submit(self.page, prepared, before_publish=Mock())
        self.assertEqual(self.page.evaluate('sent'), 1)

    def test_modal_inert_background_does_not_require_actionable_source_footer(self):
        self.page.locator('button[aria-label=Share]').first.evaluate("""el =>
          el.setAttribute('onclick', el.getAttribute('onclick') +
            ';document.querySelector(\"#page-single-post\").inert=true')""")
        prepared = self.prepare()
        self.ui.submit(self.page, prepared, before_publish=Mock())
        self.assertEqual(self.page.evaluate('sent'), 1)

    def test_inert_background_still_refuses_structurally_wrong_source(self):
        prepared = self.prepare()
        self.page.evaluate("""() => {
          const background=document.querySelector('#page-single-post');background.inert=true;
          document.querySelector('[data-post-id]').setAttribute('data-post-nesting-lvl','1');
        }""")
        before = Mock()
        with self.assertRaises(engine.StopRun):
            self.ui.submit(self.page, prepared, before_publish=before)
        before.assert_not_called()
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_same_post_id_on_text_descendant_is_not_another_post_root(self):
        self.page.locator('[data-testid=post]').evaluate("""root => {
          const text=document.createElement('div');text.dataset.testid='post_text';
          text.dataset.postId=root.dataset.postId;text.id='text-0';text.innerText='Source body';
          root.appendChild(text);
        }""")
        prepared = self.prepare()
        self.ui.submit(self.page, prepared, before_publish=Mock())
        self.assertEqual(self.page.evaluate('sent'), 1)

    def test_duplicate_genuine_source_root_is_still_ambiguous(self):
        prepared = self.prepare()
        self.page.locator('[data-testid=post]').evaluate('root=>root.parentElement.appendChild(root.cloneNode(true))')
        before = Mock()
        with self.assertRaises(engine.StopRun):
            self.ui.submit(self.page, prepared, before_publish=before)
        before.assert_not_called()
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_submit_rejects_default_message_or_unknown_radio(self):
        for mutation in ["document.querySelector('[value=message]').checked=true",
                         "document.querySelector('[value=story]').remove()",
                         "document.querySelector('[value=club]').value='unknown'"]:
            with self.subTest(mutation=mutation):
                self.page.reload()
                prepared = self.prepare()
                self.page.evaluate(mutation)
                before = Mock()
                with self.assertRaises(engine.StopRun):
                    self.ui.submit(self.page, prepared, before_publish=before)
                before.assert_not_called()
                self.assertEqual(self.page.evaluate('sent'), 0)

    def test_ambiguous_disabled_or_missing_final_refuses_before_callback(self):
        for mutation in ["document.querySelector('[data-testid=share_tab_submit]').disabled=true",
                         "document.querySelector('[data-testid=share_tab_submit]').remove()",
                         "document.querySelector('#modal').appendChild(document.querySelector('[data-testid=share_tab_submit]').cloneNode(true))"]:
            with self.subTest(mutation=mutation):
                self.page.reload()
                prepared = self.prepare()
                self.page.evaluate(mutation)
                before = Mock()
                with self.assertRaises(engine.StopRun):
                    self.ui.submit(self.page, prepared, before_publish=before)
                before.assert_not_called()
                self.assertEqual(self.page.evaluate('sent'), 0)

    def test_prepare_refuses_duplicate_final(self):
        self.page.evaluate("document.querySelector('#modal').appendChild(document.querySelector('[data-testid=share_tab_submit]').cloneNode(true))")
        with self.assertRaises(engine.StopRun):
            self.prepare()
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_duplicate_target_or_dropdown_is_never_guessed(self):
        for selector in ['[data-testid=share_club_option_99]', 'svg.vkuiIcon--dropdown_20']:
            with self.subTest(selector=selector):
                self.page.reload()
                self.page.locator(selector).evaluate('e=>e.parentElement.appendChild(e.cloneNode(true))')
                with self.assertRaises(engine.StopRun):
                    self.prepare()
                self.assertEqual(self.page.evaluate('sent'), 0)

    def test_callback_mutation_cannot_publish_to_changed_destination(self):
        prepared = self.prepare()
        def before():
            self.page.locator('[value=message]').evaluate('e=>e.checked=true')
        with self.assertRaises(engine.StopRun):
            self.ui.submit(self.page, prepared, before_publish=before)
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_unprepared_or_forged_proof_cannot_submit(self):
        before = Mock()
        with self.assertRaises(engine.StopRun):
            self.ui.submit(self.page, {}, before_publish=before)
        prepared = self.prepare()
        prepared['community_id'] = 98
        with self.assertRaises(engine.StopRun):
            self.ui.submit(self.page, prepared, before_publish=before)
        before.assert_not_called()

    def test_duplicate_community_names_are_ambiguous_after_field_confirmation(self):
        self.page.locator('[data-testid=share_club_option_98]').evaluate("e=>e.setAttribute('label','Requested club')")
        with self.assertRaises(engine.StopRun):
            self.prepare()
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_identity_failure_blocks_submit_before_reservation(self):
        prepared = self.prepare()
        before = Mock()
        with self.assertRaises(engine.StopRun):
            self.ui.submit(self.page, prepared, before_publish=before,
                           identity_guard=Mock(side_effect=engine.StopRun('Mismatch')))
        before.assert_not_called()
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_source_change_and_dialog_replacement_refuse_submit(self):
        for mutation in ["document.querySelector('[data-post-id]').setAttribute('data-post-id','-10_21')",
                         "document.querySelector('#modal').outerHTML=document.querySelector('#modal').outerHTML"]:
            with self.subTest(mutation=mutation):
                self.page.reload()
                prepared = self.prepare()
                self.page.evaluate(mutation)
                before = Mock()
                with self.assertRaises(engine.StopRun):
                    self.ui.submit(self.page, prepared, before_publish=before)
                before.assert_not_called()

    def test_invalid_ids_and_unknown_source_are_rejected(self):
        for ids in [(0, 20, 99), (10, 20, -99), (True, 20, 99), (10, 21, 99)]:
            with self.subTest(ids=ids), self.assertRaises(engine.StopRun):
                self.ui.prepare(self.page, *ids)
        self.assertEqual(self.page.evaluate('openers'), 0)

    def test_waits_for_list_load_then_confirms_field_not_aria_selected(self):
        self.page.evaluate("""() => {
          const select=document.querySelector('[data-testid=share_club_select]');
          select.setAttribute('aria-busy','true');
          const busy=document.createElement('span');busy.innerText='Список опций загружается...';select.appendChild(busy);
          document.querySelector('[data-testid=share_club_option_99]').setAttribute('aria-selected','false');
          setTimeout(()=>{select.removeAttribute('aria-busy');busy.remove()},150);
        }""")
        self.assertEqual(self.prepare()['community_id'], 99)
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_waits_for_dropdown_mounted_after_community_field(self):
        self.page.locator('[data-testid=club_share_option]').evaluate("""label => {
          label.addEventListener('click',()=>{
            const select=document.querySelector('[data-testid=share_club_select]');
            const dropdown=select.querySelector('svg');dropdown.remove();
            setTimeout(()=>select.appendChild(dropdown),350);
          }, {once:true});
        }""")
        self.assertEqual(self.prepare()['community_id'], 99)
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_waits_for_dropdown_visible_after_community_field(self):
        self.page.locator('[data-testid=club_share_option]').evaluate("""label => {
          label.addEventListener('click',()=>{
            const dropdown=document.querySelector('svg');dropdown.style.visibility='hidden';
            setTimeout(()=>dropdown.style.visibility='visible',350);
          }, {once:true});
        }""")
        self.assertEqual(self.prepare()['community_id'], 99)
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_community_click_can_already_open_options_with_chevron_up(self):
        self.page.locator('[data-testid=club_share_option]').evaluate("""label => {
          label.addEventListener('click',()=>{
            document.querySelector('#options').hidden=false;
            document.querySelector('[role=combobox]').setAttribute('aria-expanded','true');
            const icon=document.querySelector('svg');
            icon.setAttribute('class','vkuiIcon vkuiIcon--20 vkuiIcon--chevron_up_20');
            icon.setAttribute('onclick',"window.openerRetoggled=true;document.querySelector('#options').hidden=true");
          }, {once:true});
        }""")
        self.assertEqual(self.prepare()['community_id'], 99)
        self.assertFalse(self.page.evaluate('Boolean(window.openerRetoggled)'))
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_visible_exact_options_prove_open_even_without_aria_expanded(self):
        self.page.locator('[data-testid=club_share_option]').evaluate("""label => {
          label.addEventListener('click',()=>{
            document.querySelector('#options').hidden=false;
            document.querySelector('svg').setAttribute('class','vkuiIcon--chevron_up_20');
          }, {once:true});
        }""")
        self.assertEqual(self.prepare()['community_id'], 99)
        self.assertEqual(self.page.evaluate('sent'), 0)

    def scan(self, html):
        self.page.route('https://vk.com/wall-99', lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
        return self.ui.scan_destination(self.page, 10, 20, 99)

    def test_scan_exact_target_owner_embedded_source_and_exact_date_only(self):
        html = target_post(1, nested_source(), date=1792584000)
        html += target_post(2, nested_source(), owner=98, date=1792584001)
        html += target_post(3, nested_source(10, 21))
        html += target_post(4, '<a href="/wall-10_20">Link only</a>')
        html += target_post(5, '<div data-testid=wall_comments_comment_root>' + nested_source() + '</div>')
        html += target_post(6, nested_source(), level=1)
        html += target_post(7, nested_source())
        html += target_post(8, nested_source(11, 20))
        html += target_post(9, target_post(99, nested_source(), owner=98, level=1))
        self.assertEqual(self.scan(html), [{'post_id': 1, 'published_at': 1792584000},
                                           {'post_id': 7, 'published_at': None}])
        self.assertEqual(self.page.url, 'https://vk.com/wall-99')

    def test_repost_reference_uses_exact_source_json_and_resolves_original(self):
        data={'method':'wall.getById','response':{'items':[{'owner_id':-10,'id':20,'date':1792584000,
            'copy_history':[{'owner_id':-30,'id':40,'copy_history':[{'owner_id':-50,'id':60}]}]}]}}
        self.page.evaluate('(entry)=>window.cur={apiPrefetchCache:[entry]}',data)
        self.assertEqual(self.ui.repost_reference(self.page,10,20),
            {'group_id':50,'post_id':60,'published_at':1792584000})

    def test_repost_reference_accepts_personal_original_and_mixed_chain(self):
        for chain, expected in (({'owner_id':30,'id':40},-30),
                ({'owner_id':30,'id':41,'copy_history':[{'owner_id':-50,'id':40}]},50)):
            with self.subTest(expected=expected):
                entry={'method':'wall.getById','response':{'items':[{
                    'owner_id':-10,'id':20,'date':1792584000,'copy_history':[chain]}]}}
                self.page.evaluate('(entry)=>window.cur={apiPrefetchCache:[entry]}',entry)
                self.assertEqual(self.ui.repost_reference(self.page,10,20),
                    {'group_id':expected,'post_id':40,'published_at':1792584000})

    def test_personal_and_community_original_with_same_id_conflict(self):
        entries=[{'method':'wall.get','response':{'items':[{'owner_id':-10,'id':20,
            'date':1792584000,'copy_history':[{'owner_id':owner,'id':40}]}]}}
            for owner in (30,-30)]
        self.page.evaluate('(entries)=>window.cur={apiPrefetchCache:entries}',entries)
        with self.assertRaises(engine.StopRun):self.ui.repost_reference(self.page,10,20)

    def test_destination_personal_original_matches_exact_sign_and_direct_child(self):
        personal=nested_source(30,40).replace('-30_40','30_40')
        html=target_post(1,nested_source(30,40),date=1792584000)
        html+=target_post(2,personal,date=1792584000)
        html+=target_post(3,nested_source(50,60).replace('Reposted source',
            personal.replace('lvl="1"','lvl="2"')),date=1792584000)
        self.page.route('https://vk.com/wall-99',lambda route:route.fulfill(body=html,content_type='text/html'))
        self.assertEqual(self.ui.scan_destination(self.page,-30,40,99),
            [{'post_id':2,'published_at':1792584000}])

    def test_confirm_destination_accepts_personal_original_with_exact_date(self):
        html=target_post(2,nested_source(30,40).replace('-30_40','30_40'),date=1792584000)
        self.page.route('https://vk.com/wall-99',lambda route:route.fulfill(body=html,content_type='text/html'))
        self.assertEqual(self.ui.confirm_destination(self.page,-30,40,99,
            not_before=1792584000,timeout_ms=100),{'post_id':2,'published_at':1792584000})

    def test_repost_reference_rejects_missing_conflicting_and_ambiguous_metadata(self):
        with self.assertRaises(engine.StopRun):self.ui.repost_reference(self.page,10,20)
        data={'method':'wall.getById','response':{'items':[{'owner_id':-10,'id':20,'date':1792584000,
            'copy_history':[{'owner_id':-30,'id':40},{'owner_id':-50,'id':60}]}]}}
        self.page.evaluate('(entry)=>window.cur={apiPrefetchCache:[entry]}',data)
        with self.assertRaises(engine.StopRun):self.ui.repost_reference(self.page,10,20)

    def test_repost_reference_rejects_conflicting_snapshots_and_other_post(self):
        def entry(pid,original):return {'method':'wall.getById','response':{'items':[
            {'owner_id':-10,'id':pid,'date':1792584000,'copy_history':[{'owner_id':-30,'id':original}]}]}}
        self.page.evaluate('(entries)=>window.cur={apiPrefetchCache:entries}',[entry(20,40),entry(20,41)])
        with self.assertRaises(engine.StopRun):self.ui.repost_reference(self.page,10,20)
        self.page.evaluate('(entries)=>window.cur={apiPrefetchCache:entries}',[entry(21,40)])
        with self.assertRaises(engine.StopRun):self.ui.repost_reference(self.page,10,20)

    def test_scan_never_infers_from_source_reposted_flag_or_nested_timestamp(self):
        html = target_post(1, nested_source().replace('Reposted source',
            '<a data-testid="post_date_block_preview" href="/wall-10_20" data-timestamp="1792584000">date</a>'))
        self.assertEqual(self.scan(html), [{'post_id': 1, 'published_at': None}])

    def test_confirmation_waits_for_delayed_exact_repost_without_another_navigation(self):
        html = target_post(2, nested_source(), date=1792584000)
        delayed = '<script>setTimeout(()=>document.body.insertAdjacentHTML("beforeend",' + repr(html) + '),1700)</script>'
        requests = []
        def serve(route):
            requests.append(route.request.url)
            route.fulfill(body=delayed, content_type='text/html; charset=utf-8')
        self.page.route('https://vk.com/wall-99', serve)
        self.assertEqual(self.ui.confirm_destination(self.page, 10, 20, 99,
            not_before=1792584000, timeout_ms=2500), {'post_id': 2, 'published_at': 1792584000})
        self.assertEqual(requests, ['https://vk.com/wall-99'])

    def test_confirmation_waits_for_exact_date_and_rejects_old_matches(self):
        html = target_post(1, nested_source(), date=1792583900)
        html += target_post(2, nested_source())
        html += '<script>setTimeout(()=>document.querySelectorAll("article")[1].insertAdjacentHTML("afterbegin",' + repr(
            '<a data-testid="post_date_block_preview" href="/wall-99_2" data-timestamp="1792584000">Date</a>') + '),1700)</script>'
        self.page.route('https://vk.com/wall-99', lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
        self.assertEqual(self.ui.confirm_destination(self.page, 10, 20, 99,
            not_before=1792584000, timeout_ms=2500)['post_id'], 2)

    def test_confirmation_timeout_and_ambiguity_have_safe_reason_codes(self):
        self.page.route('https://vk.com/wall-99', lambda route: route.fulfill(body='<p>Empty wall</p>'))
        with self.assertRaises(engine.StopRun) as failure:
            self.ui.confirm_destination(self.page, 10, 20, 99, not_before=1792584000, timeout_ms=100)
        self.assertEqual(failure.exception.reason_code, 'community_confirmation_timeout')
        html = target_post(1, nested_source(), date=1792584000) + target_post(2, nested_source(), date=1792584001)
        self.page.route('https://vk.com/wall-99', lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
        with self.assertRaises(engine.StopRun) as failure:
            self.ui.confirm_destination(self.page, 10, 20, 99, not_before=1792584000, timeout_ms=100)
        self.assertEqual(failure.exception.reason_code, 'community_confirmation_ambiguous')

    def test_scan_uses_pacing_and_stops_on_rate_limit(self):
        with patch.object(engine, 'pace_navigation', wraps=engine.pace_navigation) as paced:
            self.assertEqual(self.scan('<p>Empty wall</p>'), [])
            paced.assert_called_once_with(self.page)
        with self.assertRaises(engine.PlatformRateLimited):
            self.scan('<p>Не удалось загрузить посты. Too many requests</p>')

    def test_scan_direct_http429_stops_even_without_warning_dom(self):
        self.page.route('https://vk.com/wall-99', lambda route: route.fulfill(status=429, body='<p>Busy</p>'))
        with self.assertRaises(engine.PlatformRateLimited):
            self.ui.scan_destination(self.page, 10, 20, 99)


if __name__ == '__main__':
    unittest.main()
