import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fim_patch.api_source_index import SourceIndex
from fim_patch.api_argument_flow import assess_argument_flow


class ArgumentFlowTests(unittest.TestCase):
    def check(self,body,api="def parse(value, *parts):\n value = value + '.'\n return value.split('.')\n",head=''):
        before=head+'from .api import parse\ndef entry(value):\n return value\n'
        after=head+'from .api import parse\ndef entry(value):\n'+body
        files={'pkg/__init__.py':b'','pkg/api.py':api.encode(),'pkg/entry.py':before.encode()}
        return assess_argument_flow(SourceIndex(files,files.__getitem__),'pkg/entry.py',before,after,'entry')

    def test_leading_argument_survives_starred_tail_as_advisory(self):
        r=self.check(" module = __import__('pkg')\n return parse(module, *value)\n")
        call=next(c for c in r['calls'] if c['call']=='parse')
        self.assertEqual(call['status'],'potential_operation_mismatch')
        self.assertTrue(call['unpacking_present']);self.assertFalse(call['binding_verified'])
        self.assertIn('module_return',r['feedback_preview']);self.assertFalse(r['automatic_rejection'])

    def test_conditional_origin_preserves_unknown_path(self):
        r=self.check(" if value:\n  value = __import__('pkg')\n return parse(value)\n")
        call=next(c for c in r['calls'] if c['call']=='parse')
        self.assertEqual({o['kind'] for o in call['arguments'][0]['origins']},{'unknown','module_return'})
        self.assertFalse(call['arguments'][0]['all_paths_verified'])

    def test_alias_then_overwrite_clears_old_module_origin(self):
        r=self.check(" value = __import__('pkg')\n alias = value\n alias = 'ok'\n return parse(alias)\n")
        self.assertEqual(r['potential_mismatches'],0)

    def test_import_shadowing_not_inferred(self):
        r=self.check(" __import__ = value\n module = __import__('pkg')\n return parse(module)\n")
        self.assertEqual(r['potential_mismatches'],0)

    def test_dynamic_namespace_disables_builtin_provenance(self):
        r=self.check(" exec(value)\n module = __import__('pkg')\n return parse(module)\n")
        self.assertEqual(r['potential_mismatches'],0)

    def test_starred_first_and_callback_are_unknown(self):
        r=self.check(' return parse(*value)\n')
        self.assertEqual(r['potential_mismatches'],0)
        r=self.check(' parse = value\n return parse(1)\n')
        self.assertEqual(r['potential_mismatches'],0)

    def test_callee_changed_or_decorated_not_inferred(self):
        r=self.check(' return parse(1)\n',api='@decorator\ndef parse(value):\n return value.split()\n')
        self.assertEqual(r['potential_mismatches'],0)

    def test_operations_and_non_string_literal_are_evidence_not_proof(self):
        r=self.check(' return parse(123)\n')
        self.assertEqual(r['potential_mismatches'],1)
        self.assertIn('add_string_literal',r['feedback_preview'])
        self.assertTrue(r['advisory_only']);self.assertFalse(r['calls'][0]['runtime_type_verified'])

    def test_unrecognized_operation_and_parameter_stay_unknown(self):
        r=self.check(' return parse(value)\n')
        self.assertEqual(r['potential_mismatches'],0)

    def test_callee_conversion_does_not_imply_original_argument_requires_string(self):
        r=self.check(' return parse(1)\n',api='def parse(value):\n value = str(value)\n return value.split()\n')
        self.assertEqual(r['potential_mismatches'],0)
        r=self.check(' return parse(1)\n',api='def parse(value):\n if flag:\n  value = str(value)\n return value.split()\n')
        self.assertEqual(r['potential_mismatches'],0)
        r=self.check(' return parse(1)\n',api='def parse(value):\n return value.custom()\n')
        self.assertEqual(r['potential_mismatches'],0)

    def test_loop_assignment_and_try_merge_are_conservative(self):
        r=self.check(" for value in items:\n  value = __import__('pkg')\n return parse(value)\n")
        self.assertEqual(r['potential_mismatches'],0)
        r=self.check(" try:\n  value = __import__('pkg')\n except Exception:\n  value = callback()\n return parse(value)\n")
        call=next(c for c in r['calls'] if c['call']=='parse')
        self.assertEqual({o['kind'] for o in call['arguments'][0]['origins']},{'unknown','module_return'})

    def test_repository_source_not_executed_and_fim_tokens_sanitized(self):
        r=self.check(' return parse(1)\n',api="raise RuntimeError('do not execute')\ndef parse(value):\n return value.split('<MID>')\n")
        self.assertEqual(r['potential_mismatches'],1)
        self.assertFalse(r['source_executed']);self.assertNotIn('<MID>',r['feedback_preview'])


if __name__=='__main__':unittest.main()
