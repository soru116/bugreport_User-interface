import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
from run_api_argument_flow_pair import FrozenOllama
from fim_patch.backend import OllamaFIM
from fim_patch.shared_budget import SharedBudgetBackend,BudgetExceeded


class FrozenPairTests(unittest.TestCase):
    def backend(self,path):
        b=object.__new__(FrozenOllama)
        b.expected=dict(prefix='p',suffix='s',seed=44,output_limit=456)
        b.new_attempts=0;b.journal=path;b.calls=[]
        b.post=lambda endpoint,payload:b.calls.append(endpoint)
        return b

    def test_changed_prompt_does_not_dispatch_or_write_started_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            b=self.backend(Path(temp)/'dispatch.json')
            with self.assertRaisesRegex(ValueError,'differs'):
                b.infill('changed','s',seed=44,max_new_tokens=456)
            self.assertEqual(b.calls,[]);self.assertFalse(b.journal.exists());self.assertEqual(b.new_attempts,0)

    def test_one_new_call_and_shared_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            b=self.backend(Path(temp)/'dispatch.json');shared=SharedBudgetBackend(b,1)
            def fake(self,*args,**kwargs):self.post('generate',{});return {'middle':'x'}
            with patch.object(OllamaFIM,'infill',fake):
                shared.infill('p','s',seed=44,max_new_tokens=456)
                with self.assertRaises(BudgetExceeded):shared.infill('p','s',seed=44,max_new_tokens=456)
            self.assertEqual(b.calls,['generate']);self.assertEqual(shared.report()['backend_generate_dispatches'],1)
            self.assertTrue(b.journal.exists())

    def test_timeout_preserves_spent_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            b=self.backend(Path(temp)/'dispatch.json')
            with patch.object(OllamaFIM,'infill',side_effect=TimeoutError('mock timeout')):
                with self.assertRaises(TimeoutError):b.infill('p','s',seed=44,max_new_tokens=456)
            self.assertTrue(b.journal.exists());self.assertEqual(b.new_attempts,1)
            with self.assertRaisesRegex(ValueError,'exhausted'):b.infill('p','s',seed=44,max_new_tokens=456)


if __name__=='__main__':unittest.main()
