import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fim_patch.target_context import assess_target_context
from fim_patch.scope_compatibility import assess_scope
from fim_patch.unified_repair import RepairPolicy


SOURCE='class Stored:\n    @property\n    def size(self):\n        return self._buffer.size\n'
PUBLIC='### Expected behavior\nThe API should never silently create a corrupt record.\n### Reproduce\n```python\nfrom pkg import Producer\nobj = Producer(data=1)\n```\n'


class TargetContextTests(unittest.TestCase):
    def test_cross_owner_getter_is_risk_not_proof(self):
        r=assess_target_context(SOURCE,'Stored.size',PUBLIC)
        self.assertTrue(r['risk']);self.assertFalse(r['semantic_incompatibility_verified'])
        self.assertEqual(r['public_calls'][0]['api'],'pkg.Producer')
        self.assertEqual(r['evidence'][0]['kind'],'output_integrity')

    def test_renamed_symbols_same_generic_rule(self):
        r=assess_target_context(SOURCE.replace('Stored','Decoded').replace('size','length'),'Decoded.length',PUBLIC.replace('Producer','Encoder').replace('corrupt record','malformed stream'))
        self.assertTrue(r['risk'])

    def test_same_owner_remains_unknown(self):
        self.assertFalse(assess_target_context(SOURCE,'Stored.size',PUBLIC.replace('Producer','Stored'))['risk'])

    def test_locally_inherited_owner_is_not_blocked(self):
        source=SOURCE+'class Producer(Stored):\n    pass\n'
        self.assertFalse(assess_target_context(source,'Stored.size',PUBLIC)['risk'])

    def test_setter_and_normal_input_method_not_getter_risk(self):
        source='class Stored:\n    def size(self, data):\n        return data\n'
        self.assertFalse(assess_target_context(source,'Stored.size',PUBLIC)['risk'])

    def test_negated_conditional_or_vague_intent_unknown(self):
        for expected in ('The API should not raise an exception.', 'If data is invalid, the API should raise ValueError.', 'The API sometimes returns corrupt files.'):
            public=PUBLIC.replace('The API should never silently create a corrupt record.',expected)
            self.assertFalse(assess_target_context(SOURCE,'Stored.size',public)['risk'])

    def test_examples_never_executed_and_trace_not_intent(self):
        public=PUBLIC.replace('obj = Producer(data=1)','raise RuntimeError("do not execute")\nobj = Producer(data=1)')
        self.assertTrue(assess_target_context(SOURCE,'Stored.size',public)['risk'])
        self.assertFalse(assess_target_context(SOURCE,'Stored.size','```python\nfrom pkg import Producer\nobj=Producer()\n# should reject invalid input\n```')['risk'])

    def test_benchmark_fields_excluded_and_risk_recorded_without_expanding_body(self):
        spans={'body':dict(start=0,end=len(SOURCE))}
        r=assess_scope(SOURCE.encode(),'Stored.size',{'test_patch':PUBLIC,'patch':PUBLIC},spans)
        self.assertFalse(r['target_context']['risk'])
        r=assess_scope(SOURCE.encode(),'Stored.size',{'description':PUBLIC},spans)
        self.assertEqual(r['action'],'retain_order_with_target_warning')
        self.assertIsNone(r['recommended_scope'])

    def test_gate_requires_explicit_scope_check(self):
        self.assertFalse(RepairPolicy().stop_on_target_context_risk)
        with self.assertRaises(ValueError):RepairPolicy(stop_on_target_context_risk=True)
        with self.assertRaises(ValueError):RepairPolicy(scope_compatibility=True,stop_on_target_context_risk='yes')


class TargetGateIntegrationTests(unittest.TestCase):
    from tests.test_local_fim import LocalFIMTests as Fixture
    setUp=Fixture.setUp
    replace_source=Fixture.replace_source
    def test_opt_in_gate_returns_before_metadata_or_infill(self):
        from unittest.mock import patch
        from fim_patch.unified_repair import UnifiedRepairGenerator
        self.replace_source(SOURCE.encode(),'Stored.size',2,4)
        self.ticket['description']=PUBLIC
        backend,_=self.Fixture.generator(self,[])
        policy=RepairPolicy(scope_compatibility=True,stop_on_target_context_risk=True)
        with patch.object(backend,'describe',side_effect=AssertionError('must not call metadata')),patch.object(backend,'infill',side_effect=AssertionError('must not generate')):
            result=UnifiedRepairGenerator(backend,policy).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'blocked_invalid_context')
        self.assertEqual(result['explanation'],'target_context_unknown_under_opt_in_gate')
        self.assertEqual(result['unified_plan']['scope_order'],[])


if __name__=='__main__':unittest.main()
