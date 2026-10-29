"""Real disposable child processes, no HTTP or live configuration."""
import subprocess
import os
import time
import unittest
from unittest.mock import patch

import vk_api_read as api


class BoundedTransportTests(unittest.TestCase):
    def test_total_timeout_reaps_child_and_sanitizes_input(self):
        started=time.monotonic()
        with patch.object(api,'_TRANSPORT_SCRIPT',"import time;time.sleep(10)"):
            with self.assertRaises(api.VkApiReadDeadline) as error:
                api.bounded_transport({'access_token':'PRIVATE-FIXTURE'},timeout=.5)
        self.assertLess(time.monotonic()-started,3)
        self.assertNotIn('PRIVATE',str(error.exception))

    def test_child_returns_only_fixed_errors_without_exception_or_token(self):
        script="import vk_api_read as a; a._default_transport=lambda p:(_ for _ in ()).throw(ValueError('PRIVATE-DOM')); a._transport_stdio()"
        with patch.object(api,'_TRANSPORT_SCRIPT',script):
            with self.assertRaises(api.VkApiFailure) as error:
                api.bounded_transport({'access_token':'PRIVATE-FIXTURE'})
        self.assertNotIn('PRIVATE',str(error.exception))

    def test_child_retains_rate_limit_and_parent_does_not_retry(self):
        script="import vk_api_read as a; a._default_transport=lambda p:(_ for _ in ()).throw(a.VkApiLimited('secret',code=6)); a._transport_stdio()"
        with patch.object(api,'_TRANSPORT_SCRIPT',script):
            with self.assertRaises(api.VkApiLimited) as error:api.bounded_transport({'access_token':'offline'})
        self.assertEqual(error.exception.code,6)

    def test_success_uses_stdin_not_argv_and_returns_original_payload(self):
        script="import vk_api_read as a; a._default_transport=lambda p:(200,{'response':{'items':[],'count':0}}); a._transport_stdio()"
        run=subprocess.run
        def inspect(args,**kw):
            self.assertNotIn('PRIVATE-FIXTURE',' '.join(args));self.assertIn(b'PRIVATE-FIXTURE',kw['input'])
            self.assertNotIn('VK_API_TOKEN',kw['env']);self.assertNotIn('VK_API_TOKEN_FILE',kw['env'])
            self.assertFalse(kw.get('shell',False))
            return run(args,**kw)
        with patch.dict(os.environ,{'VK_API_TOKEN':'PRIVATE-FIXTURE'}),patch.object(api,'_TRANSPORT_SCRIPT',script),patch.object(subprocess,'run',side_effect=inspect):
            self.assertEqual(api.bounded_transport({'access_token':'PRIVATE-FIXTURE'}),(200,{'response':{'items':[],'count':0}}))
