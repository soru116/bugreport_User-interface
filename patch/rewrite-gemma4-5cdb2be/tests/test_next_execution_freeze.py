import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
from freeze_next_execution import verify


class NextFreezeTests(unittest.TestCase):
    def fixture(self,root):
        source=root/'input.json';source.write_bytes(b'input')
        archive=root/'source_snapshot.zip'
        with zipfile.ZipFile(archive,'w'):pass
        execution=dict(full_denominator=23,archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                       archived_source_sha256={},input_sha256={str(source):hashlib.sha256(b'input').hexdigest()},
                       config=dict(input_sha256={},source_sha256={}))
        path=root/'execution.pending.json';path.write_text(json.dumps(execution))
        (root/'freeze_receipt.json').write_text(json.dumps({'execution_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))
        return source,archive,path

    def test_offline_verification_does_not_authorize_inference(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root)
            self.assertFalse(verify(root)['inference_allowed'])

    def test_changed_settings_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);_,_,path=self.fixture(root);path.write_text('{}')
            with self.assertRaises(ValueError):verify(root)

    def test_changed_input_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source,_,_=self.fixture(root);source.write_bytes(b'changed')
            with self.assertRaises(ValueError):verify(root)

    def test_changed_archive_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);_,archive,_=self.fixture(root);archive.write_bytes(b'changed')
            with self.assertRaises(ValueError):verify(root)


if __name__=='__main__':unittest.main()
