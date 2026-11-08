"""Controlled FIM factors, shared quotas and real synthetic unittest feedback."""
import json
import tempfile
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'experiments/generalization_v1'),str(ROOT)]
from tests import test_local_fim as fixtures
from fim_patch.paired_repair import PairedRepairGenerator,PAIRS,ARMS
from fim_patch.unified_repair import RepairPolicy
from fim_patch.validation import UnittestValidator
from fim_patch.generator import git,FIMPatchGenerator
from run_paired_repair import pilot_rows,arm_summary
import run_paired_repair as runner

class PairedTests(unittest.TestCase):
    setUp=fixtures.LocalFIMTests.setUp
    replace_source=fixtures.LocalFIMTests.replace_source
    def engine(self,arm,outputs,validator=None,limit=1):
        backend,_=fixtures.LocalFIMTests.generator(self,outputs)
        class Counter:
            def count(self,prefix,suffix):return max(1,len(prefix+suffix)//4)
        backend._token_counter=Counter()
        engine=PairedRepairGenerator(backend,arm,RepairPolicy(max_generation_calls=limit),
            (lambda ticket:validator) if validator else None)
        return backend,engine

    def test_context_changes_only_surroundings_and_keeps_seed_hole_budget(self):
        outputs=['cleaned.split(delimiter or ",")'];results=[];backends=[]
        for arm in PAIRS['context']:
            b,e=self.engine(arm,outputs);r=e.generate(self.ticket,self.loc,self.repo);results.append(r);backends.append(b)
        self.assertTrue(all(r['patch_status']=='generated' for r in results),results)
        self.assertEqual(results[0]['span'],results[1]['span'])
        self.assertEqual(results[0]['patch'],results[1]['patch'])
        self.assertEqual(backends[0].budgets,backends[1].budgets)
        self.assertIn('return 123',''.join(backends[0].prompts[0]))
        self.assertNotIn('return 123',''.join(backends[1].prompts[0]))
        self.assertEqual([r['shared_generation_budget']['infill_attempts'] for r in results],[1,1])
        self.assertEqual([r['shared_generation_budget']['events'][0]['seed'] for r in results],[42,42])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_parser_and_probe_are_independent(self):
        text='    cleaned = value.strip()\n    parsed = cleaned.split(delimiter or ",")\n    return parsed\n\ndef unrelated():\n    return (\n'
        results=[]
        for arm in ('A1','A2'):
            b,e=self.engine(arm,[text]);b.stop_reason='length'
            inner=e.inner(b,'body');results.append(inner.generate(self.ticket,self.loc,self.repo))
        self.assertEqual(results[0]['patch_status'],'no_valid_candidate')
        self.assertEqual(results[1]['patch_status'],'generated',results[1])
        self.assertTrue(results[1]['controlled_fim_options']['body_boundary_extraction'])
        self.assertFalse(results[1]['controlled_fim_options']['source_none_probe'])
        self.assertNotIn('source_contract_check',results[1]['candidates'][0])

    def test_source_diagnostics_can_be_disabled_independently(self):
        self.replace_source(b'def f(value):\n    if value is None:\n        return None\n    return value.data\n','f',1,4)
        self.ticket['description']='Correct returned data.'
        results=[]
        for arm in ('A2','A3'):
            b,e=self.engine(arm,['    return value.data\n'])
            results.append(e.inner(b,'body').generate(self.ticket,self.loc,self.repo))
        self.assertEqual(results[0]['patch_status'],'generated',results[0])
        self.assertEqual(results[1]['patch_status'],'no_valid_candidate',results[1])
        self.assertIn('source_none_regression',results[1]['candidates'][0]['reason'])

    def test_real_tests_run_in_both_arms_only_treatment_forwards_assertion(self):
        (self.repo/'test_spec.py').write_text('import unittest\nfrom app import parse,unrelated\nclass B(unittest.TestCase):\n    def test_bug(self):\n        self.assertEqual(parse(" a;b ", ";"), ["a", "b"])\n    def test_regression(self):\n        self.assertEqual(unrelated(), 123)\n',encoding='utf8')
        git(self.repo,'add','test_spec.py');git(self.repo,'-c','user.name=T','-c','user.email=t@invalid','commit','-m','tests')
        base=git(self.repo,'rev-parse','HEAD').decode().strip();self.ticket['base_commit']=base;self.loc['base_commit']=base
        spec={'reproducer':{'names':['test_spec.B.test_bug']},'regression':{'names':['test_spec.B.test_regression']},'expected_failure':'AssertionError'}
        results=[];backends=[]
        for arm in PAIRS['test_feedback']:
            b,e=self.engine(arm,['cleaned.split("|")','cleaned.split(delimiter or ",")'],UnittestValidator(spec),limit=3)
            results.append(e.generate(self.ticket,self.loc,self.repo));backends.append(b)
        self.assertTrue(all(r['validation_status']=='plausible' for r in results),results)
        self.assertEqual([b.calls for b in backends],[2,2])
        self.assertNotIn('AssertionError',backends[0].prompts[1][0])
        self.assertIn('AssertionError',backends[1].prompts[1][0])
        self.assertEqual(backends[0].prompts[0],backends[1].prompts[0])
        self.assertEqual([h['events'][1]['seed'] for h in (r['shared_generation_budget'] for r in results)],[43,43])

    def test_fixed_control_does_not_silently_claim_signature_repair(self):
        self.ticket['description']='Add a new optional parameter.\n```python\nparse("a;b", separator=";")\n```'
        b,e=self.engine('A0',[])
        with patch.object(b,'describe',side_effect=AssertionError('metadata')):r=e.generate(self.ticket,self.loc,self.repo,plan_only=True)
        self.assertEqual(r['patch_status'],'blocked_invalid_context');self.assertEqual(b.calls,0)

    def test_arm_cap_is_enforced_across_scope_retries(self):
        for arm in ('A1','A4'):
            b,e=self.engine(arm,['missing(cleaned)'],limit=1)
            r=e.generate(self.ticket,self.loc,self.repo)
            self.assertEqual(b.calls,1);self.assertEqual(r['shared_generation_budget']['remaining'],0)

    def test_unknown_or_invalid_options_refused(self):
        with self.assertRaises(ValueError):self.engine('unknown',[])
        with self.assertRaises(ValueError):FIMPatchGenerator(fim_context_mode='unknown')
        with self.assertRaises(ValueError):FIMPatchGenerator(body_boundary_extraction=True)

    def cli_fixture(self,root):
        from fim_patch.pipeline import FROZEN_RETRIEVAL_METHOD
        from core import dump,write_rows,sha
        tickets=[];locations=[]
        for repo in ('demoA/project','demoB/project'):
            key=repo.replace('/','__')+'-100'
            tickets.append(dict(self.ticket,ticket_id=key,repo=repo))
            locations.append(dict(self.loc,ticket_id=key,repo=repo,method=dict(FROZEN_RETRIEVAL_METHOD),source_file_sha256={'app.py':sha(self.raw)}))
        write_rows(root/'tickets.jsonl',tickets);write_rows(root/'locations.jsonl',locations)
        dump(root/'tokenizer.json',{});dump(root/'model_show.json',{})
        dump(root/'manifest.json',dict(schema_version=1,tickets='tickets.jsonl',localizations='locations.jsonl',snapshot_cache_dir='snapshots',
            tokenizer_json='tokenizer.json',model_show_json='model_show.json',
            model=dict(model='mock',expected_digest='pin',num_ctx=16384,max_new_tokens=4096),validation={'kind':'none'}))
        return root/'manifest.json'

    def test_cli_plan_never_calls_provider_and_generation_requires_matching_freeze(self):
        from fim_patch.backend import OllamaFIM
        class Counter:
            def count(self,prefix,suffix):return max(1,len(prefix+suffix)//4)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);manifest=self.cli_fixture(root);planned=root/'plan'
            base=['paired','--manifest',str(manifest),'--pair','context','--pilot-count','2','--attempts','1']
            with patch.object(runner,'snapshot_path',return_value=self.repo),patch.object(runner,'verified_counter',return_value=Counter()), \
                 patch.object(OllamaFIM,'describe',side_effect=AssertionError('metadata')),patch.object(OllamaFIM,'infill',side_effect=AssertionError('provider')), \
                 patch('urllib.request.urlopen',side_effect=AssertionError('network')),contextlib.redirect_stdout(io.StringIO()):
                with patch.object(sys,'argv',base+['--mode','plan','--output',str(planned)]):runner.main()
                self.assertEqual(json.loads((planned/'summary.json').read_text())['total_infill_attempts'],0)
                altered=base.copy();altered[altered.index('--attempts')+1]='2'
                with patch.object(sys,'argv',altered+['--mode','generate','--frozen-plan',str(planned),'--output',str(root/'bad')]):
                    with self.assertRaisesRegex(ValueError,'configuration/input/code changed'):runner.main()
                with patch.object(sys,'argv',base+['--mode','generate','--output',str(root/'no-freeze')]):
                    with self.assertRaisesRegex(ValueError,'requires --frozen-plan'):runner.main()

    def test_cli_two_cases_two_arms_dispatch_only_four_mocked_infill_attempts(self):
        from fim_patch.backend import OllamaFIM
        class Counter:
            def count(self,prefix,suffix):return max(1,len(prefix+suffix)//4)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);manifest=self.cli_fixture(root);planned=root/'plan';output=root/'generated'
            base=['paired','--manifest',str(manifest),'--pair','context','--pilot-count','2','--attempts','1']
            with patch.object(runner,'snapshot_path',return_value=self.repo),patch.object(runner,'verified_counter',return_value=Counter()), \
                 patch.object(OllamaFIM,'describe',return_value={'model':'mock','digest':'pin'}), \
                 patch.object(OllamaFIM,'infill',return_value={'middle':'cleaned.split(delimiter or ",")','stop_reason':'visible_eot','metrics':{'prompt_eval_count':80,'eval_count':8}}) as provider, \
                 patch('urllib.request.urlopen',side_effect=AssertionError('network')),contextlib.redirect_stdout(io.StringIO()):
                with patch.object(sys,'argv',base+['--mode','plan','--output',str(planned)]):runner.main()
                self.assertEqual(provider.call_count,0)
                with patch.object(sys,'argv',base+['--mode','generate','--frozen-plan',str(planned),'--output',str(output)]):runner.main()
            self.assertEqual(provider.call_count,4)
            report=json.loads((output/'summary.json').read_text())
            self.assertEqual(report['max_total_repair_infill_attempts'],4)
            self.assertEqual(report['total_infill_attempts'],4)
            for arm in PAIRS['context']:
                self.assertEqual(report['per_arm'][arm]['requested_cases'],2)
                self.assertIsNone(report['per_arm'][arm]['official_repair_rate_full_denominator'])

class PairUtilityTests(unittest.TestCase):
    def test_pilot_is_repo_round_robin_not_success_or_confidence_selected(self):
        tickets=[dict(ticket_id=str(i),repo=repo,resolved=i==2) for i,repo in enumerate(('a','a','b','b','c'))]
        self.assertEqual([t['ticket_id'] for t in pilot_rows(tickets,2)],['0','2'])
        self.assertEqual([t['ticket_id'] for t in pilot_rows(tickets,5)],['0','2','4','1','3'])
    def test_plan_has_no_generation_or_repair_rate(self):
        report=arm_summary([dict(arm='A3',status='planned')],'A3',2,'plan')
        self.assertIsNone(report['static_generation_rate_full_denominator'])
        self.assertIsNone(report['official_repair_rate_full_denominator'])
        self.assertEqual(report['requested_cases'],2)

if __name__=='__main__':unittest.main()
