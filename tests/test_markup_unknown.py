"""Regression for a regular wall URL that VK does not render as a known post root."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

import vk_batch as engine
import vk_groups as multi

GROUP=203298456
POST=1711


def diagnostic():
    return dict(correctRoute=True,idRoot=False,dataRoot=False,visibleRoot=False,
                skeletons=0,matchingLinks=1,host='vk.ru',path=f'/wall-{GROUP}_{POST}',
                documentState='complete',postCount=0,postKeys=[],singlePostWrapper=True,
                articles=2,footerLikeCount=0,footerShareCount=0,
                permalinkArticle=False,permalinkArticleLikes=0,
                permalinkAncestorTag='DIV',permalinkAncestorTestId='post_date_block_preview')


class UnknownMarkupTests(unittest.TestCase):
    def test_correct_route_and_permalink_without_root_is_unresolved_not_hidden(self):
        page=MagicMock()
        page.url=f'https://vk.ru/wall-{GROUP}_{POST}'
        page.wait_for_function.side_effect=PWTimeout('No post root')
        page.evaluate.return_value=diagnostic()
        with patch.object(engine,'ensure_ok'):
            with self.assertRaises(engine.PostMarkupUnknown) as cm:
                engine.go_to_post(page,GROUP,POST)
        self.assertNotIsInstance(cm.exception,engine.PostUnavailable)
        self.assertEqual(page.goto.call_count,2)
        page.locator.assert_not_called()
        self.assertIn('Причина неизвестна',str(cm.exception))

    def test_wrong_route_still_fails_closed(self):
        page=MagicMock()
        page.url=f'https://vk.ru/wall-{GROUP}_{POST}'
        page.wait_for_function.side_effect=PWTimeout('No post root')
        data=diagnostic();data['correctRoute']=False
        page.evaluate.return_value=data
        with patch.object(engine,'ensure_ok'):
            with self.assertRaises(engine.StopRun) as cm:
                engine.go_to_post(page,GROUP,POST)
        self.assertNotIsInstance(cm.exception,engine.PostMarkupUnknown)

    def test_read_only_audit_marks_unresolved_and_keeps_next_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=engine.db_open(Path(tmp)/'history.sqlite3')
            multi.init_dates(db)
            engine.add_posts(db,GROUP,[POST,POST-1])
            for_audit=[dict(group_id=GROUP,post_id=POST,like_status='pending',repost_status='pending'),
                       dict(group_id=GROUP,post_id=POST-1,like_status='pending',repost_status='pending')]
            calls=[]
            def inspect(_page, _db, _group, row, days, recent_only=True):
                calls.append(row['post_id'])
                if row['post_id']==POST:
                    raise engine.PostMarkupUnknown('DOM root missing on correct route')
                return 'checked'
            with patch.object(multi,'candidate_rows',return_value=for_audit), \
                 patch.object(multi,'inspect_one',side_effect=inspect):
                # Simulate same core exception policy as the dispatcher in a separate testable loop.
                outcomes=[]
                for row in for_audit:
                    try:
                        inspect(None,db,None,row,7)
                        outcomes.append('ok')
                    except engine.PostMarkupUnknown as exc:
                        engine.record_post_access(db,GROUP,row['post_id'],'unresolved',str(exc))
                        outcomes.append('unresolved')
            self.assertEqual(outcomes,['unresolved','ok'])
            self.assertEqual(calls,[POST,POST-1])
            self.assertEqual(tuple(db.execute('SELECT like_status,repost_status FROM posts WHERE post_id=?',(POST,)).fetchone()),('pending','pending'))
            self.assertEqual(db.execute('SELECT state FROM post_access WHERE post_id=?',(POST,)).fetchone()[0],'unresolved')
            db.close()

    def test_actual_dispatcher_audit_skips_unknown_and_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbpath=Path(tmp)/'history.sqlite3'
            with closing(engine.db_open(dbpath)) as db:
                multi.init_dates(db)
                engine.add_posts(db,GROUP,[POST,POST-1])
                now=int(datetime.now(timezone.utc).timestamp())
                multi.save_date(db,GROUP,POST,now,'feed-approx',False)
                multi.save_date(db,GROUP,POST-1,now-60,'feed-approx',False)
            playwright=MagicMock()
            context=playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            context.pages=[MagicMock()]
            visit=[]
            def fake_inspect(page,db,group,row,days,recent_only=True):
                visit.append(row['post_id'])
                if row['post_id']==POST:
                    raise engine.PostMarkupUnknown('Unrendered post root')
                return 'checked'
            with patch.object(engine,'PROFILE',Path(tmp)/'profile'), \
                 patch.object(engine,'DB_PATH',dbpath), \
                 patch.object(multi,'sync_playwright',playwright), \
                 patch.object(multi,'inspect_one',side_effect=fake_inspect):
                result=multi.main(['--audit','--only',str(GROUP),'--max-posts','2'])
            self.assertEqual(result,0)
            self.assertEqual(visit,[POST,POST-1])
            with closing(engine.db_open(dbpath)) as db:
                self.assertEqual(db.execute('SELECT state FROM post_access WHERE post_id=?',(POST,)).fetchone()[0],
                                 'unresolved')
                self.assertEqual(tuple(db.execute('SELECT like_status,repost_status FROM posts WHERE post_id=?',(POST,)).fetchone()),
                                 ('pending','pending'))

    def test_unresolved_revisited_only_by_read_only_audit_after_24_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=engine.db_open(Path(tmp)/'history.sqlite3')
            multi.init_dates(db)
            engine.add_posts(db,GROUP,[POST])
            multi.save_date(db,GROUP,POST,int(datetime.now(timezone.utc).timestamp()),'feed-approx',False)
            engine.record_post_access(db,GROUP,POST,'unresolved','Unknown VK markup')
            groups=[{'id':GROUP}]
            self.assertEqual(multi.candidate_rows(db,groups,7,include_unknown=True,for_audit=True,limit=10),[])
            # Execution must always exclude all post_access records, including old unresolved.
            earlier=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat(timespec='seconds')
            db.execute('UPDATE post_access SET checked_at=? WHERE group_id=? AND post_id=?',(earlier,GROUP,POST));db.commit()
            self.assertEqual(multi.candidate_rows(db,groups,7,include_unknown=False,for_audit=False,limit=10),[])
            rows=multi.candidate_rows(db,groups,7,include_unknown=True,for_audit=True,limit=10)
            self.assertEqual([r['post_id'] for r in rows],[POST])
            db.close()

    def test_structural_diagnostics_do_not_capture_text_or_credentials(self):
        markup=f'''<base href="https://vk.ru/"><main><article><a href="/wall-{GROUP}_{POST}">Example</a>
          <div data-testid="post_footer_action_like">Лайк</div></article>
          <script>window.secret='credential-secret'</script></main>'''
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium',args=['--no-sandbox'])
            try:
                page=browser.new_page()
                page.set_content(markup)
                result=page.evaluate(engine.POST_DIAG_JS,{'groupId':GROUP,'postId':POST})
                self.assertEqual(result['matchingLinks'],1)
                self.assertTrue(result['permalinkArticle'])
                self.assertEqual(result['permalinkArticleLikes'],1)
                self.assertFalse(result['dataRoot'])
                self.assertEqual(result['postCount'],0)
                self.assertNotIn('credential-secret',str(result))
            finally:
                browser.close()


if __name__=='__main__': unittest.main()
