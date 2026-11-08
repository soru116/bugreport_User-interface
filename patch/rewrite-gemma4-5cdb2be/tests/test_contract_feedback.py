import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from fim_patch.generator import FIMPatchGenerator
from fim_patch.iterative import IterativeRepairGenerator
from fim_patch.operation_contract import VERSION
from scripts.run_stage4_batch import build_parser,build_run_config,validate_resume_config,build_record,summarize
from fim_patch.cli import validate_generation_args
from tests import test_fim

WARNING=dict(version=VERSION,status='potential_operation_drift',feedback_preview='Unverified operation warning. Output only the fixed-signature FIM body.')
CLEAR=dict(version=VERSION,status='structural_evidence_only',feedback_preview='No supported operation warning.')


class ContractFeedbackTests(unittest.TestCase):
    def setUp(self):test_fim.PatchTests.setUp(self)

    def run_repair(self,outputs,diagnostics=None,counter=True,validator=None):
        class Recorder(test_fim.FakeBackend):
            def infill(this,prefix,suffix,**kwargs):
                this.requests.append(dict(prefix=prefix,suffix=suffix,**kwargs))
                if this.calls==1 and isinstance(outputs[1],Exception):
                    this.calls+=1
                    raise outputs[1]
                return super().infill(prefix,suffix,**kwargs)
        class Count:
            def count(self,*args):return 100 # explicit unit-test fixture, not a real token measurement
        backend=Recorder(outputs);backend.requests=[];backend.num_ctx=4096
        if counter:backend._token_counter=Count()
        inner=FIMPatchGenerator(backend,candidates=1,research_mode=True,prompt_layout='compact_fim',fim_scope='body',
            fim_output_ceiling=128,operation_contract_preview=True,contract_feedback=True,validator=validator)
        diagnosis=diagnostics or [copy.deepcopy(WARNING)]*len(outputs)
        with patch('fim_patch.operation_contract.diagnose',side_effect=diagnosis):
            result=IterativeRepairGenerator(inner,2).generate(self.ticket,self.loc,self.repo)
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
        self.assertEqual(inner.seed,42)
        return backend,result

    def test_repeat_keeps_first_and_stops_with_unverified_feedback(self):
        backend,result=self.run_repair(['    return x.upper()\n','    return x.upper()\n\n'])
        self.assertEqual(backend.calls,2)
        self.assertEqual(result['repair_stop_reason'],'no_logic_progress')
        self.assertEqual(result['contract_retry']['selected_round'],1)
        self.assertEqual(result['static_candidate_count'],2)
        self.assertEqual(result['logical_static_candidate_count'],1)
        self.assertIn('Unverified operation warning',backend.requests[1]['prefix'])
        self.assertNotIn('Previous failure',backend.requests[1]['prefix'])
        self.assertEqual(backend.requests[1]['prefix'],result['repair_history'][0]['candidates'][0]['operation_contract_preview']['prefix'])
        self.assertEqual([r['seed'] for r in backend.requests],[42,43])
        self.assertEqual([r['max_new_tokens'] for r in backend.requests],[128,128])
        stats=summarize([build_record(self.ticket,result=result,output_dir='unused')])
        self.assertEqual(stats['contract_review_stop_counts'],{'no_logic_progress':1})

    def test_warning_cleared_is_not_automatic_proof_or_selection(self):
        _,result=self.run_repair(['    return x.upper()\n','    return x.lower()\n'],[copy.deepcopy(WARNING),copy.deepcopy(CLEAR)])
        self.assertEqual(result['repair_stop_reason'],'contract_review_completed')
        self.assertEqual(result['contract_retry']['selected_round'],1)
        self.assertEqual(result['contract_retry']['selection_reason'],'preserve_first_without_test_evidence')
        self.assertEqual(result['logical_static_candidate_count'],2)
        self.assertIn('x.upper',result['patch'])
        self.assertIsNone(result['official_resolved'])

    def test_backend_error_does_not_lose_first_candidate(self):
        backend,result=self.run_repair(['    return x.upper()\n',OSError('Connection refused')])
        self.assertEqual(backend.calls,2)
        self.assertEqual(result['repair_stop_reason'],'backend_environment_error')
        self.assertEqual(result['patch_status'],'generated')
        self.assertEqual(result['contract_retry']['terminal_patch_status'],'no_valid_candidate')

    def test_unmeasured_budget_does_not_spend_retry(self):
        backend,result=self.run_repair(['    return x.upper()\n'],counter=False)
        self.assertEqual(backend.calls,1)
        self.assertEqual(result['repair_stop_reason'],'contract_feedback_unavailable')
        self.assertEqual(result['patch_status'],'generated')

    def test_validator_pass_overrides_warning_and_never_retries(self):
        backend,result=self.run_repair(['    return x.upper()\n'],validator=lambda *a:dict(status='plausible',official_resolved=True))
        self.assertEqual(backend.calls,1)
        self.assertEqual(result['repair_stop_reason'],'tests_passed')
        self.assertEqual(result['contract_retry']['selection_reason'],'configured_validator_pass')

    def test_later_configured_validator_pass_can_select_second(self):
        outcomes=iter([dict(status='failed',feedback='actual configured test failure'),
                       dict(status='plausible',official_resolved=True)])
        backend,result=self.run_repair(['    return x.upper()\n','    return x.lower()\n'],validator=lambda *a:next(outcomes))
        self.assertEqual(backend.calls,2)
        self.assertEqual(result['contract_retry']['selected_round'],2)
        self.assertEqual(result['contract_retry']['selection_reason'],'configured_validator_pass')
        self.assertTrue(result['official_resolved'])
        self.assertIn('x.lower',result['patch'])

    def test_no_warning_does_not_trigger_extra_call(self):
        backend,result=self.run_repair(['    return x.upper()\n'],[copy.deepcopy(CLEAR)])
        self.assertEqual(backend.calls,1)
        self.assertEqual(result['repair_stop_reason'],'static_apply_passed_without_tests')

    def test_config_requires_explicit_two_round_opt_in(self):
        parser=build_parser()
        base=['--tickets','t','--localizations','l','--output','o','--research-mode','--prompt-layout','compact_fim',
              '--candidates','1','--fim-scope','body','--operation-contract-preview']
        control=build_run_config(validate_generation_args(parser.parse_args(base),parser),[])
        opts=validate_generation_args(parser.parse_args(base+['--contract-feedback','--repair-rounds','2']),parser)
        config=build_run_config(opts,[])
        self.assertEqual(config['contract_feedback'],'contract-feedback-retry-v1')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'run_config.json';path.write_text(json.dumps(control),encoding='utf8')
            with self.assertRaises(ValueError):validate_resume_config(path,config)
        with self.assertRaises(ValueError):FIMPatchGenerator(test_fim.FakeBackend([]),contract_feedback=True)


if __name__=='__main__':unittest.main()
