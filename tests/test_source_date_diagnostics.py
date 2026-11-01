"""Date-skip evidence stays bounded and cannot serialize browser/exception data."""
import unittest
from unittest.mock import Mock, patch

import vk_batch as engine
import vk_diagnostics as diagnostics
import vk_scheduled_action as actions


class SourceDateDiagnosticsTests(unittest.TestCase):
    def test_final_skip_retains_last_numeric_inspection_and_read_failure_count(self):
        elapsed = [0.0]
        page = Mock()
        page.wait_for_timeout.side_effect = lambda ms: elapsed.__setitem__(0, elapsed[0] + ms / 1000)
        calls = [0]

        def read(*args):
            calls[0] += 1
            if calls[0] == 1:
                raise actions.PlaywrightError('private browser exception must not persist')
            return {'publishedAt': None, 'source': 'date-unknown', 'date_diagnostics': {
                'post_roots': 1, 'date_anchors': 2, 'inline_assignments': 1,
                'inline_invalid': 1, 'html': 'private DOM', 'url': 'private URL'}}

        with patch.object(engine, 'go_to_post'), \
                patch.object(actions.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch.object(actions.groups, 'exact_post_date_info', side_effect=read):
            with self.assertRaises(actions.SourceDateUnavailable) as caught:
                actions.source_ready(page, 101, 11, 1792644694, lambda: None)
        record = diagnostics.make_record('action_failed', error=caught.exception)
        summary = record['error_chain'][0].get('source_date', {})
        self.assertEqual(summary.get('read_windows'), 4)
        self.assertEqual(summary.get('read_errors'), 1)
        self.assertEqual(summary.get('read_evaluations'), calls[0])
        self.assertEqual(summary.get('post_roots'), 1)
        self.assertEqual(summary.get('date_anchors'), 2)
        self.assertIs(summary.get('last_read_error'), False)
        self.assertNotIn('private', str(record))

    def test_date_summary_rejects_raw_values_and_saturates_counts(self):
        exc = actions.SourceDateUnavailable()
        exc.date_diagnostics = {'post_roots': 1000000, 'date_anchors': -1,
            'read_errors': True, 'inline_arrays': 'private', 'last_read_error': True,
            'html': '<private>', 'url': 'https://private/', 'source_group': 123456789}
        record = diagnostics.make_record('action_failed', error=exc)
        self.assertEqual(record['error_chain'][0].get('source_date'),
                         {'post_roots': 65535, 'last_read_error': True})
        self.assertNotIn('private', str(record))


if __name__ == '__main__':
    unittest.main()
