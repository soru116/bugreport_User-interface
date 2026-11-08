"""Generic policy checks with synthetic sources, mock infill and real local tests."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_local_fim as fixtures
from fim_patch.shared_budget import SharedBudgetBackend,BudgetExceeded
from fim_patch.unified_repair import UnifiedRepairGenerator,RepairPolicy
from fim_patch.generation_records import logic_fingerprint
from fim_patch.operation_contract import diagnose
from fim_patch.none_contract import VM,CALL,Unknown,NonNoneValue
from fim_patch.saved_review import apply_single_diff,review_saved
from fim_patch.validation import UnittestValidator
from scripts.run_unified_repair import load_manifest,snapshot_path


class SharedBudgetTests(unittest.TestCase):
    def test_cap_is_shared_and_metadata_is_cached(self):
        class Backend:
            def __init__(self):self.calls=0;self.metadata=0
            def describe(self):self.metadata+=1;return {'model':'mock'}
            def infill(self,*args,**kw):self.post('generate',{});return {}
            def post(self,*args):self.calls+=1
        backend=Backend();budget=SharedBudgetBackend(backend,3)
        budget.describe();budget.describe()
        for hole in ('signature','behavior','retry'):budget.infill(hole,'',seed=42)
        with self.assertRaises(BudgetExceeded):budget.infill('fourth','')
        self.assertEqual((backend.calls,backend.metadata,budget.remaining),(3,1,0))
        self.assertEqual(budget.report()['backend_generate_dispatches'],3)
        backend.post('metadata',{}) # post restored, ledger unchanged
        self.assertEqual(budget.report()['backend_generate_dispatches'],3)

    def test_failed_attempt_is_conservatively_counted(self):
        class Backend:
            def infill(self,*args,**kw):raise OSError('offline')
        budget=SharedBudgetBackend(Backend(),1)
        with self.assertRaises(OSError):budget.infill('','')
        with self.assertRaises(BudgetExceeded):budget.infill('','')
        self.assertEqual(budget.report()['events'][0]['status'],'error')

    def test_invalid_policy_never_accepts_bool_or_nonfinite(self):
        for kwargs in ({'max_generation_calls':True},{'max_functions':1.5},
                       {'output_factor':float('nan')},{'max_body_lines':0},{'domain_adapters':('invented',)}):
            with self.assertRaises(ValueError):RepairPolicy(**kwargs)


class UnifiedTests(unittest.TestCase):
    def setUp(self):
        fixtures.LocalFIMTests.setUp(self)
        self.ticket['ticket_id']='unseen-project-912345'
    replace_source=fixtures.LocalFIMTests.replace_source

    def engine(self,outputs,validator=None,limit=3):
        backend,_=fixtures.LocalFIMTests.generator(self,outputs)
        class Counter:
            def count(self,prefix,suffix):return max(1,len(prefix+suffix)//4)
        backend._token_counter=Counter()
        return backend,UnifiedRepairGenerator(backend,RepairPolicy(max_generation_calls=limit),
                     (lambda ticket:validator) if validator else None)

    def test_arbitrary_id_plan_never_calls_backend_or_network(self):
        backend,engine=self.engine([])
        self.ticket['ticket_id']='unseen-project-912345'
        with patch.object(backend,'describe',side_effect=AssertionError('metadata')),patch('urllib.request.urlopen',side_effect=AssertionError('network')):
            result=engine.generate(self.ticket,self.loc,self.repo,plan_only=True)
        self.assertEqual(backend.calls,0)
        self.assertEqual(result['unified_plan']['scope_order'],['expression','statement','body'])
        self.assertEqual(result['unified_plan']['policy']['domain_adapters'],())
        json.dumps(result)

    def test_one_static_candidate_remains_unverified(self):
        backend,engine=self.engine(['cleaned.split(delimiter or ",")'])
        result=engine.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'generated',result)
        self.assertEqual(result['shared_generation_budget']['infill_attempts'],1)
        self.assertEqual(result['unified_stop_reason'],'static_candidate_without_verifier')
        self.assertFalse(result['verifier_configured'])
        self.assertEqual(result['executable_static_candidate_count'],1)
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
        json.dumps(result)

    def test_pipeline_preserves_failed_candidates_with_empty_selection(self):
        from fim_patch.pipeline import write_run
        _,engine=self.engine(['cleaned.split("|")','cleaned.split("|")'],lambda *args:{'status':'failed','feedback':'AssertionError unchanged'})
        result=engine.generate(self.ticket,self.loc,self.repo)
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'output';write_run(output,self.ticket,self.loc,result)
            self.assertEqual((output/'selected.diff').read_bytes(),b'')
            self.assertEqual(json.loads((output/'predictions.jsonl').read_text(encoding='utf8'))['model_patch'],'')
            manifest=json.loads((output/'generated_candidates/manifest.json').read_text(encoding='utf8'))
            self.assertEqual(len(manifest),1)
            self.assertEqual(manifest[0]['executable_fingerprint_version'],'python-executable-ast-v1')

    def test_manifest_generation_entry_writes_complete_accounting_with_mock_model(self):
        import contextlib,io,sys
        from scripts import run_unified_repair as runner
        from fim_patch.backend import OllamaFIM
        class Counter:
            def count(self,prefix,suffix):return max(1,len(prefix+suffix)//4)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'tickets.jsonl').write_text(json.dumps(self.ticket)+'\n',encoding='utf8')
            (root/'localizations.jsonl').write_text(json.dumps(dict(self.loc,ticket_id=self.ticket['ticket_id']))+'\n',encoding='utf8')
            (root/'model_show.json').write_text('{}');(root/'tokenizer.json').write_text('{}')
            data=dict(schema_version=1,tickets='tickets.jsonl',localizations='localizations.jsonl',snapshot_cache_dir='snapshots',
                tokenizer_json='tokenizer.json',model_show_json='model_show.json',model=dict(model='mock',expected_digest='pin'),validation={'kind':'none'})
            manifest=root/'manifest.json';manifest.write_text(json.dumps(data),encoding='utf8')
            output=root/'output'
            with patch.object(sys,'argv',['run','--manifest',str(manifest),'--mode','generate','--output',str(output)]), \
                 patch.object(runner,'snapshot_path',return_value=self.repo),patch.dict('os.environ'), \
                 patch('fim_patch.token_budget.verified_counter',return_value=Counter()), \
                 patch.object(OllamaFIM,'describe',return_value={'model':'mock','digest':'pin'}), \
                 patch.object(OllamaFIM,'infill',return_value={'middle':'cleaned.split(delimiter or ",")','stop_reason':'visible_eot'}), \
                 patch('urllib.request.urlopen',side_effect=AssertionError('network')),contextlib.redirect_stdout(io.StringIO()):
                runner.main()
            summary=json.loads((output/'summary.json').read_text(encoding='utf8'))
            self.assertEqual(summary['requested_tickets'],1)
            self.assertEqual(summary['requested_static_generation_rate'],1.0)
            self.assertEqual(summary['shared_infill_attempts_total'],1)
            self.assertEqual(summary['executable_static_candidates_total'],1)
            self.assertIsNone(summary['official_resolved'])

    def test_real_assertion_feedback_selects_only_passing_patch(self):
        (self.repo/'test_spec.py').write_text('import unittest\nfrom app import parse,unrelated\nclass Behaviour(unittest.TestCase):\n    def test_separator(self):\n        self.assertEqual(parse(" a;b ", ";"), ["a", "b"])\n    def test_unrelated(self):\n        self.assertEqual(unrelated(), 123)\n',encoding='utf8')
        from fim_patch.generator import git
        git(self.repo,'add','test_spec.py');git(self.repo,'-c','user.name=T','-c','user.email=t@invalid','commit','-m','trusted test')
        base=git(self.repo,'rev-parse','HEAD').decode().strip();self.ticket['base_commit']=base;self.loc['base_commit']=base
        spec={'reproducer':{'names':['test_spec.Behaviour.test_separator']},'regression':{'names':['test_spec.Behaviour.test_unrelated']},'expected_failure':'AssertionError'}
        backend,engine=self.engine(['cleaned.split("|")','cleaned.split(delimiter or ",")'],UnittestValidator(spec))
        result=engine.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'generated',result)
        self.assertEqual(result['validation_status'],'plausible')
        self.assertEqual(backend.calls,2)
        self.assertIn('AssertionError',backend.prompts[1][0])
        self.assertEqual(result['static_candidate_count'],2)
        self.assertNotIn('+    parsed = cleaned.split("|")',result['patch'])
        self.assertEqual(result['unified_stop_reason'],'tests_passed')

    def test_environment_error_stops_without_spending_remaining_budget(self):
        backend,engine=self.engine(['cleaned.split("|")'],lambda *args:{'status':'error','feedback':'unavailable runner'})
        result=engine.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(backend.calls,1)
        self.assertEqual(result['patch'],'')
        self.assertTrue(result['generated_before_validation'])
        self.assertEqual(result['unified_stop_reason'],'environment_or_budget_error')

    def test_structural_failure_escalates_scope_under_same_budget(self):
        backend,engine=self.engine(['missing(cleaned)','    parsed = cleaned.split(delimiter or ",")\n'])
        result=engine.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'generated',result)
        self.assertEqual(backend.calls,2)
        self.assertEqual(result['span']['generation_scope'],'issue_statement')
        self.assertEqual(result['shared_generation_budget']['infill_attempts'],2)

    def test_signature_and_behavior_share_limit_and_test_before_selection(self):
        self.ticket['description']='Add a new parameter for split behavior.\n```python\nparse("a;b", separator=";")\n```'
        tested=[]
        def validator(diff,repo):
            tested.append(diff)
            return {'status':'failed','feedback':'AssertionError: separator ignored','official_resolved':False} if len(tested)==1 else {'status':'plausible','official_resolved':True}
        backend,engine=self.engine(['separator=None','    cleaned = value.strip()\n    parsed = cleaned.split("|")\n    return parsed\n','    cleaned = value.strip()\n    parsed = cleaned.split(separator or delimiter or ",")\n    return parsed\n'],validator)
        result=engine.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['unified_plan']['route'],'signature_behavior')
        self.assertEqual(result['patch_status'],'generated',result)
        self.assertEqual((backend.calls,len(tested)),(3,2))
        self.assertEqual(result['shared_generation_budget']['infill_attempts'],3)
        self.assertIn('separator ignored',backend.prompts[2][0])
        self.assertTrue(result['official_resolved']) # mock validator, not benchmark evidence
        json.dumps(result)

    def test_signature_route_without_test_retains_first_static(self):
        self.ticket['description']='Add a new parameter for split behavior.\n```python\nparse("a;b", separator=";")\n```'
        backend,engine=self.engine(['separator=None','    cleaned = value.strip()\n    parsed = cleaned.split("|")\n    return parsed\n'])
        result=engine.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'generated',result)
        self.assertEqual(backend.calls,2)
        self.assertFalse(result['verifier_configured'])
        json.dumps(result)

    def test_two_call_budget_never_attempts_third_signature_retry(self):
        self.ticket['description']='Add a new parameter for split behavior.\n```python\nparse("a;b", separator=";")\n```'
        backend,engine=self.engine(['separator=None','malformed('],limit=2)
        result=engine.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(backend.calls,2)
        self.assertEqual(result['model_calls'],2)
        self.assertEqual(result['shared_generation_budget']['infill_attempts'],2)
        self.assertFalse(result['generated_before_validation'])

    def test_unverified_baseline_is_environment_error_not_semantic_retry(self):
        backend,engine=self.engine(['cleaned.split("|")'],lambda *args:{'status':'failed','reason':'baseline_not_verified'})
        result=engine.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(backend.calls,1)
        self.assertEqual(result['unified_stop_reason'],'environment_or_budget_error')

    def test_related_functions_compose_and_test_under_shared_limit(self):
        self.replace_source(b'def parse(value):\n    return helper(value)\n\ndef helper(value):\n    return value.strip()\n','parse',1,2)
        self.ticket['description']='parse and helper should handle None and lowercase text.'
        tested=[]
        def validator(diff,repo):
            tested.append(diff)
            return {'status':'failed','feedback':'AssertionError in helper: uppercase'} if len(tested)==1 else {'status':'plausible'}
        backend,engine=self.engine(['    return "" if value is None else helper(value)\n','    return value.strip().upper()\n','    return value.strip().lower()\n'],validator)
        result=engine.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['unified_plan']['route'],'related_functions')
        self.assertEqual(result['patch_status'],'generated',result)
        self.assertEqual((backend.calls,len(tested)),(3,2))
        self.assertEqual(result['shared_generation_budget']['infill_attempts'],3)
        self.assertIn('uppercase',backend.prompts[2][0])
        json.dumps(result)

    def test_repeated_failed_executable_logic_stops(self):
        validator=lambda *args:{'status':'failed','feedback':'AssertionError unchanged'}
        backend,engine=self.engine(['cleaned.split("|")','cleaned.split("|")'],validator)
        result=engine.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(backend.calls,2)
        self.assertEqual(result['unified_stop_reason'],'no_logic_progress')
        self.assertEqual(result['static_candidate_count'],1)

    def test_related_oversized_helper_is_blocked_before_infill(self):
        backend,engine=self.engine([])
        engine.policy=RepairPolicy(max_body_lines=1)
        with patch.object(backend,'describe',side_effect=AssertionError('metadata')):
            result=engine.inner(backend).generate(self.ticket,self.loc,self.repo)
        self.assertNotEqual(result['patch_status'],'planned')
        self.assertEqual(backend.calls,0)

    def test_large_function_can_still_use_small_expression_hole(self):
        backend,engine=self.engine(['cleaned.split(delimiter or ",")'])
        engine.policy=RepairPolicy(max_body_lines=1)
        result=engine.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['unified_plan']['scope_order'],['expression','statement'])
        self.assertEqual(result['patch_status'],'generated',result)
        self.assertEqual(backend.calls,1)

    def test_saved_review_rejects_source_hash_and_outside_target(self):
        _,engine=self.engine(['cleaned.split(delimiter or ",")'])
        saved=engine.generate(self.ticket,self.loc,self.repo)
        result=review_saved(self.ticket,self.loc,self.repo,saved,RepairPolicy())
        self.assertEqual(result['replay_static_candidate_count'],1,result)
        changed=copy.deepcopy(saved);changed['source_file_sha256']['app.py']='wrong'
        result=review_saved(self.ticket,self.loc,self.repo,changed,RepairPolicy())
        self.assertEqual(result['replay_static_candidate_count'],0)
        self.assertIn('fingerprint',result['candidates'][0]['reason'])
        import difflib
        changed=copy.deepcopy(saved)
        diff=''.join(difflib.unified_diff(self.raw.decode().splitlines(keepends=True),self.raw.decode().replace('return 123','return 456').splitlines(keepends=True),fromfile='a/app.py',tofile='b/app.py'))
        changed['generated_candidates']=[{'patch':diff}]
        result=review_saved(self.ticket,self.loc,self.repo,changed,RepairPolicy())
        self.assertIn('outside_planned_target',result['candidates'][0]['reason'])


class DiagnosticAndManifestTests(unittest.TestCase):
    def test_unknown_callback_is_not_or_and_unknown_truthiness_stays_unknown(self):
        import ast
        for source,env in (('cb(1,2)',{'cb':CALL}),('x or None',{'x':NonNoneValue()})):
            with self.assertRaises(Unknown):VM(env).value(ast.parse(source,mode='eval').body)

    def test_conversion_adapter_is_explicit(self):
        from tests.test_operation_contract_design import SOURCE,CHANGED
        self.assertEqual(diagnose(SOURCE,CHANGED,'Value.__ilshift__',{})['status'],'structural_evidence_only')
        enabled=diagnose(SOURCE,CHANGED,'Value.__ilshift__',{},adapters=('conversion',))
        self.assertEqual(enabled['status'],'potential_operation_drift')
        self.assertFalse(enabled['semantic_repair_verified'])

    def test_executable_fingerprint_ignores_docstrings_but_full_ast_preserves_them(self):
        first=[('app.py','def f():\n    """A"""\n    return 1\n')]
        second=[('app.py','def f():\n    """B"""\n    return 1\n')]
        self.assertNotEqual(logic_fingerprint(first),logic_fingerprint(second))
        self.assertEqual(logic_fingerprint(first,executable=True),logic_fingerprint(second,executable=True))

    def test_padding_docstring_cannot_inflate_body_retention(self):
        from fim_patch.generator import check_body_retention
        original=''.join('    a'+str(i)+' = '+str(i)+'\n' for i in range(12))+'    return a11\n'
        padded='    """'+('\n    text'*20)+'\n    """\n    return None\n'
        with self.assertRaisesRegex(ValueError,'body_shrank'):check_body_retention(padded,{'original_middle':original},0.5)

    def test_manifest_strict_and_input_hash_verified(self):
        root=Path(__file__).resolve().parents[1]
        data=load_manifest(root/'experiments/eval36_v7/unified_manifest.json')
        self.assertEqual(data['policy'].domain_adapters,())
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'manifest.json'
            raw=json.loads((root/'experiments/eval36_v7/unified_manifest.json').read_text())
            raw['unknown_field']=1;path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError,'unknown manifest'):load_manifest(path)
            raw.pop('unknown_field');raw.pop('input_sha256');raw['policy']['domain_adapters']=['unknown'];path.write_text(json.dumps(raw))
            with self.assertRaises(ValueError):load_manifest(path)
            with self.assertRaises(ValueError):snapshot_path(Path(temp),{'repo':'../..','base_commit':'invalid'})
            self.assertEqual(snapshot_path(Path(temp),{'repo':'../..','base_commit':'a'*40}).parent,Path(temp).resolve())

    def test_saved_hunk_rejects_new_coordinates(self):
        with self.assertRaisesRegex(ValueError,'new_coordinate'):
            apply_single_diff('x=1\n','--- a/app.py\n+++ b/app.py\n@@ -1 +9 @@\n-x=1\n+x=2\n','app.py')


if __name__=='__main__':unittest.main()
