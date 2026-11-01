"""Offline exact-date fixtures; no VK navigation, account profile, or HTTP."""
import json
import unittest

from playwright.sync_api import sync_playwright

import vk_groups as groups


class ExactDateRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.route('**/*', lambda route: route.abort())
        self.addCleanup(self.page.close)

    def entries(self, stamp):
        return [{'method': 'wall.getById', 'response': {'items': [
            {'owner_id': -101, 'id': 11, 'date': stamp}]}}]

    def inspect_inline(self, bodies):
        script = ''.join('window.cur.apiPrefetchCache = ' + body +
                         ';window.apiPrefetchReadyResolve();' for body in bodies)
        self.page.set_content('<script type="text/plain">' + script + '</script>')
        return groups.exact_post_date_info(self.page, 101, 11)

    def test_later_inline_assignment_recovers_exact_date_after_empty_cache(self):
        info = self.inspect_inline(['[]', json.dumps(self.entries(1792644694))])
        self.assertEqual(info['publishedAt'], 1792644694)
        self.assertEqual(info['source'], 'vk-post-json')

    def test_malformed_first_assignment_does_not_hide_later_valid_json(self):
        info = self.inspect_inline(['[not-json]', json.dumps(self.entries(1792644694))])
        self.assertEqual(info['publishedAt'], 1792644694)

    def test_conflicting_later_assignment_fails_closed(self):
        info = self.inspect_inline([json.dumps(self.entries(1792644694)),
                                    json.dumps(self.entries(1792644693))])
        self.assertIsNone(info['publishedAt'])
        self.assertEqual(info['source'], 'date-conflict')

    def inspect_anchors(self, stamps):
        anchors = ''.join('<a data-testid="post_date_block_preview" href="/wall-101_11" '
                          'data-timestamp="' + str(stamp) + '">date</a>' for stamp in stamps)
        self.page.set_content('<article data-testid="post" data-post-id="-101_11">' +
                              anchors + '</article>')
        return groups.exact_post_date_info(self.page, 101, 11)

    def test_same_source_duplicate_anchors_agree_on_one_exact_date(self):
        info = self.inspect_anchors([1792644694, 1792644694])
        self.assertEqual(info['publishedAt'], 1792644694)

    def test_same_source_duplicate_anchors_conflict_stops(self):
        info = self.inspect_anchors([1792644694, 1792644693])
        self.assertIsNone(info['publishedAt'])
        self.assertEqual(info['source'], 'date-conflict')

    def inspect_date_markup(self, markup):
        self.page.set_content('<article data-testid="post" data-post-id="-101_11">' +
                              markup + '</article>')
        return groups.exact_post_date_info(self.page, 101, 11)

    def test_duplicate_anchors_cannot_mask_parent_child_unix_conflict(self):
        info = self.inspect_date_markup('''
            <a data-testid="post_date_block_preview" href="/wall-101_11" data-timestamp="1792644694">
              <time data-timestamp="1792644693"></time></a>
            <a data-testid="post_date_block_preview" href="/wall-101_11" data-timestamp="1792644694">date</a>''')
        self.assertIsNone(info['publishedAt'])
        self.assertEqual(info['source'], 'date-conflict')

    def test_second_own_time_iso_conflict_is_not_hidden_by_first_time(self):
        info = self.inspect_date_markup('''
            <a data-testid="post_date_block_preview" href="/wall-101_11">
              <time datetime="2026-10-22T08:51:34Z"></time>
              <time datetime="2026-10-22T08:51:33Z"></time></a>''')
        self.assertIsNone(info['publishedAt'])
        self.assertEqual(info['source'], 'date-conflict')

    def test_parent_iso_conflict_with_own_time_is_not_hidden(self):
        info = self.inspect_date_markup('''
            <a data-testid="post_date_block_preview" href="/wall-101_11" datetime="2026-10-22T08:51:34Z">
              <time datetime="2026-10-22T08:51:33Z"></time></a>''')
        self.assertIsNone(info['publishedAt'])
        self.assertEqual(info['source'], 'date-conflict')

    def test_nested_other_post_time_cannot_contradict_main_anchor(self):
        info = self.inspect_date_markup('''
            <a data-testid="post_date_block_preview" href="/wall-101_11" data-timestamp="1792644694">
              <span data-testid="post" data-post-id="-555_3">
                <time datetime="2026-10-22T08:51:33Z" data-timestamp="1792644693"></time>
              </span></a>''')
        self.assertEqual(info['publishedAt'], 1792644694)

    def test_unknown_own_time_attributes_are_not_exact_evidence(self):
        info = self.inspect_date_markup('''
            <a data-testid="post_date_block_preview" href="/wall-101_11" data-timestamp="1792644694">
              <time datetime="2026-10-22T08:51:33" data-timestamp="yesterday"></time>
              <time datetime="1 hour ago" data-unixtime=""></time></a>''')
        self.assertEqual(info['publishedAt'], 1792644694)

    def test_duplicate_exact_roots_remain_ambiguous(self):
        self.inspect_anchors([1792644694])
        self.page.evaluate('document.body.append(document.querySelector("article").cloneNode(true))')
        self.assertIsNone(groups.exact_post_date_info(self.page, 101, 11)['publishedAt'])

    def test_unknown_result_has_only_bounded_numeric_inspection_counters(self):
        info = self.inspect_inline(['[not-json]'])
        summary = info.get('date_diagnostics', {})
        self.assertEqual(summary.get('inline_assignments'), 1)
        self.assertEqual(summary.get('inline_invalid'), 1)
        self.assertEqual(summary.get('post_roots'), 0)
        self.assertTrue(all(type(value) is int and 0 <= value <= 65535 for value in summary.values()))


if __name__ == '__main__':
    unittest.main()
