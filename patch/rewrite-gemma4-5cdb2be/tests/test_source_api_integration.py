import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parents[1])]
from fim_patch.source_api_integration import first_context,SourceAPIAdapter,check_candidate
from fim_patch.shared_budget import SharedBudgetBackend,BudgetExceeded
from fim_patch.unified_repair import RepairPolicy,UnifiedRepairGenerator
from tests.test_existing_test_context import Backend
from tests import test_unified_repair as fixtures


class ContextIntegrationTests(unittest.TestCase):
    def test_first_only_unchanged_suffix_seed_output_and_ledger(self):
        b=Backend();r={}
        with first_context(b,{'compact_context':'# API function(x)\n'},r):
            shared=SharedBudgetBackend(b,2)
            shared.infill('prefix','suffix',seed=42,max_new_tokens=20)
            shared.infill('prefix','suffix',seed=43,max_new_tokens=20)
            with self.assertRaises(BudgetExceeded):shared.infill('prefix','suffix')
        self.assertTrue(r['injected']);self.assertIn('SOURCE API EVIDENCE',b.calls[0][0])
        self.assertEqual(b.calls[1][0],'prefix');self.assertEqual(b.calls[0][1],'suffix')
        self.assertEqual(b.calls[0][2],dict(seed=42,max_new_tokens=20))
        self.assertEqual(shared.report()['infill_attempts'],2);self.assertNotIn('infill',b.__dict__)
        self.assertEqual(r['context_block']+'prefix',b.calls[0][0])

    def test_whole_context_omission_and_exception_restore(self):
        b=Backend();b.num_ctx=50;r={}
        with first_context(b,{'compact_context':'# '+('x'*100)+'\n'},r):
            b.infill('p','s',max_new_tokens=20)
        self.assertEqual(b.calls[0][0],'p');self.assertFalse(r['injected'])
        with self.assertRaises(RuntimeError):
            with first_context(b,{'compact_context':'# f(x)\n'},{}):raise RuntimeError('offline')
        self.assertNotIn('infill',b.__dict__)

    def test_unknown_signatures_leave_request_exact(self):
        b=Backend();r={}
        with first_context(b,{'compact_context':''},r):b.infill('p','s',seed=9)
        self.assertEqual(b.calls,[('p','s',{'seed':9})]);self.assertFalse(r['injected'])

    def test_base_budget_exceeded_does_not_dispatch(self):
        b=Backend();b.num_ctx=10
        with first_context(b,{'compact_context':'# f(x)\n'},{}):
            with self.assertRaises(ValueError):b.infill('p','s',max_new_tokens=20)
        self.assertEqual(b.calls,[])

    def test_unknown_gate_is_not_passed_and_rejection_feedback(self):
        class G:pass
        g=G();g.source_api_checker=lambda p:dict(rejected=False,counts={'unknown':1},unknown_is_passed=False)
        c={};self.assertTrue(check_candidate(g,c,'diff'))
        self.assertFalse(c['source_api_contract']['unknown_is_passed'])
        g.source_api_checker=lambda p:dict(rejected=True,feedback='unexpected keyword copy')
        c={};self.assertFalse(check_candidate(g,c,'diff'));self.assertIn('copy',c['failure_feedback'])

    def test_policy_is_explicit_bool(self):
        self.assertFalse(RepairPolicy().source_api_contracts)
        with self.assertRaises(ValueError):RepairPolicy(source_api_contracts=1)


class GeneratorIntegrationTests(unittest.TestCase):
    setUp=fixtures.UnifiedTests.setUp
    replace_source=fixtures.UnifiedTests.replace_source
    engine=fixtures.UnifiedTests.engine
    def test_real_generator_rejects_before_tests_and_retries_with_shared_cap(self):
        from fim_patch.generator import git
        (self.repo/'apis.py').write_text('def normalize(value, *, mode=None):\n    return value\n')
        git(self.repo,'add','apis.py')
        raw=b'from apis import normalize\n\ndef parse(value):\n    return normalize(value)\n'
        self.replace_source(raw,'parse',3,4)
        self.ticket['description']='Repair parse while preserving normalize API.'
        backend,unused=self.engine(['    return normalize(value, bad=True)\n','    return normalize(value, mode="safe")\n'],limit=2)
        backend.num_ctx=16000
        class Body(UnifiedRepairGenerator):
            def plan(this,*args):
                result=super().plan(*args)
                if result['patch_status']=='planned':result['unified_plan']['scope_order']=['body']
                return result
        engine=Body(backend,RepairPolicy(max_generation_calls=2,source_api_contracts=True))
        with patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')):
            result=engine.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['shared_generation_budget']['infill_attempts'],2)
        first=result['repair_history'][0]['candidates'][0]
        self.assertEqual(first['reason'],'source_api_call_incompatible')
        self.assertEqual(first['source_api_contract']['counts']['known_incompatible'],1)
        self.assertIn('bad',backend.prompts[1][0])
        self.assertTrue(result['source_api_context']['injected'])
        self.assertEqual((self.repo/'app.py').read_bytes(),raw)
        self.assertNotIn('official_resolved',result)
