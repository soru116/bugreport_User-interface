import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fim_patch.output_scope_literals import diagnose


class ScopeLiteralTests(unittest.TestCase):
    def check(self,middle,ticket=None,base='def f():\n return data\n',scope='issue_statement'):
        return diagnose(middle,dict(generation_scope=scope,statement_kind='For'),base,ticket or {})

    def test_two_statements_scope_mismatch_not_auto_rewrite(self):
        r=self.check('for x in data:\n use(x)\nif flag:\n use(data)\n')
        self.assertEqual(r['scope']['statement_kinds'],['For','If'])
        self.assertEqual(r['scope']['status'],'scope_count_mismatch');self.assertFalse(r['patch_modified'])

    def test_one_different_statement_kind_not_forced_to_original(self):
        r=self.check('if flag:\n use(data)\n')
        self.assertEqual(r['scope']['status'],'scope_count_compatible');self.assertFalse(r['scope']['statement_kind_change_verified_safe'])

    def test_example_subscript_literal_risk(self):
        t=dict(description="Example:\n```python\ndata = {'toy_name': [1]}\n```\n")
        r=self.check("return data['toy_name']\n",t)
        self.assertEqual(r['literals'][0]['status'],'example_derived_literal_risk')
        self.assertIn('toy_name',r['feedback_preview']);self.assertFalse(r['automatic_rejection'])

    def test_explicit_expectation_mention_is_unknown_not_banned(self):
        t=dict(description="```python\ndata = {'toy_name': [1]}\n```",expected_behavior="Support the key 'toy_name'.")
        r=self.check("return data['toy_name']",t)
        self.assertEqual(r['literals'][0]['status'],'public_expectation_mention_unknown')
        self.assertEqual(r['feedback_preview'],'');self.assertFalse(r['literals'][0]['api_requirement_verified'])

    def test_existing_base_literal_not_a_new_risk_and_docstrings_do_not_support(self):
        t=dict(description="```python\ndata={'toy_name':1}\n```")
        self.assertEqual(self.check("return data['toy_name']",t,base="def f():\n return data['toy_name']\n")['literals'],[])
        r=self.check("return data['toy_name']",t,base="def f():\n '''toy_name'''\n return data\n")
        self.assertEqual(r['literals'][0]['status'],'example_derived_literal_risk')

    def test_new_generic_constant_and_non_index_overlap_not_banned(self):
        r=self.check("return 'new_protocol'",dict(description="```python\nx='new_protocol'\n```"))
        self.assertEqual(r['literals'][0]['status'],'example_overlap_unknown');self.assertEqual(r['feedback_preview'],'')
        self.assertEqual(self.check("return data['fresh']")['literals'][0]['status'],'new_literal_support_unknown')

    def test_traceback_and_hidden_fields_are_not_examples(self):
        t=dict(description="```\nTraceback (most recent call last):\nKeyError: 'hidden'\n```",test_patch="```python\nx='hidden'\n```",patch="```python\nx='hidden'\n```")
        r=self.check("return data['hidden']",t)
        self.assertFalse(r['literals'][0]['seen_in_public_example']);self.assertIn('unparseable_public_example',r['public_example_unknowns'])

    def test_repl_inputs_only(self):
        t=dict(description="```\n>>> x = {'toy': 1}\n'secret_output'\n```")
        r=self.check("return data['toy']",t)
        self.assertTrue(r['literals'][0]['seen_in_public_example'])
        self.assertFalse(self.check("return data['secret_output']",t)['literals'][0]['seen_in_public_example'])

    def test_parse_error_and_unsupported_scope_remain_unknown(self):
        self.assertEqual(self.check('if flag:\n')['scope']['status'],'syntax_error')
        self.assertEqual(self.check('arg=1',scope='parameter_fragment')['scope']['status'],'unknown')
        self.assertEqual(self.check('x + 1',scope='python_expression')['scope']['status'],'parsed_only')

    def test_markers_not_forwarded_source_never_executed(self):
        t=dict(description="```python\nx={'<MID>':1}\n```")
        r=self.check("return data['<MID>']",t,base="raise RuntimeError('must not execute')\ndef f():\n return data\n")
        self.assertNotIn('<MID>',r['feedback_preview']);self.assertFalse(r['source_executed'])


if __name__=='__main__':unittest.main()
