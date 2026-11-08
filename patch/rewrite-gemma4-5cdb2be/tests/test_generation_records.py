import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from fim_patch.generation_records import annotate_generation
from fim_patch.pipeline import write_run
from scripts.run_stage4_batch import build_record,summarize,write_predictions
from tests import test_fim,test_multi_function

PATCH='--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n'
class GenerationRecordTests(unittest.TestCase):
    def rejected(self):
        return {'patch_status':'no_valid_candidate','patch':'','apply_status':'passed',
            'validation_status':'failed','official_resolved':False,'model':{'model':'test-double'},
            'candidates':[{'candidate_id':1,'status':'rejected','reason':'validation_failed','patch':PATCH,
                'apply_check_status':'passed','validation':{'status':'failed','official_resolved':False}}]}
    def test_failed_test_preserves_candidate_and_writes_separate_diff(self):
        result=self.rejected()
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'case'
            write_run(output,{'ticket_id':'x'},{},result)
            saved=json.loads((output/'result.json').read_text())
            self.assertTrue(saved['generated_before_validation'])
            self.assertEqual(saved['generation_status'],'generated')
            self.assertFalse(saved['official_resolved'])
            self.assertEqual((output/'generated_candidates/001.diff').read_text(),PATCH)
            self.assertEqual((output/'selected.diff').read_text(),'')
            self.assertEqual(json.loads((output/'predictions.jsonl').read_text())['model_patch'],'')
            record=build_record({'ticket_id':'x'},result=saved,output_dir=str(output))
            stats=summarize([record])
            self.assertEqual(stats['generated'],1);self.assertEqual(stats['accepted'],0)
            self.assertEqual(stats['official_resolved'],0)
            self.assertEqual(record['candidate_generated_count'],1)
            self.assertEqual(record['candidate_accepted_count'],0)
            self.assertEqual(write_predictions(Path(temp)/'merged.jsonl',[record],Path(temp)),0)
    def test_noop_and_comment_changes_never_count(self):
        for reason in ('no_op','no_executable_change','SyntaxError','helper_call_incompatible'):
            r=self.rejected();r['candidates'][0].pop('apply_check_status');r['candidates'][0]['reason']=reason
            annotate_generation(r)
            self.assertFalse(r['generated_before_validation'])
    def test_deduplicates_same_patch_but_keeps_trial_history(self):
        r=self.rejected();one=copy.deepcopy(r);one['repair_round']=1
        two=copy.deepcopy(r);two['repair_round']=2
        r['repair_history']=[one,two]
        annotate_generation(r)
        self.assertEqual(r['static_candidate_count'],1)
        self.assertEqual(len(r['generated_candidates'][0]['attempts']),2)
    def test_multifunction_counts_only_composition_checked_diffs(self):
        r=self.rejected();r['multi_function_repair']='v1';r['repair_history']=[copy.deepcopy(r)]
        annotate_generation(r);self.assertFalse(r['generated_before_validation'])
        r['repair_history'][0].update(combined_patch=PATCH,combined_apply_status='passed',
                                     combined_validation={'status':'error','reason':'Docker unavailable'})
        annotate_generation(r);self.assertTrue(r['generated_before_validation'])
        self.assertEqual(r['static_candidate_count'],1)
    def test_single_generator_marks_before_validator_rejection(self):
        test_fim.PatchTests.setUp(self)
        from fim_patch.generator import FIMPatchGenerator
        generator=FIMPatchGenerator(test_fim.FakeBackend(['    return "" if x is None else x.strip()\n']),
            candidates=1,validator=lambda *a:{'status':'failed','official_resolved':False})
        r=generator.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'no_valid_candidate')
        self.assertTrue(r['generated_before_validation'])
        self.assertEqual(r['static_candidate_count'],1)
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
    def test_real_multifunction_failure_keeps_combined_candidate(self):
        test_multi_function.MultiFunctionTests.setUp(self)
        _,_,repair=test_multi_function.MultiFunctionTests.configure(self,[
            '    return helper(x).lower()\n','    return x.strip().upper()\n'],
            validator=lambda *a:{'status':'failed','official_resolved':False},calls=2)
        r=repair.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch'],'')
        self.assertTrue(r['generated_before_validation'])
        self.assertEqual(r['targets_generated'],2)
        self.assertFalse(r['official_resolved'])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

if __name__=='__main__':unittest.main()
