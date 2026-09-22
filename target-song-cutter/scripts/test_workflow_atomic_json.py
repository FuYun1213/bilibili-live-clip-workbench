import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import workflow_app_core as core

class AtomicJsonTests(unittest.TestCase):
    def test_transient_reader_conflict_retries_without_exposing_partial_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"state.json"
            path.write_text('{"old":true}',encoding="utf-8")
            real_replace=core.os.replace
            calls=[]
            def replace(source,target):
                calls.append(source)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")),{"old":True})
                if len(calls)<3:raise PermissionError("reader has file open")
                return real_replace(source,target)
            with patch.object(core.os,"replace",side_effect=replace),patch.object(core.time,"sleep") as sleep:
                core.atomic_json(path,{"new":"昼夜"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),{"new":"昼夜"})
            self.assertEqual(sleep.call_count,2)
            self.assertEqual(list(Path(directory).glob("*.tmp")),[])

    def test_persistent_write_error_preserves_previous_state_and_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"state.json"
            path.write_text('{"old":true}',encoding="utf-8")
            with patch.object(core.os,"replace",side_effect=PermissionError("denied")) as replace,patch.object(core.time,"sleep"):
                with self.assertRaises(PermissionError):core.atomic_json(path,{"new":True})
            self.assertEqual(replace.call_count,20)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),{"old":True})
            self.assertEqual(list(Path(directory).glob("*.tmp")),[])

    def test_interleaved_writers_have_independent_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"state.json"
            real_replace=core.os.replace
            sources=[]
            def replace(source,target):
                sources.append(str(source))
                if len(sources)==1:core.atomic_json(path,{"writer":2})
                return real_replace(source,target)
            with patch.object(core.os,"replace",side_effect=replace):
                core.atomic_json(path,{"writer":1})
            self.assertEqual(len(set(sources)),2)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),{"writer":1})
            self.assertEqual(list(Path(directory).glob("*.tmp")),[])

if __name__=="__main__":unittest.main()
