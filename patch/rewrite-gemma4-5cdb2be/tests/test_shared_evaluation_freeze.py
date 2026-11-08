import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'experiments/generalization_v1'),str(ROOT)]
from freeze_shared_evaluation import split_cases,history_exposure,verify
from core import sha


class FreezeTests(unittest.TestCase):
    def test_same_cohort_preserved_and_seen_promoted(self):
        dev=[{'ticket_id':'old'}];pilot=[{'ticket_id':'a'},{'ticket_id':'b'}]
        seen,new=split_cases(dev,pilot,{'a'})
        self.assertEqual(seen,[pilot[0]]);self.assertEqual(new,[pilot[1]])
        self.assertEqual(dev,[{'ticket_id':'old'}])

    def test_duplicate_or_overlapping_ids_rejected(self):
        for dev,pilot in (([{'ticket_id':'a'}],[{'ticket_id':'a'}]),([], [{'ticket_id':'a'},{'ticket_id':'a'}])):
            with self.assertRaises(ValueError):split_cases(dev,pilot,set())

    def test_history_exposure_is_output_not_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name,candidates in (('a',[{'raw_response':'code'}]),('b',[]),('c',[{'reason':'budget_exceeded'}])):
                folder=root/name;folder.mkdir()
                (folder/'ticket.json').write_text(json.dumps({'ticket_id':name}))
                (folder/'candidates.json').write_text(json.dumps(candidates))
            exposed,evidence,errors=history_exposure(root)
            self.assertEqual(exposed,{'a'});self.assertEqual(len(evidence),1);self.assertEqual(errors,[])

    def test_artifact_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'facts.json').write_text('{}')
            frozen=dict(implementation_sha256={},input_sha256={},source_sha256={},prepared_artifact_sha256={'facts.json':sha(b'{}')})
            (root/'freeze.json').write_text(json.dumps(frozen))
            self.assertEqual(verify(root)['status'],'verified')
            (root/'facts.json').write_text('changed')
            with self.assertRaises(ValueError):verify(root)


if __name__=='__main__':unittest.main()
