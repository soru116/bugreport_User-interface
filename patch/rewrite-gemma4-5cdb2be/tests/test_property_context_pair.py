import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
from run_property_context_pair import canonical
from run_api_argument_flow_pair import FrozenOllama
from fim_patch.property_context_integration import first_context
from fim_patch.backend import OllamaFIM
from fim_patch.shared_budget import SharedBudgetBackend,BudgetExceeded


class PairTests(unittest.TestCase):
    def test_policy_tuple_freezes_as_same_json(self):
        import json
        config={'policies':{'context_on':{'domain_adapters':(), 'property_context':True}}}
        self.assertEqual(canonical(config),json.loads(json.dumps(config)))

    def test_inserted_context_is_checked_before_single_mock_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            b=object.__new__(FrozenOllama);b.num_ctx=1000;b.max_new_tokens=20;b.new_attempts=0
            class Counter:
                def count(self,p,s):return len(p)+len(s)
            b._token_counter=Counter();b.journal=Path(temp)/'dispatch.json';b.calls=[]
            context=dict(rendered='# Facts\n# Observed self.value -> getter Owner.value\n# L1 return: 1\n')
            b.expected=dict(prefix=context['rendered']+'p',suffix='s',seed=42,output_limit=20)
            b.post=lambda endpoint,payload:b.calls.append(endpoint)
            def fake(self,*args,**kwargs):self.post('generate',{});return {'middle':'x'}
            with patch.object(OllamaFIM,'infill',fake),first_context(b,context,{}):
                budget=SharedBudgetBackend(b,1)
                budget.infill('p','s',seed=42,max_new_tokens=20)
                with self.assertRaises(BudgetExceeded):budget.infill('p','s',seed=42,max_new_tokens=20)
            self.assertEqual(b.calls,['generate']);self.assertEqual(budget.report()['backend_generate_dispatches'],1)


if __name__=='__main__':unittest.main()
