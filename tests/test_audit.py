"""Read-only audit mode regression tests for v6.8."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from vk_batch import (
    StopRun, add_posts, audit_pending, audit_report,
    db_open, get_audit_tasks, save_audit, status_write,
)

G=188206921

class AuditTests(unittest.TestCase):
    def make_db(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        db=db_open(Path(folder.name)/'state.sqlite3')
        self.addCleanup(db.close)
        return db

    @staticmethod
    def state(liked, reposted):
        return {'liked':liked,'reposted':reposted,'source':'inline',
                'canLike':True,'canRepost':True}

    def test_audit_true_marks_already_without_click(self):
        db=self.make_db()
        add_posts(db,G,[13376])
        page=MagicMock()
        with patch('vk_batch.go_to_post') as go, \
             patch('vk_batch.refresh_state',return_value=self.state(True,True)), \
             patch('vk_batch.wait_post_footer_info',return_value={'liked':True}), \
             patch('vk_batch.ensure_ok'), \
             patch('vk_batch.click_like') as like, \
             patch('vk_batch.click_share_then_publish') as share:
            stats=audit_pending(page,db,G,10)
            like.assert_not_called()
            share.assert_not_called()
            go.assert_called_once()
        self.assertEqual(stats['already_like'],1)
        self.assertEqual(stats['already_repost'],1)
        self.assertEqual(db.execute('SELECT like_status,repost_status FROM posts').fetchone()[:],
                         ('already','already'))
        row=db.execute('SELECT liked,reposted,source FROM post_audit').fetchone()
        self.assertEqual(tuple(row),(1,1,'inline'))

    def test_missing_stays_pending_and_audit_report_show_link(self):
        db=self.make_db()
        add_posts(db,G,[13376,13377])
        with patch('vk_batch.go_to_post'), \
             patch('vk_batch.refresh_state',return_value=self.state(False,False)), \
             patch('vk_batch.wait_post_footer_info',return_value={'liked':False}), \
             patch('vk_batch.ensure_ok'):
            s=audit_pending(MagicMock(),db,G,1)
        self.assertEqual(s['both_missing'],1)
        self.assertEqual(len(get_audit_tasks(db,G,0)),2)
        self.assertEqual(db.execute('SELECT like_status FROM posts WHERE post_id=13377').fetchone()[0], 'pending')
        buf=StringIO()
        with redirect_stdout(buf): audit_report(db,G)
        out=buf.getvalue()
        self.assertIn('wall-188206921_13377',out)
        self.assertNotIn('wall-188206921_13376',out)
        self.assertIn('лайк + репост',out)

    def test_sequential_audits_prioritize_not_yet_seen_pending(self):
        db=self.make_db()
        add_posts(db,G,[13376,13377,13378])
        save_audit(db,G,13378,False,False,'inline')
        # A post needing actions stays pending, but is not re-audited endlessly.
        self.assertEqual([r['post_id'] for r in get_audit_tasks(db,G,2)],[13377,13376])

    def test_does_not_scan_uncertain(self):
        db=self.make_db()
        add_posts(db,G,[13376,13408])
        status_write(db,G,13408,'like','uncertain')
        self.assertEqual([r['post_id'] for r in get_audit_tasks(db,G,0)],[13376])

    def test_disagreement_stops_before_writing_status(self):
        db=self.make_db()
        add_posts(db,G,[13376])
        with patch('vk_batch.go_to_post'), \
             patch('vk_batch.refresh_state',return_value=self.state(False,False)), \
             patch('vk_batch.wait_post_footer_info',return_value={'liked':True}), \
             patch('vk_batch.ensure_ok'):
            with self.assertRaisesRegex(StopRun,'не согласованы'):
                audit_pending(MagicMock(),db,G,1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM post_audit').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0],'pending')

    def test_invalid_state_fails_closed(self):
        db=self.make_db()
        add_posts(db,G,[13376])
        with patch('vk_batch.go_to_post'), \
             patch('vk_batch.refresh_state',return_value=self.state(None,False)):
            with self.assertRaisesRegex(StopRun,'не вернул два однозначных'):
                audit_pending(MagicMock(),db,G,1)
        with self.assertRaises(ValueError):
            save_audit(db,G,13376,None,True,'inline')

    def test_existing_verified_conflict_stops(self):
        db=self.make_db()
        add_posts(db,G,[13376])
        status_write(db,G,13376,'like','verified')
        with patch('vk_batch.go_to_post'), \
             patch('vk_batch.refresh_state',return_value=self.state(False,False)), \
             patch('vk_batch.wait_post_footer_info',return_value={'liked':False}), \
             patch('vk_batch.ensure_ok'):
            with self.assertRaisesRegex(StopRun,'был verified'):
                audit_pending(MagicMock(),db,G,1)
        self.assertEqual(db.execute('SELECT like_status FROM posts').fetchone()[0],'verified')

    def test_audit_report_ignores_stale_after_action(self):
        db=self.make_db()
        add_posts(db,G,[13376])
        save_audit(db,G,13376,False,False,'inline')
        status_write(db,G,13376,'like','verified')
        status_write(db,G,13376,'repost','verified')
        output=StringIO()
        with redirect_stdout(output): audit_report(db,G)
        self.assertIn('Кандидатов с отсутствующим действием (на момент проверки): 0', output.getvalue())

if __name__=='__main__':
    unittest.main()

class AuditedQueueTests(unittest.TestCase):
    def test_known_missing_only_and_status_guards(self):
        from vk_batch import get_audited_missing_tasks
        with tempfile.TemporaryDirectory() as folder:
            db=db_open(Path(folder)/'db.sqlite3')
            try:
                add_posts(db,G,[13376,13377,13378,13379])
                save_audit(db,G,13376,False,False,'inline')
                save_audit(db,G,13377,True,True,'inline')
                save_audit(db,G,13378,False,True,'inline')
                status_write(db,G,13378,'like','uncertain')
                self.assertEqual([r['post_id'] for r in get_audited_missing_tasks(db,G,0)],[13376])
                status_write(db,G,13376,'like','verified')
                status_write(db,G,13376,'repost','verified')
                self.assertEqual(get_audited_missing_tasks(db,G,0),[])
            finally:
                db.close()
