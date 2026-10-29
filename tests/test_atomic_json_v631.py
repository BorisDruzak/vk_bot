import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import vk_accounts as accounts


class AtomicJsonTests(unittest.TestCase):
    def test_temporary_windows_sharing_conflict_retries_only_file_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'proof.json'
            accounts.atomic_json(path,{'state':'old'})
            replace=os.replace
            conflict=PermissionError('private data must not be logged');conflict.winerror=5
            seen=[]
            def replace_once_busy(source,target):
                seen.append(source)
                if len(seen)==1:raise conflict
                return replace(source,target)
            with patch.object(accounts.os,'replace',side_effect=replace_once_busy), \
                 patch('time.sleep') as sleep:
                accounts.atomic_json(path,{'state':'new'})
            self.assertEqual(json.loads(path.read_text()),{'state':'new'})
            self.assertEqual(len(seen),2)
            self.assertEqual(seen[0],seen[1])
            sleep.assert_called_once()
            self.assertEqual(list(Path(temp).glob('*.tmp')),[])

    def test_persistent_permission_error_is_bounded_and_old_file_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'proof.json'
            accounts.atomic_json(path,{'state':'old'})
            conflict=PermissionError('not a successful observation');conflict.winerror=5
            with patch.object(accounts.os,'replace',side_effect=conflict) as replace, \
                 patch('time.sleep'):
                with self.assertRaises(PermissionError):accounts.atomic_json(path,{'state':'new'})
            self.assertEqual(replace.call_count,6)
            self.assertEqual(json.loads(path.read_text()),{'state':'old'})
            self.assertEqual(list(Path(temp).glob('*.tmp')),[])

    @unittest.skipUnless(os.name=='nt','Windows file sharing semantics')
    def test_real_windows_reader_can_finish_before_atomic_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'proof.json'
            accounts.atomic_json(path,{'state':'old'})
            reader=path.open('rb')
            timer=threading.Timer(.12,reader.close);timer.start()
            try:accounts.atomic_json(path,{'state':'new'})
            finally:timer.join();reader.close()
            self.assertEqual(json.loads(path.read_text()),{'state':'new'})


if __name__=='__main__':unittest.main()
