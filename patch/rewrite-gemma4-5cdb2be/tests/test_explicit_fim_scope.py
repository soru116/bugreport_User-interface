import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from tests import test_local_fim as fixtures
from tests.test_fim import FakeBackend
from fim_patch.generator import FIMPatchGenerator,git
from fim_patch.iterative import IterativeRepairGenerator
from fim_patch.cli import validate_generation_args
from scripts.run_stage4_batch import build_parser,build_run_config,validate_resume_config


class ExplicitScopeTests(unittest.TestCase):
    setUp=fixtures.LocalFIMTests.setUp
    replace_source=fixtures.LocalFIMTests.replace_source

    def generator(self,outputs,scope='body',**kwargs):
        return fixtures.LocalFIMTests.generator(self,outputs,fim_scope=scope,fim_output_ceiling=128,research_mode=True,**kwargs)

    def test_body_preserves_documentation_signature_and_executes_fixture(self):
        backend,generator=self.generator(['    cleaned = value.strip()\n    parsed = cleaned.split(delimiter or ",")\n    return parsed\n'])
        self.ticket['expected_behavior']='Use the supplied delimiter.'
        result=generator.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'generated',result)
        self.assertEqual(result['fim_scope'],'body')
        self.assertEqual(backend.budgets,[128])
        self.assertIn('Use the supplied delimiter.',backend.prompts[0][0])
        self.assertIn('second documentation line.',backend.prompts[0][0])
        self.assertTrue(result['local_fim_selection']['docstring_preserved'])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
        git(self.repo,'apply',data=result['patch'].encode())
        after=(self.repo/'app.py').read_bytes()
        self.assertIn(b'    """A long irrelevant docstring.\n    second documentation line.\n    """\n',after)
        self.assertTrue(after.endswith(b'def unrelated():\n    return 123\n'))
        ns={};exec(after,ns);self.assertEqual(ns['parse'](' a;b ',';'),['a','b'])

    def test_body_original_still_noop_and_overrun_is_rejected(self):
        for middle,reason in [('    cleaned = value.strip()\n    parsed = cleaned.split(",")\n    return parsed\n','no_op'),
                              ('    return value\ndef extra():\n    return 1\n','body_contract_violation')]:
            _,generator=self.generator([middle])
            result=generator.generate(self.ticket,self.loc,self.repo)
            self.assertEqual(result['static_candidate_count'],0)
            self.assertIn(reason,result['candidates'][0]['reason'])
            self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_statement_does_not_accept_two_sibling_statements(self):
        _,generator=self.generator(['    parsed = cleaned.split(delimiter)\n    other = 1\n'],scope='statement')
        result=generator.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['static_candidate_count'],0)
        self.assertIn('statement_scope_escape',result['candidates'][0]['reason'])

    def test_locked_target_does_not_fall_back_when_body_exceeds_limit(self):
        backend,generator=self.generator([],max_body_lines=2)
        self.loc['stage3_ranked_symbols'].append(dict(file_path='app.py',symbol_qualified_name='unrelated',start_line=11,end_line=12))
        result=generator.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(backend.calls,0)
        self.assertEqual(result['patch_status'],'blocked_invalid_context')
        self.assertIn('locked scope exceeds',result['explanation'])
        self.assertEqual(result['target_resolution'][0]['status'],'selected')

    def test_crlf_unicode_documentation_and_decorator_stay_outside_hole(self):
        raw=('def marker(f):\r\n    return f\r\n\r\n@marker\r\ndef parse(value, delimiter=None):\r\n'
             '    """文件說明。"""\r\n    parsed = value.split(",")\r\n    return parsed\r\n').encode('utf8')
        self.replace_source(raw,'parse',5,8)
        _,generator=self.generator(['    parsed = value.split(delimiter or ",")\n    return parsed\n'])
        result=generator.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'generated',result)
        git(self.repo,'apply',data=result['patch'].encode())
        self.assertEqual((self.repo/'app.py').read_bytes(),raw.replace(b'value.split(",")',b'value.split(delimiter or ",")'))

    def test_config_tracks_scope_ceiling_and_isolates_resume(self):
        parser=build_parser()
        base=['--tickets','t','--localizations','l','--output','out','--prompt-layout','compact_fim',
              '--research-mode','--candidates','1','--fim-output-ceiling','128']
        configs=[]
        for scope in ('expression','body'):
            args=validate_generation_args(parser.parse_args(base+['--fim-scope',scope]),parser)
            configs.append(build_run_config(args,[]))
            self.assertEqual(configs[-1]['fim_scope'],scope)
            self.assertEqual(configs[-1]['fim_output_ceiling'],128)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'run_config.json';path.write_text(json.dumps(configs[0]),encoding='utf8')
            with self.assertRaises(ValueError):validate_resume_config(path,configs[1])
        for extra in (['--fim-scope','body','--repair-rounds','2'],['--fim-scope','body','--local-fim-expressions'],
                      ['--fim-scope','body','--fim-output-ceiling','127']):
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                validate_generation_args(parser.parse_args(base+extra),parser)

    def test_explicit_scope_outer_retries_and_bad_ceiling_rejected(self):
        _,generator=self.generator([])
        with self.assertRaises(ValueError):IterativeRepairGenerator(generator,2)
        with self.assertRaises(ValueError):FIMPatchGenerator(FakeBackend([]),fim_scope='body',fim_output_ceiling=127,research_mode=True,candidates=1)


if __name__=='__main__':unittest.main()
