import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fim_patch.adaptive import call_chain, implementation_blocks
from fim_patch.generator import FIMPatchGenerator
from fim_patch.iterative import IterativeRepairGenerator
from tests import test_iterative_repair
from tests import test_fim


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        test_fim.PatchTests.setUp(self)

    backend = test_iterative_repair.RepairTests.backend

    def configure(self, outputs, rounds=3):
        backend = self.backend(outputs)
        generator = FIMPatchGenerator(backend,candidates=1,prompt_layout='compact_fim',
            fixed_signature_body=True,helper_contracts=True,helper_implementations=True)
        return backend, generator, IterativeRepairGenerator(generator,rounds,adaptive=True)

    def add_third_target(self):
        raw = self.raw + b'\ndef h():\n    return "same"\n'
        (self.repo/'app.py').write_bytes(raw)
        test_fim.git(self.repo,'add','app.py')
        test_fim.git(self.repo,'-c','user.name=T','-c','user.email=t@example.invalid','commit','-m','targets')
        base = test_fim.git(self.repo,'rev-parse','HEAD').decode().strip()
        self.ticket['base_commit']=base
        self.loc['base_commit']=base
        self.loc['stage3_ranked_symbols'] += [
            {'file_path':'app.py','symbol_qualified_name':'g','start_line':5,'end_line':6},
            {'file_path':'app.py','symbol_qualified_name':'h','start_line':8,'end_line':9}]
        return raw

    def test_noop_reselection_is_bounded_and_preserves_inputs(self):
        raw = self.add_third_target()
        before = copy.deepcopy(self.loc)
        backend,inner,adaptive = self.configure(['    return x.strip()\n','    return f(" hi ")\n','    return "same"\n'])
        result = adaptive.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(backend.calls,3)
        self.assertEqual([r['span']['symbol'] for r in result['repair_history']],['f','g','h'])
        self.assertEqual(result['repair_stop_reason'],'round_limit')
        self.assertEqual(result['adaptive_generation_limit'],3)
        self.assertEqual(self.loc,before)
        self.assertEqual((self.repo/'app.py').read_bytes(),raw)
        self.assertEqual(inner.seed,42)
        self.assertEqual(inner.extra_directive,'')

    def test_exhaustion_stops_without_repeating_same_target(self):
        backend,_,adaptive = self.configure(['    return x.strip()\n'])
        result = adaptive.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(backend.calls,1)
        self.assertEqual(result['repair_stop_reason'],'targets_exhausted')

    def test_format_feedback_retries_then_reselects_within_total_limit(self):
        self.add_third_target()
        wrong='def wrong(x):\n    return x\n'
        backend,_,adaptive = self.configure([wrong,wrong,'    return "fixed"\n'])
        result = adaptive.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'generated')
        self.assertEqual(backend.calls,3)
        self.assertEqual([r['span']['symbol'] for r in result['repair_history']],['f','f','g'])
        self.assertIn('body_contract_violation',backend.prompts[1])

    def test_backend_environment_error_stops_before_retry(self):
        backend,_,adaptive = self.configure([])
        def timeout(*args,**kwargs):
            backend.calls += 1
            raise ValueError('timed out')
        backend.infill=timeout
        result=adaptive.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(backend.calls,1)
        self.assertEqual(result['repair_stop_reason'],'backend_environment_error')

    def test_whole_helper_blocks_are_removed_to_fit_verified_token_budget(self):
        raw=b'def f(x):\n    return g()\n\ndef g():\n    return 1\n'
        (self.repo/'app.py').write_bytes(raw)
        test_fim.git(self.repo,'add','app.py')
        test_fim.git(self.repo,'-c','user.name=T','-c','user.email=t@example.invalid','commit','-m','budget')
        base=test_fim.git(self.repo,'rev-parse','HEAD').decode().strip()
        self.ticket['base_commit']=base;self.loc['base_commit']=base
        backend,_,adaptive=self.configure(['    return g()\n'],rounds=1)
        class Counter:
            def count(self,prefix,suffix):
                return 600 if 'helper implementation (source evidence)' in prefix else 200
        backend._token_counter=Counter();backend.num_ctx=500
        result=adaptive.generate(self.ticket,self.loc,self.repo)
        self.assertTrue(result['helper_implementation_audit']['fits'])
        self.assertEqual(result['helper_implementation_audit']['helpers'][0]['reason'],'token_budget')
        self.assertNotIn('helper implementation (source evidence)',backend.prompts[0])

    def test_call_chain_resolves_unshadowed_comprehension_helpers_only(self):
        source='def root(values):\n    return [helper(v) for v in values]\ndef helper(x):\n    return x\n'
        self.assertEqual([n.name for n,d in call_chain(source,'root')],['helper'])
        shadow=source.replace('helper(v) for v in values','helper(v) for helper in values')
        self.assertEqual(call_chain(shadow,'root'),[])

    def test_call_chain_is_bounded_and_cycle_safe(self):
        text='def a(x):\n    return b(x)\ndef b(x):\n    return c(x)\ndef c(x):\n    return a(x)\n'
        self.assertEqual([(n.name,d) for n,d in call_chain(text,'a')],[('b',1),('c',2)])
        self.assertEqual(len(call_chain(text,'a',max_depth=1)),1)
        blocks,audit=implementation_blocks(text,'a',max_chars=10)
        self.assertEqual(blocks,[])
        self.assertTrue(all(not a['included'] for a in audit))

    def test_new_targets_come_from_verified_same_file_call_chain(self):
        raw=b'def f(x):\n    return helper(x)\n\ndef helper(x):\n    return x.strip()\n'
        (self.repo/'app.py').write_bytes(raw)
        test_fim.git(self.repo,'add','app.py')
        test_fim.git(self.repo,'-c','user.name=T','-c','user.email=t@example.invalid','commit','-m','chain')
        base=test_fim.git(self.repo,'rev-parse','HEAD').decode().strip()
        self.ticket['base_commit']=base;self.loc['base_commit']=base
        backend,_,adaptive=self.configure(['    return helper(x)\n','    return "" if x is None else x.strip()\n'])
        result=adaptive.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'generated')
        self.assertEqual(result['span']['symbol'],'helper')
        self.assertEqual(result['adaptive_decisions'][0]['origin'],'same_file_call_chain')
        self.assertIn('return x.strip()',backend.prompts[0])


if __name__ == '__main__':
    unittest.main()
