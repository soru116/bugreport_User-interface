import ast
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parents[1])]
from fim_patch.backend import GenerationError
from fim_patch.body_boundary import extract_body_with_echo,ECHO_HEADER
from fim_patch.none_contract import assess_none_contract,failure_feedback
from fim_patch.generator import FIMPatchGenerator,git
from fim_patch.iterative import IterativeRepairGenerator
from fim_patch.cli import validate_generation_args
from scripts.run_stage4_batch import build_parser,build_run_config,validate_resume_config
from tests import test_fim

TAIL='\n'+ECHO_HEADER+'\n# Title: repeated task\n# --- Expected behaviour ---\n# ```\n# partial echo\n'
PROMPT=ECHO_HEADER+'\n# Title: task\n'


class EchoBoundaryTests(unittest.TestCase):
    def test_complete_body_and_comment_only_echo(self):
        body='    return x\n'
        kept,evidence=extract_body_with_echo(body+TAIL,'def f(x):\n',0,PROMPT)
        self.assertEqual(kept.strip(),body.strip())
        self.assertEqual(evidence['boundary_evidence'],'outer_comment_only_prompt_echo')

    def test_untrusted_boundaries_and_incomplete_source_are_rejected(self):
        examples=['    return x\n', '    return (\n'+TAIL,
                  '    if x:\n'+TAIL,'    return x\nchanged = 1\n'+TAIL,
                  '    return x\n'+TAIL+'evil()\n',
                  '    return """\n'+ECHO_HEADER+'\n# Title: fake\n# --- Expected behaviour ---\n',
                  '    return x\n'+TAIL.replace(ECHO_HEADER,'# unrelated header'),
                  '    return x\n'+TAIL.replace('# Title: repeated task','# arbitrary'),
                  '    return x\n'+TAIL.replace('# --- Expected behaviour ---','# random heading')]
        for text in examples:
            with self.subTest(text=text),self.assertRaises((ValueError,SyntaxError)):
                extract_body_with_echo(text,'def f(x):\n',0,PROMPT)

    def test_indented_comment_is_not_a_witness(self):
        text='    return x\n'+''.join('    '+line for line in TAIL.splitlines(keepends=True))
        with self.assertRaises(ValueError):extract_body_with_echo(text,'def f(x):\n',0,PROMPT)

    def test_earlier_sibling_boundary_remains_supported(self):
        middle,proof=extract_body_with_echo('    return x\n\ndef g():\n    return (','def f(x):\n',0,PROMPT)
        self.assertEqual(middle.strip(),'return x')
        self.assertEqual(proof['boundary_evidence'],'sibling_definition')

    def test_marker_inside_body_stays_rejected_even_with_sibling(self):
        with self.assertRaises(ValueError):
            extract_body_with_echo('    return "<MID>"\n\ndef g():\n    pass\n','def f():\n',0,PROMPT)

    def test_method_crlf_and_unicode_context(self):
        text='        return "測試"\r\n'+TAIL.replace('\n','\r\n')
        middle,proof=extract_body_with_echo(text,'class A:\r\n    def f(self):\r\n',4,PROMPT)
        self.assertIn('測試',middle)
        self.assertEqual(proof['boundary_evidence'],'outer_comment_only_prompt_echo')


class NoneProbeTests(unittest.TestCase):
    original='def f(param, handler):\n    if param is None:\n        return None\n    if handler is None:\n        return None\n    return handler(param.value)\n'

    def test_source_parameters_discovered_without_case_or_field_names(self):
        modified='def f(param, handler):\n    return handler(param.value)\n'
        result=assess_none_contract(self.original,modified,'f')
        self.assertEqual(result['failed_parameters'],['handler','param'])
        self.assertEqual(result['status'],'failed')
        self.assertIn('param=None',failure_feedback(result))
        self.assertFalse(result['official_tests_run'])

    def test_guard_and_getattr_rewrites_can_preserve_none_cases(self):
        self.assertEqual(assess_none_contract(self.original,self.original,'f')['status'],'passed_bounded')
        changed='def f(param, handler):\n    if not callable(handler):\n        return None\n    value = getattr(param, "value", None)\n    if value is None:\n        return None\n    return handler(value)\n'
        self.assertEqual(assess_none_contract(self.original,changed,'f')['status'],'passed_bounded')

    def test_unsupported_logic_is_unknown_and_not_executed(self):
        modified='def f(param, handler):\n    return __import__("os").system("must_not_execute")\n'
        with patch('os.system',side_effect=AssertionError('must not execute')):
            result=assess_none_contract(self.original,modified,'f')
        self.assertEqual(result['status'],'unknown_not_checked')
        self.assertEqual(result['failed_parameters'],[])

    def test_absent_none_guards_do_not_invent_tests(self):
        source='def f(x):\n    return x\n'
        self.assertEqual(assess_none_contract(source,source,'f')['cases'],[])

    def test_shadowed_helper_is_not_assumed_stdlib_copy(self):
        source='from copy import deepcopy\ndef deepcopy(x):\n    raise RuntimeError()\ndef f(x, other):\n    if x is None:\n        return deepcopy(other)\n    return x\n'
        modified=source.replace('return deepcopy(other)','return x.missing')
        result=assess_none_contract(source,modified,'f')
        self.assertEqual(result['status'],'unknown_not_checked')
        self.assertEqual(result['failed_parameters'],[])


class SafeBodyIntegrationTests(unittest.TestCase):
    def setUp(self):
        test_fim.PatchTests.setUp(self)
        self.raw=NoneProbeTests.original.encode()
        (self.repo/'app.py').write_bytes(self.raw)
        git(self.repo,'add','app.py');git(self.repo,'-c','user.name=T','-c','user.email=t@invalid','commit','-m','fixture')
        base=git(self.repo,'rev-parse','HEAD').decode().strip()
        self.ticket.update(base_commit=base,title='Handle a value',expected_behavior='Return a processed value for ordinary input.')
        self.loc.update(base_commit=base,stage3_ranked_symbols=[dict(file_path='app.py',symbol_qualified_name='f',start_line=1,end_line=6)])

    def backend(self,raw):
        class Reply(test_fim.FakeBackend):
            def infill(this,prefix,suffix,**kwargs):
                this.calls+=1;this.prompts.append(prefix);this.budgets.append(kwargs['max_new_tokens'])
                raise GenerationError('non_code_output',dict(response=raw,done=True,done_reason='length'))
        backend=Reply([]);backend.prompts=[]
        return backend

    def generator(self,backend,enabled=True):
        return FIMPatchGenerator(backend,candidates=1,research_mode=True,prompt_layout='compact_fim',
            fim_scope='body',fim_output_ceiling=128,safe_body_feedback=enabled,helper_contracts=True)

    def test_recovered_static_patch_retained_but_none_regression_rejected(self):
        raw='    value = param.value\n    if value is not None:\n        return handler(value)\n    return None\n'+TAIL
        backend=self.backend(raw)
        result=IterativeRepairGenerator(self.generator(backend),2).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(backend.calls,2)
        self.assertEqual(backend.budgets,[128,128])
        self.assertEqual(result['patch_status'],'no_valid_candidate')
        self.assertEqual(result['static_candidate_count'],1)
        self.assertEqual(result['candidates'][0]['reason'],'source_none_regression')
        self.assertEqual(result['candidates'][0]['raw_response'],raw)
        self.assertIn('handler=None',backend.prompts[1])
        self.assertIn('param=None',backend.prompts[1])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_disabled_mode_preserves_backend_rejection(self):
        backend=self.backend('    return None\n'+TAIL)
        result=self.generator(backend,False).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['static_candidate_count'],0)
        self.assertEqual(result['candidates'][0]['reason'],'non_code_output')

    def test_noop_and_docstring_only_echoes_are_not_candidates(self):
        original=''.join(self.raw.decode().splitlines(keepends=True)[1:])
        for body in (original,'    """New documentation."""\n'+original):
            result=self.generator(self.backend(body+TAIL)).generate(self.ticket,self.loc,self.repo)
            self.assertEqual(result['static_candidate_count'],0)
            self.assertTrue(result['candidates'][0]['reason'].startswith(('no_op','no_executable_change')))

    def test_backend_environment_error_stops_without_retry(self):
        class Down(test_fim.FakeBackend):
            def infill(this,*a,**kw):
                this.calls+=1
                raise OSError('Connection refused')
        backend=Down([])
        result=IterativeRepairGenerator(self.generator(backend),2).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(backend.calls,1)
        self.assertEqual(result['repair_stop_reason'],'backend_environment_error')

    def test_config_isolation_and_two_round_limit(self):
        parser=build_parser()
        base=['--tickets','t','--localizations','l','--output','o','--research-mode','--prompt-layout','compact_fim',
              '--candidates','1','--fim-scope','body','--fim-output-ceiling','128']
        plain=build_run_config(validate_generation_args(parser.parse_args(base),parser),[])
        safe=build_run_config(validate_generation_args(parser.parse_args(base+['--safe-body-feedback','--repair-rounds','2']),parser),[])
        self.assertIn('safe-body-feedback-v1',safe['prompt_version'])
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'run_config.json';path.write_text(json.dumps(plain),encoding='utf8')
            with self.assertRaises(ValueError):validate_resume_config(path,safe)
        with self.assertRaises(ValueError):IterativeRepairGenerator(self.generator(self.backend('')),3)


if __name__=='__main__':unittest.main()
