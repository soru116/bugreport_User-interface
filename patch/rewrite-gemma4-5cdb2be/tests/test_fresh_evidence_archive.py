from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'experiments/generalization_v1'),str(ROOT/'src'),str(ROOT)]
from archive_fresh_validation import verify
from core import dump,sha


class EvidenceArchiveTests(unittest.TestCase):
    def fixture(self,root):
        raw=b'saved evidence';path=root/'evidence.zip'
        with zipfile.ZipFile(path,'w') as z:z.writestr('runs/result.json',raw)
        manifest=dict(files=[dict(path='runs/result.json',bytes=len(raw),sha256=sha(raw))],
            archive_sha256=sha(path.read_bytes()),archive_bytes=path.stat().st_size)
        dump(root/'manifest.json',manifest)
        return path,manifest

    def test_saved_evidence_verified_without_source_or_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root)
            self.assertEqual(verify(root)['files'],1)

    def test_archive_byte_change_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,_=self.fixture(root);path.write_bytes(path.read_bytes()+b'changed')
            with self.assertRaises(ValueError):verify(root)

    def test_entry_change_detected_even_if_archive_checksum_refreshed(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,manifest=self.fixture(root)
            with zipfile.ZipFile(path,'w') as z:z.writestr('runs/result.json',b'other evidence')
            manifest['archive_sha256']=sha(path.read_bytes());dump(root/'manifest.json',manifest)
            with self.assertRaises(ValueError):verify(root)


if __name__=='__main__':unittest.main()
