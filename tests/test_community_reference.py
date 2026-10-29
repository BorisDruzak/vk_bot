import tempfile,unittest
from pathlib import Path
from contextlib import closing
import vk_batch as engine
import vk_community as community


class CommunityReferenceTests(unittest.TestCase):
    def test_personal_alias_deduplicates_without_matching_same_number_community(self):
        with tempfile.TemporaryDirectory() as folder,closing(engine.db_open(Path(folder)/'one.db')) as db:
            community.init_state(db,1001)
            community.remember_reference(db,10,20,-30,40)
            community.write_state(db,99,10,20,'verified',target_post_id=7)
            community.remember_reference(db,11,21,-30,40)
            self.assertEqual(community.reference_state(db,99,11,21)['target_post_id'],7)
            community.remember_reference(db,12,22,30,40)
            community.require_reference_available(db,99,12,22)
            with self.assertRaises(engine.StopRun):community.remember_reference(db,10,20,30,40)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)

    def test_uncertain_personal_alias_blocks_same_owner_but_not_other_destination(self):
        with tempfile.TemporaryDirectory() as folder,closing(engine.db_open(Path(folder)/'one.db')) as db:
            community.init_state(db,1001)
            community.remember_reference(db,10,20,-30,40)
            community.write_state(db,99,10,20,'uncertain')
            community.remember_reference(db,11,21,-30,40)
            with self.assertRaises(engine.StopRun):community.require_reference_available(db,99,11,21)
            community.require_reference_available(db,98,11,21)
            for invalid in (0,True,-30.0,-(2**53)):
                with self.subTest(invalid=invalid),self.assertRaises(engine.StopRun):
                    community.remember_reference(db,12,22,invalid,40)

    def test_aliases_share_destination_history_without_changing_attempts(self):
        with tempfile.TemporaryDirectory() as folder,closing(engine.db_open(Path(folder)/'one.db')) as db:
            community.init_state(db,1001)
            community.remember_reference(db,10,20,30,40)
            community.write_state(db,99,10,20,'verified',target_post_id=7)
            community.remember_reference(db,11,21,30,40)
            self.assertEqual(community.reference_state(db,99,11,21)['target_post_id'],7)
            with self.assertRaises(engine.StopRun):community.require_reference_available(db,99,11,21)
            community.require_reference_available(db,98,11,21)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM action_attempts').fetchone()[0],0)
            with self.assertRaises(engine.StopRun):community.remember_reference(db,10,20,50,60)

    def test_uncertain_alias_blocks_other_wrapper_and_original(self):
        with tempfile.TemporaryDirectory() as folder,closing(engine.db_open(Path(folder)/'one.db')) as db:
            community.init_state(db,1001);community.remember_reference(db,10,20,30,40)
            community.write_state(db,99,10,20,'uncertain')
            community.remember_reference(db,11,21,30,40)
            with self.assertRaises(engine.StopRun):community.require_reference_available(db,99,11,21)
            with self.assertRaises(engine.StopRun):community.require_reference_available(db,99,30,40)

    def test_unmapped_historical_repost_blocks_new_wrapper_before_send(self):
        with tempfile.TemporaryDirectory() as folder,closing(engine.db_open(Path(folder)/'one.db')) as db:
            community.init_state(db,1001)
            community.write_state(db,99,10,20,'uncertain')
            community.remember_reference(db,11,21,30,40)
            with self.assertRaises(engine.StopRun):community.require_reference_available(db,99,11,21)


if __name__=='__main__':unittest.main()
