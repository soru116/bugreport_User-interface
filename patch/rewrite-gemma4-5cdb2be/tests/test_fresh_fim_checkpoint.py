from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'experiments/generalization_v1'), str(ROOT / 'src'), str(ROOT)]
from freeze_fresh_fim_protocol import archive_sources, check_archive


class FreshCheckpointTests(unittest.TestCase):
    def test_reproducible_archive_and_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            a, b = Path(temp) / 'a.zip', Path(temp) / 'b.zip'
            sources = {'src/code.py': b'print(1)\n', 'scripts/run.py': b'pass\n'}
            receipts = archive_sources(a, sources)
            archive_sources(b, dict(reversed(list(sources.items()))))
            self.assertEqual(a.read_bytes(), b.read_bytes())
            check_archive(a, receipts)
            with self.assertRaises(ValueError):
                check_archive(a, dict(receipts, **{'src/code.py': '0' * 64}))

    def test_archive_extra_entries_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'a.zip'
            receipts = archive_sources(path, {'src/code.py': b'pass\n'})
            with zipfile.ZipFile(path, 'a') as bundle:
                bundle.writestr('extra.py', b'pass\n')
            with self.assertRaises(ValueError):
                check_archive(path, receipts)

    def test_path_escape_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                archive_sources(Path(temp) / 'a.zip', {'../escape.py': b'pass\n'})


if __name__ == '__main__':
    unittest.main()
