from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
import prepare_generation_claim as module
from fim_patch.paired_repair import ARMS


class GenerationClaimTests(unittest.TestCase):
    def test_existing_pair_differs_only_in_summary(self):
        self.assertEqual(set(module.arm_difference(ARMS['A3'],ARMS['A3_summary'])),{'source_summary'})

    def test_second_factor_rejected(self):
        with self.assertRaises(ValueError):
            module.arm_difference(ARMS['A3'],replace(ARMS['A3_summary'],test_feedback=True))

    def fixture(self,root):
        out=root/'output';out.mkdir();code=root/'method.py';code.write_bytes(b'fixed')
        reference=root/'reference.json';reference.write_bytes(b'reference')
        (out/'protocol.json').write_bytes(b'{}')
        with zipfile.ZipFile(out/'source_snapshot.zip','w') as z:z.writestr('method.py',b'fixed')
        h=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
        receipt=dict(artifact_sha256={p.name:h(p) for p in out.iterdir()},
            archived_source_sha256={'method.py':h(code)},implementation_sha256={'method.py':h(code)},
            reference_sha256={str(reference):h(reference)})
        (out/'receipt.json').write_text(json.dumps(receipt))
        return out,code,reference

    def test_unchanged_checkpoint_verified_without_inference(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);out,_,_=self.fixture(root)
            with patch.object(module,'ROOT',root):self.assertEqual(module.verify(out)['generation_requests'],0)

    def test_protocol_and_method_mutations_rejected(self):
        for kind in ('protocol','code'):
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp);out,code,_=self.fixture(root)
                (out/'protocol.json' if kind=='protocol' else code).write_bytes(b'changed')
                with patch.object(module,'ROOT',root),self.assertRaises(ValueError):module.verify(out)

    def test_exposure_reference_change_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);out,_,reference=self.fixture(root);reference.write_bytes(b'changed')
            with patch.object(module,'ROOT',root),self.assertRaises(ValueError):module.verify(out)


if __name__=='__main__':unittest.main()
