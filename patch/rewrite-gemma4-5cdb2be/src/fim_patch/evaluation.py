"""Post-generation Ground Truth evidence and isolated SWE-bench evaluation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SWEbenchEvaluationConfig:
    dataset_name: str
    split: str
    run_id: str
    max_workers: int = 1
    timeout: int = 1800
    python_executable: str = sys.executable
    modal: bool = False

    def __post_init__(self):
        if not self.dataset_name or not self.split:
            raise ValueError('dataset_name and split are required')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', self.run_id):
            raise ValueError('run_id must be 1-80 safe filename characters')
        if self.max_workers < 1 or self.timeout < 1:
            raise ValueError('max_workers and timeout must be positive')


def _prediction_record(path: Path, instance_id: str) -> dict:
    if path.suffix.lower() != '.jsonl' or not path.is_file():
        raise ValueError('predictions_path must be an existing JSONL file')
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError('Official single-case evaluation requires exactly one prediction')
    row = rows[0]
    if row.get('instance_id') != instance_id:
        raise ValueError('Prediction instance_id does not match the requested case')
    if not isinstance(row.get('model_patch'), str) or not row['model_patch']:
        raise ValueError('Official evaluation requires a nonempty model_patch')
    return row


def run_swebench_evaluation(predictions_path, instance_id, work_dir, config, *, runner=subprocess.run):
    """Run the official harness, whose local mode executes tests in Docker."""
    predictions_path = Path(predictions_path).resolve()
    _prediction_record(predictions_path, instance_id)
    work_dir = Path(work_dir).resolve()
    report = work_dir / 'logs' / 'run_evaluation' / config.run_id / 'results.json'
    if report.parent.exists():
        raise FileExistsError('Use a new run_id; an official harness run already exists')
    work_dir.mkdir(parents=True, exist_ok=True)
    command = [
        config.python_executable, '-m', 'swebench.harness.run_evaluation',
        '--dataset_name', config.dataset_name,
        '--split', config.split,
        '--predictions_path', str(predictions_path),
        '--max_workers', str(config.max_workers),
        '--run_id', config.run_id,
        '--timeout', str(config.timeout),
        '--instance_ids', instance_id,
    ]
    if config.modal:
        command.extend(['--modal', 'true'])
    evaluator_env = os.environ.copy()
    evaluator_env.update({
        'PYTHONUTF8': '1',
        'PYTHONIOENCODING': 'utf-8',
        'NO_COLOR': '1',
    })
    completed = runner(command, cwd=work_dir, capture_output=True, text=True,
                       timeout=config.timeout + 300, env=evaluator_env)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or 'official evaluator failed')[-4000:]
        raise RuntimeError(detail)
    if not report.is_file():
        raise RuntimeError('Official evaluator completed without results.json')
    harness_report = json.loads(report.read_text(encoding='utf-8'))
    resolved = instance_id in harness_report.get('resolved_ids', [])
    unresolved = instance_id in harness_report.get('unresolved_ids', [])
    if not resolved and not unresolved:
        raise RuntimeError('Official evaluator did not produce a resolved/unresolved verdict')
    return {
        'schema_version': 'SWEbenchOfficialEvaluationV1',
        'status': 'resolved' if resolved else 'unresolved',
        'official_resolved': resolved,
        'instance_id': instance_id,
        'dataset_name': config.dataset_name,
        'split': config.split,
        'run_id': config.run_id,
        'isolation': 'modal' if config.modal else 'official-docker-harness',
        'predictions_sha256': hashlib.sha256(predictions_path.read_bytes()).hexdigest(),
        'harness_report': harness_report,
        'stdout_tail': (completed.stdout or '')[-4000:],
    }


def _changed_files(patch: str) -> list[str]:
    paths = set()
    for line in patch.splitlines():
        if not line.startswith(('--- ', '+++ ')):
            continue
        path = line[4:].split('\t', 1)[0].strip()
        if path == '/dev/null':
            continue
        if path.startswith(('a/', 'b/')):
            path = path[2:]
        if path:
            paths.add(path)
    return sorted(paths)


def load_ground_truth(path, instance_id):
    """Select one post-generation reference row from JSON or JSONL."""
    path = Path(path)
    if path.suffix.lower() == '.jsonl':
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines()
                if line.strip()]
    else:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        rows = value if isinstance(value, list) else [value]
    matches = [row for row in rows if isinstance(row, dict)
               and (row.get('instance_id') or row.get('ticket_id')) == instance_id]
    if len(matches) != 1:
        raise ValueError('Ground Truth must contain exactly one matching instance_id/ticket_id')
    return matches[0]


def compare_ground_truth(predicted_patch, reference, *, instance_id, base_commit):
    """Record reference-patch similarity; never treat similarity as functional success."""
    reference_id = ((reference.get('instance_id') or reference.get('ticket_id'))
                    if isinstance(reference, dict) else None)
    if reference_id != instance_id:
        raise ValueError('Ground Truth instance_id does not match the generated case')
    if base_commit and reference.get('base_commit') and reference['base_commit'] != base_commit:
        raise ValueError('Ground Truth base_commit does not match the generated case')
    reference_patch = reference.get('patch')
    if not isinstance(reference_patch, str) or not reference_patch:
        raise ValueError('Ground Truth requires a nonempty patch')
    predicted_files = _changed_files(predicted_patch)
    reference_files = _changed_files(reference_patch)
    return {
        'schema_version': 'PatchGroundTruthComparisonV1',
        'instance_id': instance_id,
        'base_commit': base_commit,
        'reference_patch_sha256': hashlib.sha256(reference_patch.encode('utf-8')).hexdigest(),
        'predicted_patch_sha256': hashlib.sha256(predicted_patch.encode('utf-8')).hexdigest(),
        'exact_patch_match': predicted_patch == reference_patch,
        'predicted_changed_files': predicted_files,
        'reference_changed_files': reference_files,
        'common_changed_files': sorted(set(predicted_files) & set(reference_files)),
        'reference_verified': reference.get('verified') is True,
        'merged_commit': reference.get('merged_commit'),
        'interpretation': 'Reference similarity is descriptive; official tests determine repair success.',
    }


def update_evaluation_artifact(output, *, ground_truth=None, official=None):
    output = Path(output)
    path = output / 'evaluation.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    if ground_truth is not None:
        data['ground_truth'] = ground_truth
    if official is not None:
        data['official_evaluation'] = official
        data['official_resolved'] = official['official_resolved']
        data['interpretation'] = ('Resolved by official SWE-bench evaluation' if official['official_resolved']
                                  else 'Unresolved by official SWE-bench evaluation')
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
