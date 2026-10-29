import unittest
from unittest.mock import Mock,patch
import vk_batch as engine


class ActionConfirmationTests(unittest.TestCase):
    def test_late_main_footer_is_polled_without_another_navigation_or_click(self):
        page=Mock();guard=Mock()
        with patch.object(engine,'go_to_post') as navigate,patch.object(engine,'ensure_ok'),\
             patch.object(engine,'refresh_state',return_value={'liked':True,'reposted':False}),\
             patch.object(engine,'post_footer_info',side_effect=[{'ok':False,'liked':None},{'ok':True,'liked':True}]):
            result=engine.after_action_state(page,10,20,action='like',guard=guard)
        self.assertTrue(result['main_post_liked']);navigate.assert_called_once_with(page,10,20)
        page.wait_for_timeout.assert_called_once_with(250);page.click.assert_not_called()
        self.assertEqual(guard.call_count,2)

    def test_consistent_false_keeps_unconfirmed_and_exposes_only_boolean_evidence(self):
        page=Mock()
        with patch.object(engine,'go_to_post'),patch.object(engine,'ensure_ok'),\
             patch.object(engine,'refresh_state',return_value={'liked':False,'reposted':False,'secret':'PRIVATE'}),\
             patch.object(engine,'post_footer_info',return_value={'ok':True,'liked':False}),\
             patch.object(engine.time,'monotonic',side_effect=[0,10]):
            with self.assertRaises(engine.ActionConfirmationError) as exc:
                engine.after_action_state(page,10,20,action='like')
        self.assertEqual(exc.exception.reason_code,'like_confirmation_timeout')
        self.assertEqual(exc.exception.observation,{'liked':False,'reposted':False,'main_post_liked':False})
        page.click.assert_not_called()

    def test_prefetch_true_but_main_like_false_is_never_verified(self):
        page=Mock()
        with patch.object(engine,'go_to_post'),patch.object(engine,'ensure_ok'),\
             patch.object(engine,'refresh_state',return_value={'liked':True,'reposted':False}),\
             patch.object(engine,'post_footer_info',return_value={'ok':True,'liked':False}),\
             patch.object(engine.time,'monotonic',side_effect=[0,10]):
            with self.assertRaises(engine.ActionConfirmationError):engine.after_action_state(page,10,20,action='like')

    def test_personal_repost_waits_for_its_own_flag_not_existing_like(self):
        page=Mock()
        with patch.object(engine,'go_to_post') as navigate,patch.object(engine,'ensure_ok'),\
             patch.object(engine,'refresh_state',side_effect=[{'liked':True,'reposted':False},{'liked':True,'reposted':True}]),\
             patch.object(engine,'post_footer_info',return_value={'ok':True,'liked':True}):
            result=engine.after_action_state(page,10,20,action='repost')
        self.assertTrue(result['reposted']);navigate.assert_called_once();page.click.assert_not_called()

    def test_guard_warning_is_not_hidden_by_confirmation_timeout(self):
        page=Mock()
        with patch.object(engine,'go_to_post'),patch.object(engine,'ensure_ok'):
            with self.assertRaises(engine.PlatformRateLimited):
                engine.after_action_state(page,10,20,action='like',guard=Mock(side_effect=engine.PlatformRateLimited('offline429')))
        page.click.assert_not_called()


if __name__=='__main__':unittest.main()
