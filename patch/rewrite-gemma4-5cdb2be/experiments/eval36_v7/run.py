"""Portable entry point for the frozen 36-case generation evaluation."""
import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def resolve_experiment_model(models, manifest, override=None):
    name = override or manifest['model']
    if name not in (manifest['model'], 'codellama:7b-code', 'codellama:13b-code'):
        raise ValueError('Choose the frozen Qwen model or codellama:7b-code / codellama:13b-code')
    matching = [model for model in models if model.get('name') == name]
    if not matching:
        raise ValueError('Required model is not installed: '+name)
    if name == manifest['model'] and matching[0]['digest'] != manifest['model_digest']:
        raise ValueError('Frozen Qwen digest changed; comparison model must remain fixed')
    return {'model': name, 'digest': matching[0]['digest'],
            'experiment': 'frozen-qwen' if name == manifest['model'] else 'codellama-model-comparison'}

def scoped_git_environment(args):
    """Trust only this cohort's cache/snapshot paths in the child process."""
    env = os.environ.copy()
    count = int(env.get('GIT_CONFIG_COUNT', '0'))
    cache = args.repo_cache_dir.resolve()
    snapshots = (args.snapshot_cache_dir or cache/'_stage4_snapshots').resolve()
    paths = {ROOT.as_posix()}
    tickets = [json.loads(line) for line in (HERE/'tickets.jsonl').read_text(encoding='utf-8').splitlines()]
    for ticket in tickets:
        safe_repo = ticket['repo'].replace('/', '__').replace('\\', '__').replace(':', '_')
        paths.add((cache/safe_repo).as_posix())
        paths.add((snapshots/(safe_repo+'__'+ticket['base_commit'][:16])).as_posix())
    for path in sorted(paths):
        env[f'GIT_CONFIG_KEY_{count}'] = 'safe.directory'
        env[f'GIT_CONFIG_VALUE_{count}'] = path
        count += 1
    env['GIT_CONFIG_COUNT'] = str(count)
    return env

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-cache-dir', type=Path, default=ROOT / 'data/repositories')
    parser.add_argument('--snapshot-cache-dir', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'runs/eval36-v7-issue-compact-v1')
    parser.add_argument('--url', default='http://localhost:11434')
    parser.add_argument('--model', choices=('qwen2.5-coder:14b-base', 'codellama:7b-code', 'codellama:13b-code'))
    parser.add_argument('--temperature', type=float, default=0.2)
    parser.add_argument('--timeout', type=int, default=300,
                        help='Seconds allowed per model call. Raise it on a shared Ollama server so a queued request is not recorded as a timeout')
    parser.add_argument('--expected-model-digest', help='Stop before generation if the installed tag differs from the paired control')
    parser.add_argument('--ticket-ids-from', type=Path,
                        help='Optional unique subset of the fixed 36 IDs for a targeted retry')
    parser.add_argument('--tokenizer-json', type=Path)
    parser.add_argument('--repair-rounds', type=int, choices=(1, 2, 3), default=1)
    parser.add_argument('--repair-official', action='store_true')
    sys.path.insert(0, str(ROOT/'src'))
    from fim_patch.cloud_official import add_cloud_arguments
    add_cloud_arguments(parser)
    parser.add_argument('--safe-function-extraction', action='store_true')
    parser.add_argument('--local-fim', action='store_true')
    parser.add_argument('--local-fim-expressions', action='store_true')
    parser.add_argument('--fim-scope', choices=('expression','statement','body'))
    parser.add_argument('--fim-output-ceiling', type=int)
    parser.add_argument('--safe-body-feedback', action='store_true')
    parser.add_argument('--operation-contract-preview', action='store_true')
    parser.add_argument('--contract-feedback', action='store_true')
    parser.add_argument('--domain-adapter', action='append', choices=('tolerance','conversion'), default=[])
    parser.add_argument('--omit-original-body', action='store_true',
                        help='Single-hole local expression ablation: omit the commented original Middle copy')
    parser.add_argument('--scope-adjustment', action='store_true', help='Opt-in bounded additive-signature and behavior FIM')
    parser.add_argument('--fixed-signature-body', action='store_true')
    parser.add_argument('--fixed-body-extraction', action='store_true')
    parser.add_argument('--helper-contracts', action='store_true')
    parser.add_argument('--adaptive-repair', action='store_true')
    parser.add_argument('--multi-function-repair', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--clone-missing', action='store_true')
    parser.add_argument('--fetch-missing-commits', action='store_true')
    parser.add_argument('--prompt-layout', choices=['task_last', 'legacy', 'compact_fim'], default='task_last')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    if not math.isfinite(args.temperature) or args.temperature < 0:
        parser.error('--temperature must be finite and nonnegative')
    if args.timeout < 1:
        parser.error('--timeout must be at least 1 second')
    if args.contract_feedback and (not args.operation_contract_preview or args.fim_scope!='body' or args.repair_rounds!=2
                                   or args.adaptive_repair or args.multi_function_repair):
        parser.error('--contract-feedback requires operation contract preview, body scope and two rounds')
    if args.safe_body_feedback and (args.fim_scope!='body' or args.repair_rounds>2):
        parser.error('--safe-body-feedback requires --fim-scope body and at most two rounds')
    if args.fim_scope:
        if (args.repair_rounds != 1 and not (args.safe_body_feedback or args.contract_feedback)) or args.scope_adjustment or (args.local_fim_expressions and args.fim_scope != 'expression'):
            parser.error('--fim-scope requires one round without scope adjustment or conflicting expression flag')
        args.local_fim=True
        args.local_fim_expressions=args.fim_scope=='expression'
    if args.fim_output_ceiling is not None and (not args.fim_scope or not 128 <= args.fim_output_ceiling <= 4096):
        parser.error('--fim-output-ceiling requires explicit scope and 128..4096 tokens')
    if args.scope_adjustment and (not args.local_fim_expressions or args.repair_rounds != 1):
        parser.error('--scope-adjustment requires local FIM expressions and --repair-rounds 1')
    if args.local_fim_expressions and not args.local_fim:
        parser.error('--local-fim-expressions requires --local-fim')
    if args.omit_original_body and (not (args.local_fim_expressions or args.fim_scope) or args.scope_adjustment):
        parser.error('--omit-original-body requires local expressions or explicit --fim-scope without --scope-adjustment')
    if args.local_fim:
        if args.prompt_layout != 'compact_fim' or args.multi_function_repair or args.adaptive_repair or args.fixed_body_extraction or args.safe_function_extraction:
            parser.error('--local-fim requires compact_fim without multi-function/adaptive/body extraction')
        args.fixed_signature_body=True
        args.helper_contracts=True
    if args.repair_cloud_repo and args.repair_official:
        parser.error('Choose cloud OR local official tests')
    if args.fixed_body_extraction and not args.fixed_signature_body:
        parser.error('--fixed-body-extraction requires --fixed-signature-body')
    if args.adaptive_repair and args.multi_function_repair:
        parser.error('Choose either --adaptive-repair or --multi-function-repair')
    if (args.adaptive_repair or args.multi_function_repair) and not args.fixed_signature_body:
        parser.error('Automatic repair requires --fixed-signature-body')
    if args.fixed_signature_body and (args.prompt_layout != 'compact_fim' or args.safe_function_extraction):
        parser.error('--fixed-signature-body requires --prompt-layout compact_fim without --safe-function-extraction')
    manifest = json.loads((HERE/'manifest.json').read_text(encoding='utf-8'))
    for name, metadata in manifest['files'].items():
        if hashlib.sha256((HERE/name).read_bytes()).hexdigest() != metadata['sha256']:
            raise SystemExit(f'Input hash mismatch: {name}')
    ids = (HERE/'ticket_ids.txt').read_text(encoding='utf-8').splitlines()
    if args.ticket_ids_from:
        subset = args.ticket_ids_from.read_text(encoding='utf-8-sig').splitlines()
        if not subset or len(set(subset)) != len(subset) or not set(subset).issubset(ids):
            raise SystemExit('Retry IDs must be a nonempty unique subset of the frozen cohort.')
    def check_fields(value):
        if isinstance(value, dict):
            leaked = set(value) & set(manifest['excluded_input_fields'])
            if leaked:
                raise SystemExit(f'Forbidden generation input fields: {sorted(leaked)}')
            for item in value.values():
                check_fields(item)
        elif isinstance(value, list):
            for item in value:
                check_fields(item)
    for name in ['tickets.jsonl', 'localizations.jsonl']:
        rows = [json.loads(line) for line in (HERE/name).read_text(encoding='utf-8').splitlines()]
        check_fields(rows)
        if [r['ticket_id'] for r in rows] != ids or len(ids) != 36:
            raise SystemExit(f'Expected exactly the frozen ordered 36 IDs: {name}')
    with urllib.request.urlopen(args.url.rstrip('/')+'/api/tags', timeout=10) as response:
        models = json.load(response)['models']
    try:
        identity = resolve_experiment_model(models, manifest, args.model)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if args.expected_model_digest and identity['digest'] != args.expected_model_digest:
        raise SystemExit('Installed model digest differs from the expected paired control; no generation started.')
    if identity['experiment'] != 'frozen-qwen' and args.tokenizer_json:
        if hashlib.sha256(args.tokenizer_json.read_bytes()).hexdigest() != 'dcb4337ba92f948b322e080f8b59a2a21cfe65dc2aa7ecf7df080ae8aa63c157':
            raise SystemExit('Code Llama requires its own verified tokenizer, not the Qwen tokenizer.')
    print('Verified: 36 paired cases, input hashes, model '+identity['model']+' digest '+identity['digest'], flush=True)
    if args.check_only:
        return
    if identity['experiment'] != 'frozen-qwen':
        provenance = args.output.resolve()/'experiment_model.json'
        if args.output.exists() and not args.resume:
            raise SystemExit('Use a new output folder for the Code Llama comparison, or --resume for the same run.')
        if args.resume:
            if not provenance.is_file() or json.loads(provenance.read_text(encoding='utf-8')) != identity:
                raise SystemExit('Code Llama model/digest does not match the existing run; cannot resume.')
        else:
            args.output.mkdir(parents=True, exist_ok=False)
            provenance.write_text(json.dumps(identity,indent=2),encoding='utf-8')
    command = [sys.executable, '-u', str(ROOT/'scripts/run_stage4_batch.py'),
        '--tickets', str(HERE/'tickets.jsonl'), '--localizations', str(HERE/'localizations.jsonl'),
        '--ticket-ids-from', str(args.ticket_ids_from.resolve() if args.ticket_ids_from else HERE/'ticket_ids.txt'), '--output', str(args.output.resolve()),
        '--repo-cache-dir', str(args.repo_cache_dir.resolve()), '--model', identity['model'],
        '--url', args.url, '--generation-mode', 'fim', '--fim-transport', 'ollama_suffix',
        '--prompt-layout', args.prompt_layout, '--target-policy', 'first_supported_ast_top5' if args.local_fim else 'related_targets_top5',
        '--max-body-lines', '120', '--candidates', '1', '--seed', '42',
        '--num-ctx', '16384', '--temperature', str(args.temperature), '--max-new-tokens', '4096',
        '--timeout', str(args.timeout), '--explicit-argument-edit',
        '--min-body-retention', '0.5', '--max-related-targets', '3', '--local-context', '--research-mode']
    if not args.omit_original_body:
        command += ['--include-original-body']
    if args.fim_scope:
        command += ['--fim-scope',args.fim_scope]
    if args.safe_body_feedback:
        command += ['--safe-body-feedback']
    if args.operation_contract_preview:
        command += ['--operation-contract-preview']
    if args.contract_feedback:
        command += ['--contract-feedback']
    for name in args.domain_adapter:
        command += ['--domain-adapter',name]
    if args.fim_output_ceiling is not None:
        command += ['--fim-output-ceiling',str(args.fim_output_ceiling)]
    if args.snapshot_cache_dir:
        command += ['--snapshot-cache-dir', str(args.snapshot_cache_dir.resolve())]
    if args.tokenizer_json:
        command += ['--tokenizer-json', str(args.tokenizer_json.resolve())]
    if args.repair_rounds > 1:
        command += ['--repair-rounds', str(args.repair_rounds)]
    if args.local_fim:
        command += ['--local-fim']
    if args.local_fim_expressions:
        command += ['--local-fim-expressions']
    if args.scope_adjustment:
        command += ['--scope-adjustment']
    if args.multi_function_repair:
        command += ['--multi-function-repair']
    if args.adaptive_repair:
        command += ['--adaptive-repair']
    if args.helper_contracts:
        command += ['--helper-contracts']
    if args.fixed_body_extraction:
        command += ['--fixed-body-extraction']
    if args.fixed_signature_body:
        command += ['--fixed-signature-body']
    if args.safe_function_extraction:
        command += ['--safe-function-extraction']
    if args.repair_cloud_repo:
        command += ['--repair-cloud-repo', args.repair_cloud_repo, '--repair-cloud-ref', args.repair_cloud_ref]
    if args.repair_official:
        command += ['--repair-official']
    for flag in ['resume', 'clone_missing', 'fetch_missing_commits']:
        if getattr(args, flag):
            command.append('--'+flag.replace('_', '-'))
    raise SystemExit(subprocess.call(command, cwd=ROOT, env=scoped_git_environment(args)))

if __name__ == '__main__':
    main()
