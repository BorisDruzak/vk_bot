"""Local token loader regressions; fixtures contain fake credentials only."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vk_api_read as api


class TokenFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_environment_token_wins_without_reading_file(self):
        with patch.dict(os.environ,{'VK_API_TOKEN':' env-fake ','VK_API_TOKEN_FILE':'missing'},clear=True):
            self.assertEqual(api.configured_token(),'env-fake')

    def test_default_file_is_relative_to_module_not_working_directory(self):
        (self.root/'api_keys.txt').write_text('fake-file-token\n',encoding='utf-8')
        with patch.dict(os.environ,{},clear=True),patch.object(api,'__file__',str(self.root/'vk_api_read.py')):
            self.assertEqual(api.configured_token(),'fake-file-token')

    def test_explicit_utf8_bom_assignment_file(self):
        path=self.root/'local.txt';path.write_text('VK_API_TOKEN="fake-token"\n',encoding='utf-8-sig')
        with patch.dict(os.environ,{'VK_API_TOKEN_FILE':str(path)},clear=True):
            self.assertEqual(api.configured_token(),'fake-token')

    def test_missing_default_returns_none_but_explicit_missing_fails(self):
        with patch.dict(os.environ,{},clear=True),patch.object(api,'__file__',str(self.root/'vk_api_read.py')):
            self.assertIsNone(api.configured_token())
        with patch.dict(os.environ,{'VK_API_TOKEN_FILE':str(self.root/'missing')},clear=True):
            with self.assertRaises(api.VkApiAuthFailure):api.configured_token()

    def test_malformed_or_oversized_file_error_does_not_reveal_contents(self):
        secret='fake-sensitive-material'
        for content in (secret+'\n'+secret,'VK_API_TOKEN=',secret*2000,'UNKNOWN='+secret):
            path=self.root/'local.txt';path.write_text(content,encoding='utf-8')
            with self.subTest(length=len(content)),patch.dict(os.environ,{'VK_API_TOKEN_FILE':str(path)},clear=True):
                with self.assertRaises(api.VkApiAuthFailure) as caught:api.configured_token()
                self.assertNotIn(secret,str(caught.exception))


if __name__=='__main__':unittest.main()
