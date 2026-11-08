"""Expression holes are inline, bounded and reconstructed without source loss."""
import ast
import json
import tempfile
import unittest
from pathlib import Path
from tests import test_local_fim as fixtures
from tests.test_fim import FakeBackend
from fim_patch.generator import FIMPatchGenerator, git
from fim_patch.local_fim import normalize_expression
from fim_patch.iterative import IterativeRepairGenerator
from fim_patch.cli import validate_generation_args
from scripts.run_stage4_batch import build_parser,make_generator,build_run_config,validate_resume_config


class ExpressionFIMTests(unittest.TestCase):
    setUp = fixtures.LocalFIMTests.setUp
    replace_source = fixtures.LocalFIMTests.replace_source

    def generator(self, outputs, **kwargs):
        return fixtures.LocalFIMTests.generator(self,outputs,local_fim_expressions=True,**kwargs)

    def test_inline_assignment_preserves_docstring_and_executable_source(self):
        b,g=self.generator(['cleaned.split(delimiter or ",")\n'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertEqual(r['span']['generation_scope'],'python_expression')
        self.assertEqual(r['output_normalization'],'single-expression-v2')
        self.assertTrue(b.prompts[0][0].endswith('    parsed = '))
        self.assertTrue(b.prompts[0][1].startswith('\n    return parsed'))
        self.assertIn('second documentation line.',b.prompts[0][0])
        self.assertNotIn('trailing_newline_added',r['candidates'][0])
        git(self.repo,'apply',data=r['patch'].encode())
        after=(self.repo/'app.py').read_bytes()
        self.assertEqual(after,self.raw.replace(b'cleaned.split(",")',b'cleaned.split(delimiter or ",")'))
        ns={};exec(after,ns);self.assertEqual(ns['parse'](' a;b ',';'),['a','b'])

    def test_if_condition_keeps_complete_original_body_in_suffix(self):
        raw=b'def parse(value):\n    """Keep this documentation."""\n    if value is None:\n        return "missing"\n    return value.strip()\n'
        self.replace_source(raw,'parse',1,5)
        self.ticket['description']='None and empty strings should be missing.'
        b,g=self.generator(['value is None or value == ""'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertTrue(b.prompts[0][0].endswith('    if '))
        self.assertTrue(b.prompts[0][1].startswith(':\n        return "missing"'))
        git(self.repo,'apply',data=r['patch'].encode())
        self.assertEqual((self.repo/'app.py').read_bytes(),raw.replace(b'value is None:',b'value is None or value == "":'))

    def test_expression_rejects_statements_comments_and_incomplete_output(self):
        for output in ('parsed = cleaned.split(delimiter)',
                       'cleaned.split(delimiter)\nreturn parsed',
                       'cleaned.split(delimiter) # explain',
                       'def parse(value):\n    return value',
                       'cleaned.split('):
            _,g=self.generator([output]);r=g.generate(self.ticket,self.loc,self.repo)
            self.assertFalse(r['generated_before_validation'],output)
            self.assertIn('expression_contract_violation',r['candidates'][0]['reason'])
            self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_noop_undefined_helper_and_length_are_still_rejected(self):
        for output,reason,stop in [('cleaned.split(",")','no_op',None),
                                   ('unknown_parser(cleaned)','undefined',None),
                                   ('cleaned.split(delimiter)','requires model end marker','length')]:
            b,g=self.generator([output])
            if stop:b.stop_reason=stop
            r=g.generate(self.ticket,self.loc,self.repo)
            self.assertFalse(r['generated_before_validation'])
            self.assertIn(reason,r['candidates'][0]['reason'])

    def test_incompatible_same_file_helper_call_is_not_accepted(self):
        raw=b'def parse(value, delimiter=None):\n    parsed = split(value)\n    return parsed\n\ndef split(value):\n    return value.split(",")\n'
        self.replace_source(raw,'parse',1,3)
        self.ticket['description']='split should use delimiter.'
        _,g=self.generator(['split(value, delimiter)'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertFalse(r['generated_before_validation'])
        self.assertIn('helper_call_incompatible',r['candidates'][0]['reason'])

    def test_unicode_byte_columns_crlf_bom_and_inline_comment_are_preserved(self):
        raw=('\ufeffdef parse(value, delimiter=None):\r\n    """原始說明。"""\r\n'
             '    名稱 = value.split(",")  # 原始註解\r\n    return 名稱\r\n').encode('utf8')
        self.replace_source(raw,'parse',1,4)
        self.ticket['description']='split should use delimiter.'
        b,g=self.generator(['value.split(delimiter or ",")'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertTrue(b.prompts[0][0].endswith('    名稱 = '))
        self.assertTrue(b.prompts[0][1].startswith('  # 原始註解'))
        git(self.repo,'apply',data=r['patch'].encode())
        self.assertEqual((self.repo/'app.py').read_bytes(),raw.replace(b'value.split(",")',b'value.split(delimiter or ",")'))

    def test_multiline_expression_inside_existing_parentheses(self):
        raw=b'def parse(value, delimiter=None):\n    parsed = (\n        value.split(\n            ","\n        )\n    )\n    return parsed\n'
        self.replace_source(raw,'parse',1,7)
        b,g=self.generator(['value.split(\n    delimiter or ","\n)'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertTrue(b.prompts[0][0].endswith('    parsed = (\n        '))
        self.assertTrue(b.prompts[0][1].startswith('\n    )'))
        git(self.repo,'apply',data=r['patch'].encode())
        ast.parse((self.repo/'app.py').read_text())

    def test_method_keeps_class_header_and_inline_skeleton(self):
        raw=b'class Parser:\n    def parse(self, value, delimiter=None):\n        """Keep method documentation."""\n        parsed = value.split(",")\n        return parsed\n'
        self.replace_source(raw,'Parser.parse',2,5)
        b,g=self.generator(['value.split(delimiter or ",")'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertIn('class Parser:\n    def parse',b.prompts[0][0])
        self.assertTrue(b.prompts[0][0].endswith('        parsed = '))
        self.assertEqual(r['prompt_context']['parent_headers'],['Parser'])
        git(self.repo,'apply',data=r['patch'].encode())
        ns={};exec((self.repo/'app.py').read_bytes(),ns)
        self.assertEqual(ns['Parser']().parse('a;b',';'),['a','b'])

    def test_parent_header_does_not_duplicate_method_decorator(self):
        raw=b'class Parser:\n    @staticmethod\n    def parse(value, delimiter=None):\n        """Keep documentation."""\n        parsed = value.split(",")\n        return parsed\n'
        self.replace_source(raw,'Parser.parse',3,6)
        b,g=self.generator(['value.split(delimiter or ",")'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertEqual(b.prompts[0][0].count('@staticmethod'),1)

    def test_ambiguous_selection_fallback_keeps_original_docstring_out_of_middle(self):
        self.ticket['description']='Unexpected behavior.';self.ticket['title']='Bug'
        b,g=self.generator(['    cleaned = value.strip()\n    parsed = cleaned.split(delimiter or ",")\n    return parsed\n'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertEqual(r['expression_fim']['action'],'body_after_docstring')
        self.assertIn('second documentation line.',b.prompts[0][0])
        self.assertNotIn('-    """',r['patch'])
        git(self.repo,'apply',data=r['patch'].encode())
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw.replace(b'cleaned.split(",")',b'cleaned.split(delimiter or ",")'))

    def test_long_docstring_shortened_only_in_prompt(self):
        raw=b'def parse(value, delimiter=None):\n    """'+b'documentation '*160+b'"""\n    parsed = value.split(",")\n    return parsed\n'
        self.replace_source(raw,'parse',1,4)
        b,g=self.generator(['value.split(delimiter or ",")'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertEqual(r['prompt_context']['docstring_context'],'shortened')
        self.assertIn('documentation shortened in prompt',b.prompts[0][0])
        self.assertLessEqual(r['prompt_context']['source_context_chars'],3200)
        git(self.repo,'apply',data=r['patch'].encode())
        self.assertEqual((self.repo/'app.py').read_bytes(),raw.replace(b'value.split(",")',b'value.split(delimiter or ",")'))

    def test_retry_remains_expression_and_stops_after_valid_candidate(self):
        b,g=self.generator(['parsed = cleaned.split(delimiter)','cleaned.split(delimiter or ",")'])
        r=IterativeRepairGenerator(g,3).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,2)
        self.assertTrue(r['generated_before_validation'])
        self.assertTrue(b.prompts[1][0].endswith('    parsed = '))
        self.assertIn('expression_contract_violation',b.prompts[1][0])

    def test_option_and_resume_isolation(self):
        parser=build_parser();base=['--tickets','t','--localizations','l','--output','r','--prompt-layout','compact_fim','--candidates','1']
        with self.assertRaises(SystemExit):
            validate_generation_args(parser.parse_args(base+['--local-fim-expressions']),parser)
        with self.assertRaises(ValueError):
            FIMPatchGenerator(FakeBackend([]),local_fim_expressions=True)
        args=validate_generation_args(parser.parse_args(base+['--local-fim','--local-fim-expressions']),parser)
        self.assertTrue(make_generator(args).local_fim_expressions)
        config=build_run_config(args,[])
        self.assertEqual(config['local_fim_expressions'],'local-fim-expression-v2')
        self.assertEqual(config['output_normalization'],'scope-aware-expression-v2')
        self.assertTrue(config['prompt_version'].endswith('-local-fim-expression-v2-public-behavior-v1'))
        old=validate_generation_args(parser.parse_args(base+['--local-fim']),parser)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'run_config.json';path.write_text(json.dumps(build_run_config(old,[])),encoding='utf8')
            with self.assertRaises(ValueError):validate_resume_config(path,config)
            previous=dict(config)
            previous['prompt_version']=previous['prompt_version'].removesuffix('-public-behavior-v1')
            path.write_text(json.dumps(previous),encoding='utf8')
            with self.assertRaises(ValueError):validate_resume_config(path,config)

    def test_newline_expression_is_wrapped_without_adding_statements(self):
        self.assertIsInstance(ast.parse(normalize_expression('value\n + 1'),mode='eval').body,ast.BinOp)


if __name__ == '__main__':unittest.main()
