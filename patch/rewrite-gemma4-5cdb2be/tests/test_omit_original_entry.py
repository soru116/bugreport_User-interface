"""The cohort switch changes only original-copy inclusion and isolates resume."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from scripts.run_stage4_batch import build_parser,build_run_config,validate_resume_config
from fim_patch.cli import validate_generation_args
from fim_patch.generator import FIMPatchGenerator
from tests import test_local_fim as fixtures

spec=importlib.util.spec_from_file_location('omit_runner',ROOT/'experiments/eval36_v7/run.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class CohortEntryTests(unittest.TestCase):
    def command(self, omit=False, fim_scope=None, temperature=None, safe_body=False, contract_preview=False, contract_feedback=False):
        with tempfile.TemporaryDirectory() as temp:
            argv=['run.py','--model','codellama:13b-code','--local-fim',
                  '--local-fim-expressions','--prompt-layout','compact_fim',
                  '--output',str(Path(temp)/'out')]
            if omit:argv.append('--omit-original-body')
            if temperature is not None:argv += ['--temperature',str(temperature),'--expected-model-digest','offline-test']
            if safe_body:argv += ['--safe-body-feedback','--repair-rounds','2']
            if contract_preview:argv.append('--operation-contract-preview')
            if contract_feedback:
                argv.append('--contract-feedback')
                if not safe_body:argv += ['--repair-rounds','2']
            if fim_scope:
                argv.remove('--local-fim-expressions')
                argv += ['--fim-scope',fim_scope,'--fim-output-ceiling','128']
            body=json.dumps({'models':[{'name':'codellama:13b-code','digest':'offline-test'}]}).encode()
            with patch.object(sys,'argv',argv),patch.object(runner.urllib.request,'urlopen',return_value=io.BytesIO(body)), \
                 patch.object(runner.subprocess,'call',return_value=0) as call,contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as exit:
                    runner.main()
                self.assertEqual(exit.exception.code,0)
                command=call.call_args.args[0]
            return command

    def test_switch_preserves_other_forwarded_flags(self):
        control=self.command();omit=self.command(True)
        self.assertIn('--include-original-body',control)
        self.assertNotIn('--include-original-body',omit)
        # Temporary output locations differ; all other options are equal.
        def normalized(command):
            values=list(command)
            values[values.index('--output')+1]='OUTPUT'
            return [v for v in values if v!='--include-original-body']
        self.assertEqual(normalized(control),normalized(omit))
        self.assertIn('--local-fim-expressions',omit)

    def test_body_scope_and_ceiling_forwarded_without_expression_flag(self):
        command=self.command(True,'body')
        self.assertEqual(command[command.index('--fim-scope')+1],'body')
        self.assertEqual(command[command.index('--fim-output-ceiling')+1],'128')
        self.assertNotIn('--local-fim-expressions',command)
        self.assertNotIn('--include-original-body',command)

    def test_temperature_is_the_only_changed_child_sampling_option(self):
        control=self.command(True,'body');experiment=self.command(True,'body',0.8)
        self.assertEqual(control[control.index('--temperature')+1],'0.2')
        self.assertEqual(experiment[experiment.index('--temperature')+1],'0.8')
        for command in (control,experiment):
            command[command.index('--output')+1]='OUTPUT'
            command[command.index('--temperature')+1]='TEMP'
        self.assertEqual(control,experiment)

    def test_safe_body_flag_and_two_rounds_reach_child(self):
        command=self.command(True,'body',safe_body=True)
        self.assertIn('--safe-body-feedback',command)
        self.assertEqual(command[command.index('--repair-rounds')+1],'2')

    def test_contract_preview_is_forwarded_without_retry_or_sampling_changes(self):
        control=self.command(True,'body')
        opted=self.command(True,'body',contract_preview=True)
        self.assertIn('--operation-contract-preview',opted)
        opted.remove('--operation-contract-preview')
        for command in (control,opted):command[command.index('--output')+1]='OUT'
        self.assertEqual(control,opted)

    def test_contract_feedback_forwarding_owns_two_round_limit(self):
        command=self.command(True,'body',contract_preview=True,contract_feedback=True)
        self.assertIn('--contract-feedback',command)
        self.assertEqual(command[command.index('--repair-rounds')+1],'2')
        self.assertEqual(command[command.index('--fim-output-ceiling')+1],'128')

    def test_contract_feedback_without_preview_stops_before_network(self):
        with patch.object(sys,'argv',['run.py','--contract-feedback','--fim-scope','body','--repair-rounds','2']), \
             patch.object(runner.urllib.request,'urlopen') as network,contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):runner.main()
            network.assert_not_called()

    def test_bad_temperature_stops_before_network(self):
        for value in ('-1','nan','inf'):
            with patch.object(sys,'argv',['run.py','--temperature',value]), \
                 patch.object(runner.urllib.request,'urlopen') as network,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exit:runner.main()
                self.assertEqual(exit.exception.code,2)
                network.assert_not_called()

    def test_digest_mismatch_stops_before_creating_output_or_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'out'
            argv=['run.py','--model','codellama:13b-code','--expected-model-digest','different','--output',str(out)]
            body=json.dumps({'models':[{'name':'codellama:13b-code','digest':'installed'}]}).encode()
            with patch.object(sys,'argv',argv),patch.object(runner.urllib.request,'urlopen',return_value=io.BytesIO(body)), \
                 patch.object(runner.subprocess,'call') as call:
                with self.assertRaises(SystemExit) as exit:runner.main()
                self.assertIn('digest differs',str(exit.exception))
                call.assert_not_called()
                self.assertFalse(out.exists())

    def test_invalid_two_hole_or_nonlocal_switch_stops_before_network(self):
        for args in (['--omit-original-body'],['--omit-original-body','--scope-adjustment',
                      '--local-fim','--local-fim-expressions','--prompt-layout','compact_fim']):
            with patch.object(sys,'argv',['run.py',*args]),patch.object(runner.urllib.request,'urlopen') as network, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exit:runner.main()
                self.assertEqual(exit.exception.code,2)
                network.assert_not_called()

    def test_config_and_resume_already_distinguish_copy_inclusion(self):
        parser=build_parser()
        base=['--tickets','t','--localizations','l','--output','out','--local-fim',
              '--local-fim-expressions','--prompt-layout','compact_fim']
        omit=build_run_config(validate_generation_args(parser.parse_args(base),parser),[])
        original=build_run_config(validate_generation_args(parser.parse_args(base+['--include-original-body']),parser),[])
        self.assertFalse(omit['include_original_body'])
        self.assertTrue(original['include_original_body'])
        self.assertNotEqual(omit['prompt_version'],original['prompt_version'])
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'run_config.json';path.write_text(json.dumps(original),encoding='utf8')
            with self.assertRaises(ValueError):validate_resume_config(path,omit)


class OmitCopyIntegrationTests(unittest.TestCase):
    setUp=fixtures.LocalFIMTests.setUp
    replace_source=fixtures.LocalFIMTests.replace_source

    def test_only_comment_copy_removed_and_gates_still_run(self):
        cases=[]
        for include in (True,False):
            backend,generator=fixtures.LocalFIMTests.generator(self,['cleaned.split(",")'],local_fim_expressions=True)
            generator.include_original_body=include
            result=generator.generate(self.ticket,self.loc,self.repo)
            self.assertEqual(result['candidates'][0]['reason'],'no_op')
            self.assertEqual(result['static_candidate_count'],0)
            cases.append((backend,result))
        old=cases[0][0].prompts[0][0];new=cases[1][0].prompts[0][0]
        start=old.index('# ===== CURRENT IMPLEMENTATION')
        end=old.index('# Source context follows; fill the missing code.')
        self.assertEqual(old[:start]+old[end:],new)
        self.assertEqual(cases[0][0].prompts[0][1],cases[1][0].prompts[0][1])
        self.assertEqual(cases[0][0].budgets,cases[1][0].budgets)
        self.assertEqual(cases[0][1]['expression_fim'],cases[1][1]['expression_fim'])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)


if __name__=='__main__':unittest.main()
