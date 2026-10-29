"""Real Chromium metadata, all requests fulfilled or aborted locally."""
import json,unittest
from playwright.sync_api import sync_playwright,Error
import vk_batch as engine
import vk_diagnostics as diag


class DiagnosticBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):self.page=self.browser.new_page();self.addCleanup(self.page.close)

    def test_real_footer_reason_has_safe_code_and_counts(self):
        self.page.set_content('<article data-testid="post" data-post-id="-101_11" style="width:300px;height:100px">No toolbar</article>')
        info=engine.post_footer_info(self.page,101,11,'like')
        self.assertEqual(info['reasonCode'],'footer_like_not_unique')
        record=diag.make_record('action_failed',observation=info)
        self.assertEqual(record['observation']['like_elements'],0)
        self.assertNotIn('reason',record['observation'])

    def test_http_error_and_network_failure_log_only_safe_metadata(self):
        events=[]
        diag.watch_network(self.page,lambda event,**data:events.append(diag.make_record(event,**data)))
        self.page.route('**/*',lambda route:route.fulfill(status=503,content_type='application/json',body='{"private":"BODY_SECRET"}'))
        self.page.goto('https://vk.com/al_like.php?access_token=QUERY_SECRET')
        self.assertEqual(events[0]['http_status'],503)
        self.assertEqual(events[0]['request_method'],'GET')
        self.page.unroute('**/*')
        self.page.route('**/*',lambda route:route.abort())
        with self.assertRaises(Error):self.page.goto('https://vk.com/al_wall.php?private=QUERY_SECRET')
        self.assertTrue(any(r['event']=='network_failed' for r in events))
        self.assertNotIn('SECRET',json.dumps(events))


if __name__=='__main__':unittest.main()
