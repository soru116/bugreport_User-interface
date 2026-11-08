import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fim_patch.backend import OllamaFIM, OllamaRewrite
from fim_patch.cli import (DEFAULT_FIM_MODEL, build_parser, make_backend,
                           result_exit_code, validate_generation_args)
from fim_patch.evaluation import (
    SWEbenchEvaluationConfig,
    compare_ground_truth,
    load_ground_truth,
    run_swebench_evaluation,
)


class CliContractTests(unittest.TestCase):
    def parse_generation(self, *extra):
        parser = build_parser()
        args = parser.parse_args([
            '--ticket', 'ticket.json', '--localization', 'location.json',
            '--repo', 'repo', '--output', 'output', *extra
        ])
        return validate_generation_args(args, parser)

    def test_cli_requires_external_localization_handoff(self):
        parser = build_parser()
        help_text = parser.format_help()
        self.assertIn('--ticket', help_text)
        self.assertIn('--localization', help_text)
        self.assertIn('--repo', help_text)
        self.assertIn('--output', help_text)
        self.assertNotIn('--system-src', help_text)

    def test_default_dataset_uses_v5_task_schema(self):
        args = build_parser().parse_args([
            '--ticket', 'ticket.json', '--localization', 'location.json',
            '--repo', 'repo', '--output', 'output'
        ])
        self.assertEqual(args.dataset_name, 'SWE-bench/SWE-bench_Lite')
        self.assertEqual(args.large_function_strategy, 'full_body')
        self.assertEqual(args.generation_mode, 'fim')

    def test_default_mode_builds_the_legacy_fim_backend(self):
        args = self.parse_generation()
        self.assertEqual(args.model, DEFAULT_FIM_MODEL)
        self.assertIsInstance(make_backend(args), OllamaFIM)

    def test_rewrite_mode_builds_rewrite_backend_without_fim_transport(self):
        args = self.parse_generation('--generation-mode', 'rewrite',
                                     '--model', 'codellama:7b-instruct')
        backend = make_backend(args)
        self.assertIsInstance(backend, OllamaRewrite)
        self.assertFalse(hasattr(backend, 'transport'))

    def test_rewrite_mode_requires_explicit_model_and_compatible_options(self):
        for extra in (
            ('--generation-mode', 'rewrite'),
            ('--generation-mode', 'rewrite', '--model', 'instruct-model',
             '--fim-transport', 'raw_psm'),
            ('--generation-mode', 'rewrite', '--model', 'instruct-model',
             '--large-function-strategy', 'issue_statement'),
        ):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.parse_generation(*extra)

    def test_large_function_statement_strategy_is_explicit(self):
        args = build_parser().parse_args([
            '--ticket', 'ticket.json', '--localization', 'location.json',
            '--repo', 'repo', '--output', 'output',
            '--large-function-strategy', 'issue_statement',
        ])
        self.assertEqual(args.large_function_strategy, 'issue_statement')

    def test_exit_code_requires_validation(self):
        self.assertEqual(result_exit_code({'patch': 'diff', 'validation_status': 'plausible'}), 0)
        self.assertEqual(result_exit_code({'patch': 'diff', 'validation_status': 'not_run'}), 2)
        self.assertEqual(result_exit_code({'patch': '', 'validation_status': 'not_run'}), 1)


class OfficialEvaluationTests(unittest.TestCase):
    def test_official_harness_command_and_resolved_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            predictions = root / 'predictions.jsonl'
            predictions.write_text(json.dumps({
                'instance_id': 'owner__repo-1',
                'model_name_or_path': 'codellama',
                'model_patch': 'diff --git a/a.py b/a.py\n',
            }) + '\n', encoding='utf-8')
            work = root / 'official'

            def runner(command, **kwargs):
                self.assertIn('swebench.harness.run_evaluation', command)
                self.assertEqual(kwargs['cwd'], work)
                self.assertEqual(kwargs['env']['PYTHONUTF8'], '1')
                self.assertEqual(kwargs['env']['PYTHONIOENCODING'], 'utf-8')
                self.assertEqual(kwargs['env']['NO_COLOR'], '1')
                report = work / 'logs' / 'run_evaluation' / 'run-1' / 'results.json'
                report.parent.mkdir(parents=True)
                report.write_text(json.dumps({
                    'resolved_ids': ['owner__repo-1'],
                    'unresolved_ids': [], 'error_ids': [],
                }), encoding='utf-8')
                return SimpleNamespace(returncode=0, stdout='ok', stderr='')

            result = run_swebench_evaluation(
                predictions,
                'owner__repo-1',
                work,
                SWEbenchEvaluationConfig(
                    dataset_name='princeton-nlp/SWE-bench_Lite',
                    split='test', run_id='run-1', max_workers=1, timeout=60,
                ),
                runner=runner,
            )
            self.assertEqual(result['status'], 'resolved')
            self.assertTrue(result['official_resolved'])
            self.assertEqual(result['dataset_name'], 'princeton-nlp/SWE-bench_Lite')
            self.assertRegex(result['predictions_sha256'], r'^[0-9a-f]{64}$')

    def test_existing_run_directory_is_rejected_to_avoid_cached_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            predictions = root / 'predictions.jsonl'
            predictions.write_text(json.dumps({
                'instance_id': 'owner__repo-1', 'model_name_or_path': 'm', 'model_patch': 'x'
            }) + '\n', encoding='utf-8')
            cached = root / 'official' / 'logs' / 'run_evaluation' / 'same-run'
            cached.mkdir(parents=True)
            with self.assertRaisesRegex(FileExistsError, 'run_id'):
                run_swebench_evaluation(
                    predictions, 'owner__repo-1', root / 'official',
                    SWEbenchEvaluationConfig('d', 'test', 'same-run'),
                    runner=lambda *a, **k: self.fail('runner must not execute'),
                )

    def test_harness_failure_is_not_reported_as_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            predictions = root / 'predictions.jsonl'
            predictions.write_text(json.dumps({
                'instance_id': 'owner__repo-1', 'model_name_or_path': 'm', 'model_patch': 'x'
            }) + '\n', encoding='utf-8')

            def failed(*args, **kwargs):
                return subprocess.CompletedProcess(args[0], 1, stdout='', stderr='docker failed')

            with self.assertRaisesRegex(RuntimeError, 'docker failed'):
                run_swebench_evaluation(
                    predictions, 'owner__repo-1', root / 'official',
                    SWEbenchEvaluationConfig('d', 'test', 'new-run'), runner=failed,
                )


class GroundTruthTests(unittest.TestCase):
    def test_selects_ticket_id_from_dataset_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'gold.jsonl'
            path.write_text(
                json.dumps({'ticket_id': 'other', 'patch': 'other patch'}) + '\n' +
                json.dumps({'ticket_id': 'owner__repo-1', 'base_commit': 'a' * 40,
                            'patch': 'wanted patch'}) + '\n', encoding='utf-8')
            self.assertEqual(load_ground_truth(path, 'owner__repo-1')['patch'], 'wanted patch')

    def test_ground_truth_is_provenance_not_a_success_substitute(self):
        predicted = '--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n'
        reference = {
            'instance_id': 'owner__repo-1', 'base_commit': 'a' * 40,
            'patch': '--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+z\n',
            'merged_commit': 'b' * 40, 'verified': True,
        }
        result = compare_ground_truth(predicted, reference, instance_id='owner__repo-1',
                                      base_commit='a' * 40)
        self.assertFalse(result['exact_patch_match'])
        self.assertEqual(result['common_changed_files'], ['a.py'])
        self.assertTrue(result['reference_verified'])
        self.assertNotIn('official_resolved', result)

    def test_ground_truth_identity_must_match_generated_case(self):
        with self.assertRaisesRegex(ValueError, 'instance_id'):
            compare_ground_truth('x', {'instance_id': 'wrong', 'patch': 'y'},
                                 instance_id='right', base_commit=None)


class CloudEvaluationWorkflowTests(unittest.TestCase):
    def test_github_runner_keeps_linux_and_docker_off_the_local_machine(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github' / 'workflows' /
                    'swebench-official-evaluation.yml')
        text = workflow.read_text(encoding='utf-8')
        self.assertIn('workflow_dispatch:', text)
        self.assertIn('runs-on: ubuntu-24.04', text)
        self.assertIn('swebench==5.0.2', text)
        self.assertIn('swebench.harness.run_evaluation', text)
        self.assertIn('actions/upload-artifact@', text)
        self.assertIn('if: always()', text)


if __name__ == '__main__':
    unittest.main()
