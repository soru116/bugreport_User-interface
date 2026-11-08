import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fim_patch import cli
from fim_patch.generator import FIMPatchGenerator, git
from fim_patch.localization_handoff import load_localization
from fim_patch.pipeline import FROZEN_RETRIEVAL_METHOD, RepairPipeline, write_run


class Backend:
    max_new_tokens = 2048

    def __init__(self):
        self.calls = []

    def describe(self):
        return {'model': 'fake-backend'}

    def infill(self, prefix, suffix, *, seed, max_new_tokens=None):
        self.calls.append((prefix, suffix))
        return {'middle': '    return "" if value is None else value.strip().lower()\n',
                'seed': seed, 'stop_reason': 'visible_eot'}


def frozen_location(ticket, base, source):
    row = {'file_path': 'username.py', 'symbol_qualified_name': 'normalize_username',
           'start_line': 1, 'end_line': 2, 'symbol_kind': 'function'}
    return {
        'ticket_id': ticket['ticket_id'],
        'repo': ticket['repo'],
        'base_commit': base,
        'confidence_level': 'high',
        'should_manual_review': False,
        'recommend_patch_generation': True,
        'stage3_ranked_symbols': [row],
        'source_file_sha256': {'username.py': hashlib.sha256(source).hexdigest()},
        'method': dict(FROZEN_RETRIEVAL_METHOD),
    }


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.source = b'def normalize_username(value):\n    return value.strip().lower()\n'
        (self.repo / 'username.py').write_bytes(self.source)
        (self.repo / 'test_username.py').write_text(
            'import unittest\nfrom username import normalize_username\n'
            'class Tests(unittest.TestCase):\n'
            ' def test_none(self): self.assertEqual(normalize_username(None), "")\n'
            ' def test_string(self): self.assertEqual(normalize_username(" A "), "a")\n',
            encoding='utf-8')
        git(self.repo, 'init')
        git(self.repo, 'config', 'core.autocrlf', 'false')
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'base')
        self.base = git(self.repo, 'rev-parse', 'HEAD').decode().strip()
        self.ticket = {'ticket_id': 'smoke-1', 'repo': 'org/proj', 'base_commit': self.base,
                       'title': 'normalize_username crashes on None',
                       'description': 'normalize_username(None) must return an empty string; strip and lowercase strings.',
                       'logs': 'File "username.py", line 2, in normalize_username: AttributeError'}
        self.location = frozen_location(self.ticket, self.base, self.source)

    def test_pipeline_consumes_external_stage3_record_without_local_retrieval(self):
        backend = Backend()
        pipeline = RepairPipeline(FIMPatchGenerator(backend, candidates=1))
        location, result = pipeline.run(self.ticket, self.repo, self.location)
        self.assertIs(location, self.location)
        self.assertEqual(result['validation_status'], 'not_run')
        self.assertEqual(result['bug_location']['function'], 'normalize_username')
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(git(self.repo, 'status', '--porcelain'), b'')

    def test_wrong_hash_is_rejected_before_backend_call(self):
        backend = Backend()
        location = dict(self.location)
        location['source_file_sha256'] = {'username.py': '0' * 64}
        with self.assertRaisesRegex(ValueError, 'source_file_sha256 mismatch'):
            RepairPipeline(FIMPatchGenerator(backend, candidates=1)).run(
                self.ticket, self.repo, location)
        self.assertEqual(backend.calls, [])

    def test_missing_source_hash_is_rejected(self):
        location = dict(self.location)
        location['source_file_sha256'] = {}
        with self.assertRaisesRegex(ValueError, 'missing a valid SHA-256'):
            RepairPipeline(FIMPatchGenerator(Backend(), candidates=1)).run(
                self.ticket, self.repo, location)

    def test_head_must_match_handoff_base_commit(self):
        git(self.repo, 'commit', '--allow-empty', '-m', 'move HEAD')
        with self.assertRaisesRegex(ValueError, 'HEAD does not match'):
            RepairPipeline(FIMPatchGenerator(Backend(), candidates=1)).run(
                self.ticket, self.repo, self.location)

    def test_repo_commit_and_frozen_method_must_match(self):
        pipeline = RepairPipeline(FIMPatchGenerator(Backend(), candidates=1))
        wrong_repo = dict(self.location, repo='other/project')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            pipeline.run(self.ticket, self.repo, wrong_repo)
        wrong_base = dict(self.location, base_commit='0' * 40)
        with self.assertRaises(ValueError):
            pipeline.run(self.ticket, self.repo, wrong_base)
        wrong_method = dict(self.location, method={**self.location['method'], 'embedding_backend': 'sbert'})
        with self.assertRaisesRegex(ValueError, 'embedding_backend'):
            pipeline.run(self.ticket, self.repo, wrong_method)

    def test_jsonl_loader_selects_by_ticket_id_and_rejects_mismatch(self):
        path = self.root / 'localizations.jsonl'
        other = dict(self.location, ticket_id='another-ticket')
        path.write_text(json.dumps(other) + '\n' + json.dumps(self.location) + '\n', encoding='utf-8')
        self.assertEqual(load_localization(path, self.ticket)['ticket_id'], 'smoke-1')
        path.write_text(json.dumps(other) + '\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            load_localization(path, self.ticket)

    def test_json_object_handoff_is_supported(self):
        path = self.root / 'localization.json'
        path.write_text(json.dumps(self.location), encoding='utf-8')
        self.assertEqual(load_localization(path, self.ticket)['base_commit'], self.base)

    def test_supervised_checkpoint_metadata_is_rejected(self):
        location = dict(self.location)
        location['method'] = {**self.location['method'], 'ml_checkpoint': 'phase19-reranker.bin'}
        with self.assertRaisesRegex(ValueError, 'supervised checkpoint'):
            RepairPipeline(FIMPatchGenerator(Backend(), candidates=1)).run(
                self.ticket, self.repo, location)

    def test_official_cli_passes_jsonl_handoff_to_fake_backend(self):
        backend = Backend()
        ticket_path = self.root / 'ticket.json'
        localization_path = self.root / 'external-localization.jsonl'
        output = self.root / 'run-output'
        ticket_path.write_text(json.dumps(self.ticket), encoding='utf-8')
        localization_path.write_text(json.dumps(self.location) + '\n', encoding='utf-8')
        parser = cli.build_parser()
        args = parser.parse_args([
            '--ticket', str(ticket_path), '--localization', str(localization_path),
            '--repo', str(self.repo), '--output', str(output), '--candidates', '1',
        ])
        with patch('fim_patch.cli.OllamaFIM', return_value=backend):
            code = cli.execute(args, parser)
        saved_location = json.loads((output / 'localization.json').read_text(encoding='utf-8'))
        saved_result = json.loads((output / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(code, 2)  # patch generated, but no test spec was supplied
        self.assertEqual(saved_location['stage3_ranked_symbols'], self.location['stage3_ranked_symbols'])
        self.assertEqual(saved_result['bug_location']['function'], 'normalize_username')
        self.assertEqual(len(backend.calls), 1)

    def test_write_run_preserves_handoff_artifact(self):
        backend = Backend()
        location, result = RepairPipeline(FIMPatchGenerator(backend, candidates=1)).run(
            self.ticket, self.repo, self.location)
        out = self.root / 'output'
        write_run(out, self.ticket, location, result)
        stored = json.loads((out / 'localization.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['source_file_sha256'], self.location['source_file_sha256'])
        self.assertEqual(json.loads((out / 'predictions.jsonl').read_text())['model_patch'], result['patch'])
        with self.assertRaises(FileExistsError):
            write_run(out, self.ticket, location, result)


if __name__ == '__main__':
    unittest.main()
