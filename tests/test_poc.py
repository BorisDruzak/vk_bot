import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright
from vk_test import (
    canonical_post_url,
    like_already_set,
    choose_and_click,
    choose_wall_destination,
    choose_publish,
    repost_flow,
    SHARE_DIALOG_JS,
    PUBLISH_CONTROLS_JS,
)


class TestScript(unittest.TestCase):
    def test_canonical_post_url(self):
        self.assertEqual(canonical_post_url('https://vk.ru/wall-188206921_13390'),
                         'https://vk.com/wall-188206921_13390')
        self.assertEqual(canonical_post_url('https://vk.com/group?w=wall-22_33%2Fall'),
                         'https://vk.com/wall-22_33')
        self.assertEqual(canonical_post_url('https://m.vk.com/wall-88_3?from=feed'),
                         'https://vk.com/wall-88_3')

    def test_url_not_external(self):
        for url in ('https://other.example/wall-22_33', 'http://vk.com/wall-22_33', 'https://vk.com/club12'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                canonical_post_url(url)

    def test_like_exists(self):
        self.assertTrue(like_already_set('Убрать реакцию «Лайк»'))
        self.assertFalse(like_already_set('Отправить реакцию «Лайк»'))

    def test_no_click_on_existing_like(self):
        page = MagicMock()
        page.evaluate.return_value = {'rootFound': True, 'candidates': [
            {'id': 0, 'label': 'Убрать реакцию «Лайк»', 'popup': False}
        ]}
        with patch('builtins.input', return_value='1'):
            self.assertEqual(choose_and_click(page, 'like', '-188206921_13390', 'Лайк'), 'already_liked')
        page.locator.assert_not_called()

    def test_repost_stops_on_unverified_destination(self):
        page = MagicMock()
        with patch('vk_test.choose_and_click', return_value='clicked'), \
             patch('vk_test.choose_wall_destination', return_value='destination_not_selected'), \
             patch('vk_test.choose_publish') as publish:
            self.assertEqual(repost_flow(page, '-188206921_13390'), 'destination_not_selected')
            publish.assert_not_called()

    def test_repost_with_confirmed_publish(self):
        page = MagicMock()
        with patch('vk_test.choose_and_click', return_value='clicked'), \
             patch('vk_test.choose_wall_destination', return_value='verified'), \
             patch('vk_test.choose_publish', return_value='clicked'), \
             patch('vk_test.operator_check', return_value='confirmed_by_operator'), \
             patch('builtins.input', return_value='нет'):
            self.assertEqual(repost_flow(page, '-188206921_13390'), 'confirmed_by_operator')

    def test_block_when_direct_message_selected(self):
        page = MagicMock()
        page.evaluate.side_effect = [
            {'ok': True, 'method': 'element', 'point': None,
             'state': {'wall': None, 'direct_message': True}},
            {'ok': True, 'state': {'wall': False, 'direct_message': True}},
        ]
        with patch('builtins.input', return_value='ДА'):
            self.assertEqual(choose_wall_destination(page), 'destination_not_selected')

    def test_publish_refuses_to_send_dm(self):
        page = MagicMock()
        page.evaluate.side_effect = [
            {'ok': True, 'candidates': [{'id': 0, 'label': 'Отправить', 'disabled': False}]},
            {'ok': True, 'state': {'wall': False, 'direct_message': True}},
        ]
        with patch('builtins.input', side_effect=['1', 'ДА']):
            self.assertEqual(choose_publish(page), 'destination_changed')
        page.locator.return_value.click.assert_not_called()

    def test_disabled_publish_never_clicked(self):
        page = MagicMock()
        page.evaluate.return_value = {'ok': True, 'candidates': [
            {'id': 0, 'label': 'Отправить', 'disabled': True}
        ]}
        with patch('builtins.input', return_value='1'):
            self.assertEqual(choose_publish(page), 'publish_disabled')
        page.locator.assert_not_called()

    def test_manual_fallback_for_custom_radio(self):
        page = MagicMock()
        page.evaluate.side_effect = [
            {'ok': True, 'method': 'point', 'point': {'x': 100, 'y': 200},
             'state': {'wall': None, 'direct_message': None}},
            {'ok': True, 'state': {'wall': None, 'direct_message': None}},
        ]
        with patch('builtins.input', side_effect=['ДА', 'да']):
            self.assertEqual(choose_wall_destination(page), 'confirmed_visually')
        page.mouse.click.assert_called_once_with(100, 200)


class TestBrowserFixtures(unittest.TestCase):
    """Offline DOM tests; no VK session, token, account or network use."""
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True,
                          args=['--no-sandbox'])
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_native_radio_and_send_button(self):
        page = self.browser.new_page(viewport={'width': 1320, 'height': 900})
        try:
            page.set_content('''
              <button>Отправить</button><!-- unrelated to share dialog -->
              <section role="dialog" style="width:450px;height:480px;position:fixed;top:30px;right:30px;padding:15px">
                <h2>Поделиться</h2>
                <label><input type="radio" name="dest"> <span>На своей стене</span></label><br>
                <label><input type="radio" name="dest"> <span>В сообщество</span></label><br>
                <label><input type="radio" name="dest" checked> <span>В личном сообщении</span></label><br>
                <button id="publish">Отправить</button>
              </section>
            ''')
            state = page.evaluate(SHARE_DIALOG_JS, {'action': 'prepare'})
            self.assertTrue(state['ok'], state)
            self.assertEqual(state['state'], {'wall': False, 'direct_message': True})
            page.locator('[data-vk-poc-own="true"]').click()
            state = page.evaluate(SHARE_DIALOG_JS, {'action': 'state'})
            self.assertEqual(state['state'], {'wall': True, 'direct_message': False})
            found = page.evaluate(PUBLISH_CONTROLS_JS)
            self.assertTrue(found['ok'], found)
            self.assertEqual(len(found['candidates']), 1)
            self.assertEqual(found['candidates'][0]['label'], 'Отправить')
            page.locator('[data-vk-poc-publish="0"]').click()
        finally:
            page.close()

    def test_custom_radio_geometry_and_semantic_checked(self):
        page = self.browser.new_page(viewport={'width': 1320, 'height': 900})
        try:
            page.set_content('''
              <style>.option{height:38px;display:flex;align-items:center;gap:10px;cursor:pointer}
              .dot{display:inline-block;width:16px;height:16px;border:1px solid gray;border-radius:50%}
              .name{display:inline-block}</style>
              <section role="dialog" style="width:450px;height:480px;position:fixed;top:30px;right:30px;padding:15px">
                <h2>Поделиться</h2>
                <div class="option" onclick="selectOption(this)"><i class="dot"></i><span class="name">На своей стене</span></div>
                <div class="option" onclick="selectOption(this)"><i class="dot"></i><span>В сообщество</span></div>
                <div class="option" onclick="selectOption(this)"><i class="dot"></i><span>В личном сообщении</span></div>
                <button id="publish">Опубликовать</button>
              </section>
              <script>
                function selectOption(row) {
                  document.querySelectorAll('.option').forEach(e => e.removeAttribute('data-selected'));
                  row.dataset.selected = 'yes';
                }
              </script>
            ''')
            out = page.evaluate(SHARE_DIALOG_JS, {'action': 'prepare'})
            self.assertTrue(out['ok'], out)
            self.assertEqual(out['method'], 'point')
            pt = out['point']
            page.mouse.click(pt['x'], pt['y'])
            self.assertEqual(page.locator('.option[data-selected="yes"] .name').inner_text(), 'На своей стене')
            found = page.evaluate(PUBLISH_CONTROLS_JS)
            self.assertEqual(found['candidates'][0]['label'], 'Опубликовать')
        finally:
            page.close()


    def test_final_publish_button_named_podelitsya_postom(self):
        """Regression: VK button label from 20.10.2026 screenshot."""
        page = self.browser.new_page(viewport={'width': 1320, 'height': 900})
        try:
            page.set_content('''
              <button id="outer_share">Поделиться постом</button>
              <section role="dialog" style="width:450px;height:480px;position:fixed;top:30px;right:30px;padding:15px">
                <h2>Поделиться</h2>
                <label><input type="radio" name="dest" checked> <span>На своей стене</span></label><br>
                <label><input type="radio" name="dest"> <span>В личном сообщении</span></label><br>
                <button id="finish">Поделиться постом</button>
              </section>
            ''')
            state = page.evaluate(SHARE_DIALOG_JS, {'action': 'state'})
            self.assertTrue(state['ok'])
            self.assertEqual(state['state'], {'wall': True, 'direct_message': False})
            found = page.evaluate(PUBLISH_CONTROLS_JS)
            self.assertTrue(found['ok'], found)
            self.assertEqual(len(found['candidates']), 1, found)
            self.assertEqual(found['candidates'][0]['label'], 'Поделиться постом')
            self.assertEqual(page.locator('[data-vk-poc-publish="0"]').get_attribute('id'), 'finish')
        finally:
            page.close()

    def test_do_not_select_share_modal_heading_or_unrelated_control(self):
        """Never treat «Поделиться» heading as a terminal submit control."""
        page = self.browser.new_page(viewport={'width': 1320, 'height': 900})
        try:
            page.set_content('''
              <button>Поделиться постом</button>
              <section role="dialog" style="width:450px;height:480px;position:fixed;top:30px;right:30px;padding:15px">
                <h2>Поделиться</h2>
                <label><input type="radio" name="dest" checked><span>На своей стене</span></label><br>
                <label><input type="radio" name="dest"><span>В личном сообщении</span></label><br>
                <button aria-label="Поделиться" id="not-final">Поделиться</button>
                <button id="valid-final" disabled>Поделиться постом</button>
              </section>
            ''')
            found = page.evaluate(PUBLISH_CONTROLS_JS)
            self.assertTrue(found['ok'], found)
            self.assertEqual(len(found['candidates']), 1, found)
            self.assertEqual(found['candidates'][0]['label'], 'Поделиться постом')
            self.assertTrue(found['candidates'][0]['disabled'])
            self.assertEqual(page.locator('[data-vk-poc-publish="0"]').get_attribute('id'), 'valid-final')
        finally:
            page.close()

    def test_current_vk_submit_testid_short_share_in_sibling_footer(self):
        """Actual user's DOM: short label and submit below radio group."""
        page = self.browser.new_page(viewport={'width': 1320, 'height': 900})
        try:
            page.set_content('''
              <button id="outside" data-testid="share_tab_submit">Поделиться</button>
              <section class="share-modal" style="width:450px;height:490px;position:fixed;right:30px;top:40px;background:#eee">
                <h2>Поделиться</h2>
                <div class="choices" style="height:270px">
                  <label><input type="radio" name="dest" checked><span>На своей стене</span></label><br>
                  <label><input type="radio" name="dest"><span>В личном сообщении</span></label>
                </div>
                <div data-testid="modalfooter-actions">
                  <button type="button" aria-busy="false" data-testid="share_tab_submit"><span>Поделиться</span></button>
                </div>
              </section>
            ''')
            found = page.evaluate(PUBLISH_CONTROLS_JS)
            self.assertTrue(found['ok'], found)
            self.assertEqual(len(found['candidates']), 1, found)
            self.assertEqual(found['candidates'][0]['label'], 'Поделиться')
            self.assertEqual(page.locator('[data-vk-poc-publish="0"]').get_attribute('data-testid'), 'share_tab_submit')
            self.assertEqual(page.locator('[data-vk-poc-publish="0"]').count(), 1)
            self.assertEqual(page.locator('[data-vk-poc-publish="0"]').evaluate('(el) => el.parentElement.dataset.testid'), 'modalfooter-actions')
        finally:
            page.close()

    def test_submit_testid_not_clickable_outside_modal(self):
        page = self.browser.new_page(viewport={'width': 1320, 'height': 900})
        try:
            page.set_content('''
              <button data-testid="share_tab_submit">Поделиться</button>
              <section style="position:fixed;right:30px;top:30px;width:450px;height:400px;background:#eee">
                <div><label><input type="radio" checked><span>На своей стене</span></label></div>
                <div><label><input type="radio"><span>В личном сообщении</span></label></div>
              </section>
            ''')
            found = page.evaluate(PUBLISH_CONTROLS_JS)
            self.assertFalse(found['ok'], found)
            self.assertEqual(found['candidates'], [])
        finally:
            page.close()

    def test_submit_testid_aria_busy_stops_click(self):
        page = self.browser.new_page(viewport={'width': 1320, 'height': 900})
        try:
            page.set_content('''
              <section style="position:fixed;right:30px;top:30px;width:450px;height:400px;background:#eee">
                <label><input type="radio" checked><span>На своей стене</span></label><br>
                <label><input type="radio"><span>В личном сообщении</span></label><br>
                <button data-testid="share_tab_submit" aria-busy="true">Поделиться</button>
              </section>
            ''')
            found = page.evaluate(PUBLISH_CONTROLS_JS)
            self.assertTrue(found['ok'], found)
            self.assertTrue(found['candidates'][0]['disabled'])
        finally:
            page.close()

    def test_submit_ambiguous_multiple_blocks(self):
        page = self.browser.new_page(viewport={'width': 1320, 'height': 900})
        try:
            page.set_content('''
              <section style="position:fixed;right:30px;top:30px;width:450px;height:400px;background:#eee">
                <label><input type="radio" checked><span>На своей стене</span></label><br>
                <label><input type="radio"><span>В личном сообщении</span></label><br>
                <button data-testid="share_tab_submit">Поделиться</button>
                <button data-testid="share_tab_submit">Поделиться</button>
              </section>
            ''')
            found = page.evaluate(PUBLISH_CONTROLS_JS)
            self.assertFalse(found['ok'], found)
            self.assertEqual(found['candidates'], [])
        finally:
            page.close()

if __name__ == '__main__':
    unittest.main(verbosity=2)
