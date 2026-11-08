import sys
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.prepare_live_evaluation_dataset import select_one


class LiveEvaluationDatasetTests(unittest.TestCase):
    def setUp(self):
        self.task=dict(instance_id='demo__lib-1',repo='demo/lib',base_commit='a'*40,
            patch='GOLD',hints_text='ANSWER',test_patch='OFFICIAL_TEST',FAIL_TO_PASS=['t'])

    def test_reference_solution_removed_and_test_information_is_evaluation_only(self):
        result=select_one([self.task],'demo__lib-1','demo/lib','a'*40)
        self.assertNotIn('patch',result);self.assertNotIn('hints_text',result)
        self.assertEqual(result['test_patch'],'OFFICIAL_TEST');self.assertIn('patch',self.task)

    def test_membership_and_base_identity_required(self):
        for records,repo,base in (([],'demo/lib','a'*40),([self.task,self.task],'demo/lib','a'*40),
                                  ([self.task],'wrong/lib','a'*40),([self.task],'demo/lib','b'*40)):
            with self.assertRaises(ValueError):select_one(records,'demo__lib-1',repo,base)


if __name__=='__main__':unittest.main()
