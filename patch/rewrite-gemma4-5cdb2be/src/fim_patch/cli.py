"""Single supported command-line entry for FL -> patch generation -> evaluation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backend import TRANSPORTS, OllamaFIM, OllamaRewrite
from .evaluation import (SWEbenchEvaluationConfig, compare_ground_truth,
                         load_ground_truth, run_swebench_evaluation,
                         update_evaluation_artifact)
from .generator import (GENERATION_MODES, LARGE_FUNCTION_STRATEGIES, TARGET_POLICY_DEPTH,
                        FIMPatchGenerator, PROMPT_LAYOUTS,
                        RESEARCH_ONLY_TARGET_POLICIES)
from .localization_handoff import load_localization
from .pipeline import RepairPipeline, write_run
from .validation import UnittestValidator


DEFAULT_FIM_MODEL = 'codellama:7b-code'
DEFAULT_FIM_TRANSPORT = 'ollama_suffix'


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--multi-function-repair', action='store_true',
                        help='Compose source-related fixed-signature FIM bodies within one total candidate-call limit')
    parser.add_argument('--adaptive-repair', action='store_true',
                        help='Bound helper implementations and reselect public/source-derived targets within the round limit')
    parser.add_argument('--helper-contracts', action='store_true',
                        help='Provide same-file helper signatures and reject provably incompatible changed calls')
    parser.add_argument('--fixed-body-extraction', action='store_true',
                        help='Extract only witnessed complete fixed-signature bodies; preserve raw outputs and all gates')
    parser.add_argument('--local-fim', action='store_true', help='Opt-in bounded AST statement FIM with compact source context')
    parser.add_argument('--local-fim-expressions', action='store_true', help='With local FIM, retain docstrings and anchor holes inside expressions')
    parser.add_argument('--fim-scope', choices=('expression','statement','body'))
    parser.add_argument('--fim-output-ceiling', type=int)
    parser.add_argument('--safe-body-feedback', action='store_true')
    parser.add_argument('--operation-contract-preview', action='store_true',
                        help='Record source/public contract diagnostics and an unsent FIM preview; no verdict or retry changes')
    parser.add_argument('--contract-feedback', action='store_true',
                        help='Opt-in one extra body-FIM contract review, preserving the first selected patch without test evidence')
    parser.add_argument('--domain-adapter', action='append', choices=('tolerance','conversion'), default=[])
    parser.add_argument('--scope-adjustment', action='store_true', help='Opt-in bounded additive-signature and behavior FIM')
    parser.add_argument('--fixed-signature-body', action='store_true',
                        help='With compact_fim, preserve original signature in Prefix and generate body only')
    parser.add_argument('--safe-function-extraction', action='store_true',
                        help='Extract a complete whole-function FIM output only with a witnessed boundary; retain all gates')
    parser.add_argument('--ticket', type=Path, required=True)
    parser.add_argument('--localization', type=Path, required=True,
                        help='External frozen FL result as one JSON record or JSONL handoff')
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--test-spec', type=Path, help='Trusted local unittest specification')
    parser.add_argument('--ground-truth', type=Path, help='Reference patch JSON read only after generation')
    parser.add_argument('--generation-mode', choices=GENERATION_MODES, default='fim',
                        help='fim fills a function body; rewrite asks an instruction model '
                             'to return one complete function')
    parser.add_argument('--model', default=None,
                        help=f'Model tag. Defaults to {DEFAULT_FIM_MODEL!r} in FIM mode; '
                             'rewrite mode requires an explicit instruction-model tag')
    parser.add_argument('--fim-transport', choices=TRANSPORTS, default=DEFAULT_FIM_TRANSPORT,
                        help="raw_psm reproduces the pre-fix runs whose markers were "
                             "never parsed as tokens; it does not infill")
    parser.add_argument('--url', default='http://localhost:11434')
    parser.add_argument('--tokenizer-json', type=Path, default=None,
                        help='Verified local Qwen or Code Llama tokenizer for FIM token budgeting')
    parser.add_argument('--repair-rounds', type=int, choices=(1,2,3), default=1)
    parser.add_argument('--repair-official', action='store_true')
    from .cloud_official import add_cloud_arguments
    add_cloud_arguments(parser)
    parser.add_argument('--candidates', type=int, default=4)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--num-ctx', type=int, default=4096)
    parser.add_argument('--temperature', type=float, default=OllamaFIM.temperature,
                        help='Sampling temperature. Kept at the historical default so no past run changes meaning; the capability probe found 0.8 removes almost all verbatim copying (see reports/STAGE4_CAPABILITY_PROBE_ZH.md), so a new baseline passes it explicitly rather than inheriting it')
    parser.add_argument('--timeout', type=int, default=180,
                        help='Seconds allowed per model call. A 13B model regenerating a '
                             '60-line body exceeds the 180 s default and every candidate '
                             'is lost to a timeout with nothing recorded')
    parser.add_argument('--max-new-tokens', type=int, default=512)
    parser.add_argument('--research-mode', action='store_true')
    parser.add_argument('--local-context', action='store_true')
    parser.add_argument('--target-policy', choices=tuple(TARGET_POLICY_DEPTH),
                        default='first_supported_ast',
                        help='strict_top1 patches Stage-3 rank 1 or nothing; '
                             'first_supported_ast walks down to the first rank the AST '
                             'supports and reports target_rank')
    parser.add_argument('--expand-truncated-end', dest='expand_truncated_end',
                        action='store_true', default=None,
                        help='Accept a Stage-3 end_line that falls inside the definition '
                             '(the 80-line chunk boundary case). Default: on for scanning '
                             'policies, off for strict_top1')
    parser.add_argument('--no-expand-truncated-end', dest='expand_truncated_end',
                        action='store_false')
    parser.add_argument('--include-original-body', action=argparse.BooleanOptionalAction,
                        default=True,
                        help='Show the model the implementation it is replacing, as comments '
                             'in the prefix. Without it the model rewrites a function it has '
                             'never seen and invents APIs that do not exist')
    parser.add_argument('--explicit-argument-edit', action='store_true',
                        help='Experiment: restate an explicit positional-argument correction '
                             'from the public issue in the generation prompt')
    parser.add_argument('--prompt-layout', choices=PROMPT_LAYOUTS, default='task_last',
                        help='task_last puts the repair instruction immediately '
                             'above the generation point; legacy reproduces the '
                             'pre-2026-10-02 layout for an existing run.')
    parser.add_argument('--min-body-retention', type=float, default=0.5,
                        help='Reject a body keeping less than this share of the original '
                             "lines. 0 disables the check (the pre-3h behaviour)")
    parser.add_argument('--max-body-lines', type=int, default=0,
                        help='Skip a target whose body exceeds this many lines, before any '
                             'model call. 0 disables the limit')
    parser.add_argument('--large-function-strategy', choices=LARGE_FUNCTION_STRATEGIES,
                        default='full_body',
                        help='full_body preserves the existing whole-body FIM path; '
                             'issue_statement narrows functions of at least 80 lines to one '
                             'public-issue-matched top-level statement')
    parser.add_argument('--max-related-targets', type=int, default=3,
                        help='Maximum total functions in a related-target repair (1-5)')
    parser.add_argument('--official-eval', action='store_true',
                        help='Run the official Docker-based SWE-bench harness after generation')
    parser.add_argument('--dataset-name', default='SWE-bench/SWE-bench_Lite')
    parser.add_argument('--split', default='test')
    parser.add_argument('--run-id')
    parser.add_argument('--max-workers', type=int, default=1)
    parser.add_argument('--eval-timeout', type=int, default=1800)
    parser.add_argument('--swebench-python', default=sys.executable)
    parser.add_argument('--modal', action='store_true')
    return parser


def validate_generation_args(args, parser):
    from .domain_adapters import validate_adapters
    try:validate_adapters(getattr(args,'domain_adapter',()))
    except ValueError as exc:parser.error(str(exc))
    scope = getattr(args,'fim_scope',None)
    ceiling = getattr(args,'fim_output_ceiling',None)
    safe_body = getattr(args,'safe_body_feedback',False)
    contract = getattr(args,'contract_feedback',False)
    if contract and (not getattr(args,'operation_contract_preview',False) or scope!='body' or args.repair_rounds!=2
                     or getattr(args,'adaptive_repair',False) or getattr(args,'multi_function_repair',False)
                     or getattr(args,'max_targets',1)!=1
                     or args.target_policy not in ('strict_top1','first_supported_ast','first_supported_ast_top5')):
        parser.error('--contract-feedback requires --operation-contract-preview, --fim-scope body and --repair-rounds 2')
    if getattr(args,'operation_contract_preview',False) and scope!='body':
        parser.error('--operation-contract-preview requires --fim-scope body')
    if safe_body and (scope!='body' or args.repair_rounds>2):
        parser.error('--safe-body-feedback requires --fim-scope body and at most two rounds')
    if scope is not None:
        if (args.generation_mode != 'fim' or args.prompt_layout != 'compact_fim' or not args.research_mode
                or args.candidates != 1 or (args.repair_rounds != 1 and not (safe_body or contract)) or getattr(args,'scope_adjustment',False)
                or (getattr(args,'local_fim_expressions',False) and scope != 'expression')):
            parser.error('--fim-scope requires single-candidate single-round compact research FIM without scope adjustment or conflicting expression flag')
        args.local_fim=True
        args.local_fim_expressions=scope=='expression'
    if ceiling is not None and (scope is None or ceiling < 128 or ceiling > args.max_new_tokens):
        parser.error('--fim-output-ceiling requires explicit scope and 128 <= ceiling <= max-new-tokens')
    """Resolve the legacy FIM default and reject cross-mode option leakage."""

    if getattr(args,'scope_adjustment',False) and (not getattr(args,'local_fim_expressions',False)
            or not args.research_mode or args.candidates != 1 or args.repair_rounds != 1):
        parser.error('--scope-adjustment requires research mode, local FIM expressions, candidates=1 and repair-rounds=1')
    if getattr(args,'local_fim_expressions',False) and not getattr(args,'local_fim',False):
        parser.error('--local-fim-expressions requires --local-fim')
    if getattr(args, 'local_fim', False):
        if (args.generation_mode != 'fim' or args.prompt_layout != 'compact_fim'
                or args.large_function_strategy != 'full_body' or getattr(args,'max_targets',1)!=1
                or getattr(args,'multi_function_repair',False) or getattr(args,'adaptive_repair',False)
                or getattr(args,'fixed_body_extraction',False) or getattr(args,'safe_function_extraction',False)
                or args.target_policy in {'related_targets_top5','called_helper_multisymbol'}):
            parser.error('--local-fim requires compact_fim single-target FIM without adaptive/multi-function/body extraction')
        args.fixed_signature_body=True
        args.helper_contracts=True
    if getattr(args, 'repair_cloud_repo', None) and (getattr(args, 'repair_official', False) or getattr(args, 'official_eval', False) or getattr(args, 'test_spec', None)):
        parser.error('Choose cloud OR local official/tests')
    if getattr(args, 'fixed_body_extraction', False) and not getattr(args, 'fixed_signature_body', False):
        parser.error('--fixed-body-extraction requires --fixed-signature-body')
    if getattr(args, 'multi_function_repair', False):
        if getattr(args, 'adaptive_repair', False):
            parser.error('Choose either --adaptive-repair or --multi-function-repair')
        if (args.generation_mode != 'fim' or not getattr(args, 'fixed_signature_body', False)
                or args.prompt_layout != 'compact_fim' or args.candidates != 1
                or getattr(args, 'max_targets', 1) != 1 or args.large_function_strategy != 'full_body'
                or getattr(args, 'safe_function_extraction', False)):
            parser.error('--multi-function-repair requires compact_fim, fixed-signature FIM, full_body, one candidate and --max-targets 1')
        if args.max_related_targets not in (2, 3):
            parser.error('--multi-function-repair requires --max-related-targets 2 or 3')

    if args.generation_mode == 'rewrite':
        if getattr(args, 'tokenizer_json', None):
            parser.error('--tokenizer-json is only supported in FIM mode')
        if not args.model:
            parser.error('--generation-mode rewrite requires an explicit --model')
        if args.fim_transport != DEFAULT_FIM_TRANSPORT:
            parser.error('--fim-transport is only configurable in --generation-mode fim')
        if args.large_function_strategy != 'full_body':
            parser.error('--generation-mode rewrite requires --large-function-strategy full_body')
    elif not args.model:
        args.model = DEFAULT_FIM_MODEL
    return args


def make_backend(args):
    common = dict(model=args.model, url=args.url, num_ctx=args.num_ctx,
                  max_new_tokens=args.max_new_tokens, timeout=args.timeout,
                  temperature=args.temperature)
    if args.generation_mode == 'rewrite':
        return OllamaRewrite(**common)
    return OllamaFIM(**common, transport=args.fim_transport,
                     tokenizer_json=getattr(args, 'tokenizer_json', None))


def result_exit_code(result, official=None):
    if official is not None:
        return 0 if official.get('official_resolved') is True else 1
    if result.get('validation_status') == 'plausible':
        return 0
    return 2 if result.get('patch') else 1


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def execute(args, parser):
    validate_generation_args(args, parser)
    if args.target_policy in RESEARCH_ONLY_TARGET_POLICIES and not args.research_mode:
        parser.error('Alternative target selection requires --research-mode')
    if args.max_body_lines < 0:
        parser.error('--max-body-lines must be zero or positive')
    if args.output.exists() or args.output.resolve().is_relative_to(args.repo.resolve()):
        parser.error('Use a new output directory outside the input repository')
    if args.official_eval and not args.run_id:
        parser.error('--official-eval requires --run-id')
    ticket = _read(args.ticket)
    localization = load_localization(args.localization, ticket)
    validator = UnittestValidator(_read(args.test_spec)) if args.test_spec else None
    generator = FIMPatchGenerator(
        make_backend(args),
        candidates=args.candidates, seed=args.seed, validator=validator,
        generation_mode=args.generation_mode,
        research_mode=args.research_mode, local_context=args.local_context,
        target_policy=args.target_policy,
        expand_truncated_end=args.expand_truncated_end,
        max_body_lines=args.max_body_lines,
        large_function_strategy=args.large_function_strategy,
        include_original_body=args.include_original_body,
        explicit_argument_edit=args.explicit_argument_edit,
        prompt_layout=args.prompt_layout,
        safe_function_extraction=getattr(args, "safe_function_extraction", False),
        fixed_signature_body=getattr(args, "fixed_signature_body", False),
        fixed_body_extraction=getattr(args, "fixed_body_extraction", False),
        local_fim=getattr(args,"local_fim",False),
        local_fim_expressions=getattr(args,"local_fim_expressions",False),
        scope_adjustment=getattr(args,"scope_adjustment",False),
        fim_scope=getattr(args,"fim_scope",None),
        fim_output_ceiling=getattr(args,"fim_output_ceiling",None),
        safe_body_feedback=getattr(args,"safe_body_feedback",False),
        operation_contract_preview=getattr(args,"operation_contract_preview",False),
        contract_feedback=getattr(args,"contract_feedback",False),
        domain_adapters=getattr(args,'domain_adapter',()),
        helper_contracts=(getattr(args, "helper_contracts", False) or (getattr(args, "adaptive_repair", False) or getattr(args, "multi_function_repair", False))),
        helper_implementations=(getattr(args, "adaptive_repair", False) or getattr(args, "multi_function_repair", False)),
        min_body_retention=args.min_body_retention,
        max_related_targets=args.max_related_targets,
    )
    if args.repair_rounds > 1 or args.repair_official or args.repair_cloud_repo or args.adaptive_repair or args.multi_function_repair:
        from .iterative import IterativeRepairGenerator, OfficialRepairValidator, require_official_environment
        if args.repair_cloud_repo and (args.repair_official or args.test_spec):
            parser.error('Choose cloud OR local official/tests')
        if args.repair_official and args.test_spec:
            parser.error('Choose either --test-spec or --repair-official for iterative validation')
        if args.repair_official:
            require_official_environment(args.swebench_python)
        factory = (lambda item: OfficialRepairValidator(
            item.get('instance_id') or item['ticket_id'], args.output.parent/(args.output.name+'_official_repair'),
            dataset=args.dataset_name, split=args.split, timeout=args.eval_timeout, python=args.swebench_python)
        ) if args.repair_official else None
        if args.repair_cloud_repo:
            from .cloud_official import cloud_factory
            factory = cloud_factory(args,args.output.parent/(args.output.name+'_official_cloud'),
                dataset=args.dataset_name,split=args.split,timeout=args.eval_timeout)
        if args.multi_function_repair:
            if args.adaptive_repair:
                parser.error('Choose either --adaptive-repair or --multi-function-repair')
            from .multi_function import MultiFunctionRepairGenerator
            generator = MultiFunctionRepairGenerator(generator, args.repair_rounds, args.max_related_targets, factory)
        else:
            generator = IterativeRepairGenerator(generator,args.repair_rounds,factory,adaptive=args.adaptive_repair)
    location, result = RepairPipeline(generator).run(
        ticket, str(args.repo.resolve()), localization)
    write_run(args.output, ticket, location, result)

    instance_id = ticket.get('instance_id') or ticket.get('ticket_id')
    if args.ground_truth:
        ground_truth = compare_ground_truth(
            result['patch'], load_ground_truth(args.ground_truth, instance_id),
            instance_id=instance_id,
            base_commit=result.get('base_commit'),
        )
        (args.output / 'ground_truth.json').write_text(
            json.dumps(ground_truth, ensure_ascii=False, indent=2), encoding='utf-8')
        update_evaluation_artifact(args.output, ground_truth=ground_truth)

    official = None
    if args.official_eval:
        if not result.get('patch'):
            raise ValueError('Official evaluation requires a generated patch')
        if not instance_id:
            raise ValueError('Official evaluation requires instance_id')
        official = run_swebench_evaluation(
            args.output / 'predictions.jsonl', instance_id,
            args.output / 'official_harness',
            SWEbenchEvaluationConfig(
                dataset_name=args.dataset_name, split=args.split, run_id=args.run_id,
                max_workers=args.max_workers, timeout=args.eval_timeout,
                python_executable=args.swebench_python, modal=args.modal,
            ),
        )
        (args.output / 'official_evaluation.json').write_text(
            json.dumps(official, ensure_ascii=False, indent=2), encoding='utf-8')
        update_evaluation_artifact(args.output, official=official)

    summary = {'output': str(args.output.resolve()), 'patch_status': result['patch_status'],
               'validation_status': result['validation_status'],
               'official_resolved': None if official is None else official['official_resolved']}
    print(json.dumps(summary, ensure_ascii=False))
    return result_exit_code(result, official)


def main(argv=None):
    parser = build_parser()
    return execute(parser.parse_args(argv), parser)
