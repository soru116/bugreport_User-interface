import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fim_patch.generator import FIMPatchGenerator
from fim_patch.iterative import IterativeRepairGenerator, OfficialRepairValidator, require_official_environment, test_failure_excerpt
from fim_patch.repair_diagnostics import diagnose_candidate, extract_function
import hashlib
from tests import test_fim


class RepairTests(unittest.TestCase):
    def setUp(self):
        test_fim.PatchTests.setUp(self)

    def backend(self, outputs):
        class Recorder(test_fim.FakeBackend):
            def infill(this, prefix, suffix, **kwargs):
                this.prompts.append(prefix)
                return super(Recorder,this).infill(prefix,suffix,**kwargs)
        backend = Recorder(outputs)
        backend.prompts = []
        return backend

    def test_compact_fim_preserves_reconstruction_and_shortens_feedback(self):
        backend = self.backend(['    return x.strip()', '    return "" if x is None else x.strip()'])
        inner = FIMPatchGenerator(backend, candidates=1, prompt_layout='compact_fim', include_original_body=True)
        result = IterativeRepairGenerator(inner, 3).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'], 'generated')
        self.assertEqual(result['repair_feedback_version'], 'compact-feedback-v1')
        self.assertIn('no_op', backend.prompts[1])
        self.assertNotIn('Previous generated Middle', backend.prompts[1])
        self.assertNotIn('WHAT TO WRITE BELOW', backend.prompts[0])
        self.assertTrue(backend.prompts[0].rstrip().endswith('def f(x):'))
        self.assertEqual((self.repo/'app.py').read_bytes(), self.raw)
        self.assertIn('compact-fim-v1',result['prompt_version'])

    def extraction_result(self, output, enabled=True, stop_reason='visible_eot'):
        backend = self.backend([output])
        backend.stop_reason = stop_reason
        result = FIMPatchGenerator(backend, candidates=1, research_mode=True, target_policy='related_targets_top5',
            safe_function_extraction=enabled).generate(self.ticket,self.loc,self.repo)
        self.assertEqual((self.repo/'app.py').read_bytes(), self.raw)
        return result

    def test_safe_extraction_is_opt_in_and_retains_raw_output(self):
        output = 'def f(x):\n    return "" if x is None else x.strip()\n\ndef extra():\n    return 1\n'
        self.assertEqual(self.extraction_result(output,False)['patch_status'],'no_valid_candidate')
        result = self.extraction_result(output)
        self.assertEqual(result['patch_status'],'generated')
        candidate = result['candidates'][0]
        self.assertEqual(candidate['untrimmed_middle'],output)
        self.assertNotIn('extra',candidate['middle'])
        self.assertEqual(candidate['function_extraction']['status'],'extracted')

    def test_safe_extraction_still_rejects_noop(self):
        result = self.extraction_result('def f(x):\n    return x.strip()\n\ndef extra():\n    return 1\n')
        self.assertEqual(result['patch_status'],'no_valid_candidate')
        self.assertTrue(result['candidates'][0]['reason'].startswith('no_op'))

    def test_safe_extraction_rejects_wrong_target_and_truncated_target(self):
        for output in ('def wrong(x):\n    return x\n\ndef extra():\n    return 1\n',
                       'def f(x):\n    return (\n'):
            with self.subTest(output=output):
                self.assertEqual(self.extraction_result(output)['patch_status'],'no_valid_candidate')

    def test_safe_extraction_does_not_accept_length_stop(self):
        output = 'def f(x):\n    return "" if x is None else x.strip()\n\ndef extra():\n    return 1\n'
        result = self.extraction_result(output,stop_reason='length')
        self.assertTrue(result['candidates'][0]['reason'].startswith('incomplete_function'))
        self.assertNotIn('function_extraction',result['candidates'][0])

    def test_safe_extraction_retains_undefined_name_gate(self):
        result = self.extraction_result('def f(x):\n    return nonexistent(x)\n\ndef extra():\n    return 1\n')
        self.assertEqual(result['patch_status'],'no_valid_candidate')
        self.assertIn('undefined',result['candidates'][0]['reason'])

    def fixed_body_result(self, output, stop_reason='visible_eot'):
        backend = self.backend([output])
        backend.stop_reason = stop_reason
        result = FIMPatchGenerator(backend,candidates=1,research_mode=True,
            target_policy='related_targets_top5',prompt_layout='compact_fim',
            fixed_signature_body=True,include_original_body=True).generate(self.ticket,self.loc,self.repo)
        return backend,result

    def test_fixed_signature_keeps_header_in_prefix_and_repairs_body(self):
        backend,result = self.fixed_body_result('    return "" if x is None else x.strip()\n')
        self.assertEqual(result['patch_status'],'generated')
        self.assertFalse(result['span']['include_signature'])
        self.assertTrue(backend.prompts[0].endswith('def f(x):\n'))
        self.assertIn('do not output its signature',backend.prompts[0])
        self.assertEqual((self.repo/'app.py').read_bytes(), self.raw)
        self.assertIn('-fixed-signature-body-v1',result['prompt_version'])

    def test_fixed_body_rejects_complete_function_and_length_stop(self):
        for output,stop in [('def wrong(x):\n    return x\n','visible_eot'),
                            ('def f(x):\n    return "fixed"\n','visible_eot'),
                            ('    return "fixed"\n','length'),
                            ('    return x.strip()\n','visible_eot')]:
            with self.subTest(output=output,stop=stop):
                _,result=self.fixed_body_result(output,stop)
                self.assertEqual(result['patch_status'],'no_valid_candidate')

    def test_fixed_signature_handles_multiline_header_decorator_and_comments(self):
        raw=b'class A:\n    @staticmethod\n    def f(\n        x: str,\n        y: str = "a:b",\n    ) -> str:\n        # leading body comment\n        return x.strip()\n'
        (self.repo/'app.py').write_bytes(raw)
        test_fim.git(self.repo,'add','app.py')
        test_fim.git(self.repo,'-c','user.name=T','-c','user.email=t@example.invalid','commit','-m','header')
        base=test_fim.git(self.repo,'rev-parse','HEAD').decode().strip()
        self.ticket['base_commit']=base;self.loc['base_commit']=base
        self.loc['stage3_ranked_symbols']=[{'file_path':'app.py','symbol_qualified_name':'A.f','start_line':2,'end_line':8}]
        backend,result=self.fixed_body_result('        # leading body comment\n        return "" if x is None else x.strip()\n')
        self.assertEqual(result['patch_status'],'generated')
        self.assertTrue(backend.prompts[0].endswith('    ) -> str:\n'))
        self.assertEqual(result['span']['line_start'],7)
        self.assertEqual((self.repo/'app.py').read_bytes(),raw)

    def test_fixed_body_requires_matching_prompt_and_fim_contract(self):
        for kwargs in ({'prompt_layout':'task_last'}, {'generation_mode':'rewrite'},
                       {'safe_function_extraction':True}, {'large_function_strategy':'issue_statement'}):
            with self.subTest(kwargs=kwargs):
                options={'prompt_layout':'compact_fim','fixed_signature_body':True,**kwargs}
                with self.assertRaises(ValueError):
                    FIMPatchGenerator(self.backend([]),**options)

    def test_helper_failure_is_rejected_and_fed_back_before_retry(self):
        raw=b'def f(x):\n    return helper(x)\n\ndef helper(x, flag=False):\n    return x\n'
        (self.repo/'app.py').write_bytes(raw)
        test_fim.git(self.repo,'add','app.py')
        test_fim.git(self.repo,'-c','user.name=T','-c','user.email=t@example.invalid','commit','-m','helper')
        base=test_fim.git(self.repo,'rev-parse','HEAD').decode().strip()
        self.ticket['base_commit']=base;self.loc['base_commit']=base
        backend=self.backend(['    return helper(x, bad=True)\n','    return helper(x, flag=True)\n'])
        inner=FIMPatchGenerator(backend,candidates=1,prompt_layout='compact_fim',
            fixed_signature_body=True,helper_contracts=True)
        result=IterativeRepairGenerator(inner,2).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'generated')
        self.assertTrue(result['repair_history'][0]['candidates'][0]['reason'].startswith('helper_call_incompatible'))
        self.assertIn('helper(x, flag=False)',backend.prompts[0])
        self.assertIn("unexpected keyword argument 'bad'",backend.prompts[1])
        self.assertEqual(result['candidates'][0]['helper_call_check']['changed_calls_checked'],1)
        self.assertEqual((self.repo/'app.py').read_bytes(),raw)

    def test_missing_newline_is_automatically_repaired_without_second_call(self):
        backend = self.backend(['    return "" if x is None else x.strip()'])
        result = FIMPatchGenerator(backend,candidates=1).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'generated')
        self.assertTrue(result['candidates'][0]['trailing_newline_added'])
        self.assertEqual(backend.calls,1)
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_noop_feedback_then_success_stops_after_two_rounds(self):
        backend = self.backend(['    return x.strip()','    return "" if x is None else x.strip()'])
        inner = FIMPatchGenerator(backend,candidates=1)
        result = IterativeRepairGenerator(inner,3).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['repair_rounds_used'],2)
        self.assertIn('no_op',backend.prompts[1])
        self.assertIn('Previous generated Middle',backend.prompts[1])
        self.assertIn('no repair logic was produced',backend.prompts[1])
        self.assertIn('Requirement/target audit',backend.prompts[1])
        self.assertEqual(inner.extra_directive,'')
        self.assertEqual(inner.seed,42)

    def test_failed_test_feedback_is_used_and_success_stops(self):
        backend = self.backend(['    return "bad"','    return "" if x is None else x.strip()'])
        reports = iter([{'status':'failed','feedback':'AssertionError: expected empty string', 'official_resolved':False},
                        {'status':'plausible','official_resolved':True}])
        inner = FIMPatchGenerator(backend,candidates=1,validator=lambda *_:next(reports))
        result = IterativeRepairGenerator(inner,3).generate(self.ticket,self.loc,self.repo)
        self.assertTrue(result['official_resolved'])
        self.assertEqual(result['repair_rounds_used'],2)
        self.assertIn('AssertionError',backend.prompts[1])
        self.assertEqual(len(result['repair_history']),2)

    def test_environment_failure_does_not_spend_another_round(self):
        backend = self.backend(['    return "bad"'])
        inner = FIMPatchGenerator(backend,candidates=1,validator=lambda *_:{'status':'error','reason':'Docker unavailable'})
        result = IterativeRepairGenerator(inner,3).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['repair_stop_reason'],'test_environment_error')
        self.assertEqual(backend.calls,1)

    def test_round_limit_and_nonlogic_modifications_remain_rejected(self):
        backend = self.backend(['    return x.strip()']*3)
        result = IterativeRepairGenerator(FIMPatchGenerator(backend,candidates=1),3).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['repair_rounds_used'],3)
        self.assertEqual(result['patch_status'],'no_valid_candidate')
        self.assertEqual(result['repair_stop_reason'],'round_limit')

    def diagnosis(self, middle):
        span = {'symbol':'f','include_signature':True,'start':0,'end':self.raw.index(b'\n\n')+1,
                'newline':'\n','bom':False}
        result = {'span':span,'bug_location':{'file':'app.py'},'target_rank':1,
                  'source_file_sha256':{'app.py':hashlib.sha256(self.raw).hexdigest()}}
        candidate = {'reason':'Generated body changed syntax outside the target function','middle':middle}
        return diagnose_candidate(dict(self.ticket,expected_behavior='return empty string',patch='SECRET'),result,candidate,self.repo)

    def test_scope_error_can_hide_byte_identical_target(self):
        diagnostic = self.diagnosis(self.raw.decode())
        self.assertTrue(diagnostic['target_unchanged'])
        self.assertIn('byte-identical',diagnostic['finding'])
        self.assertNotIn('SECRET',json.dumps(diagnostic))
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_changed_target_is_not_mislabeled_unchanged(self):
        diagnostic = self.diagnosis(self.raw.decode().replace('return x.strip()','return "fixed"'))
        self.assertFalse(diagnostic['target_unchanged'])
        self.assertIn('original gate rejection still applies',diagnostic['finding'])

    def test_incomplete_target_is_not_guessed(self):
        diagnostic = self.diagnosis('def f(x):\n    return (')
        self.assertNotIn('target_unchanged',diagnostic)
        self.assertIn('diagnosis_unavailable',diagnostic)

    def test_nested_functions_are_kept_but_siblings_removed(self):
        middle, _ = extract_function('def f(x):\n    def helper():\n        return x\n    return helper()\ndef sibling():\n    pass\n','f')
        self.assertIn('def helper',middle)
        self.assertNotIn('def sibling',middle)


class OfficialAdapterTests(unittest.TestCase):
    def test_missing_environment_is_detected_before_generation(self):
        with patch('fim_patch.iterative.subprocess.run',return_value=SimpleNamespace(returncode=1,stderr='No SWE-bench',stdout='')):
            with self.assertRaisesRegex(ValueError,'environment_unavailable'):
                require_official_environment()

    def test_assertion_is_kept_when_log_ends_with_long_summary(self):
        log = 'Traceback\nAssertionError: expected 2\n'+'summary\n'*500
        self.assertIn('AssertionError',test_failure_excerpt(log))

    def test_per_instance_report_and_test_log_are_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            def runner(command, cwd, **kwargs):
                run_id = command[command.index('--run_id')+1]
                directory = Path(cwd)/'logs/run_evaluation'/run_id/'iterative-repair'/'case-1'
                directory.mkdir(parents=True)
                (directory/'report.json').write_text(json.dumps({'case-1':{'resolved':False}}))
                (directory/'test_output.txt').write_text('Traceback\nAssertionError: expected 2')
                return SimpleNamespace(returncode=0,stdout='',stderr='')
            validator = OfficialRepairValidator('case-1',tmp,runner=runner)
            result = validator('diff','unused')
            self.assertFalse(result['official_resolved'])
            self.assertEqual(result['status'],'failed')
            self.assertIn('AssertionError',result['feedback'])

    def test_missing_report_is_environment_error_not_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            validator = OfficialRepairValidator('case-1',tmp,
                runner=lambda *a,**k:SimpleNamespace(returncode=1,stdout='',stderr='Docker unavailable'))
            result = validator('diff','unused')
            self.assertEqual(result['status'],'error')
            self.assertNotIn('official_resolved',result)


if __name__ == '__main__':
    unittest.main()
