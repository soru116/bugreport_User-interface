import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from fim_patch.generator import FIMPatchGenerator
from fim_patch.operation_contract import capture, VERSION, PREVIEW_VERSION
from fim_patch.pipeline import write_run
from scripts.run_stage4_batch import build_parser,build_run_config,validate_resume_config,build_record,summarize
from fim_patch.cli import validate_generation_args
from tests import test_fim


class PreviewTests(unittest.TestCase):
    def test_fim_markers_are_sanitized_and_no_statement_is_inserted(self):
        before='def f(x):\n    return x\n';after=before.replace('return x','return x+1')
        prefix='# Source context follows; fill the missing code.\ndef f(x):\n'
        diag,preview=capture(before,after,'f',{'expected_behavior':'Expected <MID> value.'},prefix,'END',128)
        self.assertEqual(preview['status'],'ready')
        self.assertNotIn('<MID>',preview['prefix'])
        self.assertEqual(preview['suffix'],'END')
        self.assertEqual(preview['budget_status'],'unmeasured')
        self.assertIsNone(preview['proposed_input_tokens'])
        self.assertTrue(preview['prefix'].endswith('def f(x):\n'))
        self.assertTrue(preview['not_dispatched'])

    def test_missing_boundary_and_failed_counter_do_not_invent_budget(self):
        before='def f(x):\n    return x\n';after=before.replace('return x','return x+1')
        _,preview=capture(before,after,'f',{},'missing','suffix',128)
        self.assertEqual(preview['reason'],'unverified_insertion_boundary')
        class Broken:
            def count(self,*args):raise RuntimeError()
        diag,preview=capture(before,after,'f',{},'# Source context follows; fill the missing code.\n','suffix',128,counter=Broken())
        self.assertEqual(preview['status'],'unavailable')
        self.assertNotIn('prefix',preview)
        self.assertEqual(diag['status'],'structural_evidence_only')

    def test_diagnostic_error_returns_unknown(self):
        diag,preview=capture('broken syntax','broken','f',{},'prefix','suffix',128)
        self.assertEqual(diag['status'],'unknown')
        self.assertFalse(diag['automatic_rejection'])
        self.assertEqual(preview['status'],'unavailable')


class PipelineTests(unittest.TestCase):
    def setUp(self): test_fim.PatchTests.setUp(self)
    def generator(self,enabled=True,safe_body=False):
        backend=test_fim.FakeBackend(['    return "" if x is None else x.strip()\n'])
        self.backend=backend
        return FIMPatchGenerator(backend,candidates=1,research_mode=True,prompt_layout='compact_fim',
            fim_scope='body',fim_output_ceiling=128,operation_contract_preview=enabled,safe_body_feedback=safe_body)

    def test_selected_patch_and_model_calls_unchanged_with_artifacts(self):
        control=self.generator(False).generate(self.ticket,self.loc,self.repo)
        generator=self.generator(True);result=generator.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(self.backend.calls,1)
        self.assertEqual(result['patch'],control['patch'])
        self.assertEqual(result['patch_status'],control['patch_status'])
        self.assertNotIn('operation_contract_preview',control)
        self.assertEqual(result['candidates'][0]['operation_contract_diagnostic']['status'],'structural_evidence_only')
        self.assertEqual(result['candidates'][0]['operation_contract_diagnostic']['source_file'],'app.py')
        self.assertEqual(result['candidates'][0]['operation_contract_diagnostic']['base_commit'],self.ticket['base_commit'])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'run';write_run(output,self.ticket,self.loc,result)
            manifest=json.loads((output/'operation_contract/manifest.json').read_text())
            self.assertEqual(len(manifest),1)
            self.assertTrue(manifest[0]['not_dispatched'])
            self.assertTrue((output/'operation_contract/round001-candidate001-prefix.txt').exists())
            self.assertEqual((output/'selected.diff').read_bytes(),control['patch'].encode())
            record=build_record(self.ticket,result=result,output_dir=str(output))
            stats=summarize([record])
            self.assertEqual(stats['operation_contract_diagnostic_counts'],{'structural_evidence_only':1})

    def test_diagnostic_failure_does_not_reject_static_candidate(self):
        with patch('fim_patch.operation_contract.diagnose',side_effect=RuntimeError('unavailable')):
            result=self.generator().generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'generated')
        self.assertEqual(result['candidates'][0]['operation_contract_diagnostic']['status'],'unknown')

    def test_existing_validation_failure_is_preserved(self):
        generator=self.generator()
        generator.validator=lambda *a:dict(status='failed',official_resolved=False,feedback='AssertionError')
        result=generator.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'no_valid_candidate')
        self.assertEqual(result['candidates'][0]['reason'],'validation_failed')
        self.assertFalse(result['candidates'][0]['validation']['official_resolved'])

    def test_warning_does_not_enable_additional_round(self):
        from fim_patch.iterative import IterativeRepairGenerator
        generator=self.generator(safe_body=True)
        warning=dict(version=VERSION,status='potential_operation_drift',feedback_preview='Unverified warning.')
        with patch('fim_patch.operation_contract.diagnose',return_value=warning):
            result=IterativeRepairGenerator(generator,2).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(self.backend.calls,1)
        self.assertEqual(result['repair_rounds_used'],1)
        self.assertEqual(result['patch_status'],'generated')

    def test_config_isolates_new_flag_and_invalid_scope_rejected(self):
        parser=build_parser()
        base=['--tickets','t','--localizations','l','--output','o','--research-mode',
              '--prompt-layout','compact_fim','--candidates','1','--fim-scope','body']
        plain=build_run_config(validate_generation_args(parser.parse_args(base),parser),[])
        opted=build_run_config(validate_generation_args(parser.parse_args(base+['--operation-contract-preview']),parser),[])
        self.assertEqual(plain['prompt_version'],opted['prompt_version'])
        self.assertEqual(opted['operation_contract_preview'],PREVIEW_VERSION)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'run_config.json';path.write_text(json.dumps(plain),encoding='utf8')
            with self.assertRaises(ValueError):validate_resume_config(path,opted)
        with self.assertRaises(ValueError):FIMPatchGenerator(test_fim.FakeBackend([]),operation_contract_preview=True)


if __name__=='__main__':unittest.main()
