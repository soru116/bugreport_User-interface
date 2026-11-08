"""Opt-in, source-related FIM bodies composed into one bounded repair.

Every body is generated against the same immutable base. Signatures/imports stay
locked. This is a repair plan, never permission to splice arbitrary model siblings.
"""
import ast
import copy
import difflib
import re
import subprocess

from .adaptive import STOP_WORDS
from .generator import (PUBLIC_REQUIREMENT_KEYS, _function_symbol_table, _target_function_node,
                        check_locked_function_contract, function_span, git, sha, source_for)
from .helper_contracts import check_calls, direct_calls, eligible_calls, signature
from .iterative import feedback_block


def related_chain(text, root, max_depth=3, max_helpers=8):
    """Verified globals and syntactic same-class instance calls; bounded BFS.

    Instance-call evidence is lexical, not a guarantee against runtime overrides.
    Decorated/rebound receiver methods, nested/dynamic/imported calls are skipped.
    """
    queue, seen, found = [(root, 0)], {root}, []
    tree = ast.parse(text)
    while queue and len(found) < max_helpers:
        symbol, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        node = _target_function_node(text, symbol)
        if node is None:
            continue
        edges = [(helper.name, helper, 'same_module_global')
                 for _, helper in eligible_calls(text, symbol, include_comprehensions=True)]
        if '.' in symbol and not node.decorator_list:
            parent = symbol.rsplit('.', 1)[0]
            # Only top-level classes: no guessing nested class identity.
            cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == parent), None)
            positional = node.args.posonlyargs + node.args.args
            if cls and positional:
                receiver = positional[0].arg
                binding = _function_symbol_table(text, symbol).lookup(receiver)
                if not binding.is_assigned():
                    for call in direct_calls(node):
                        fn = call.func
                        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                                and fn.value.id == receiver):
                            continue
                        if any(isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del))
                               and isinstance(n.value, ast.Name) and n.value.id == receiver and n.attr == fn.attr
                               for statement in node.body for n in ast.walk(statement)):
                            continue
                        definitions = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                                       and n.name == fn.attr]
                        rebound = any(isinstance(n, ast.Name) and n.id == fn.attr and isinstance(n.ctx, (ast.Store, ast.Del))
                                      for statement in cls.body if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                                      for n in ast.walk(statement))
                        if len(definitions) == 1 and not definitions[0].decorator_list and not rebound:
                            helper = definitions[0]
                            edges.append((parent + '.' + helper.name, helper, 'same_class_receiver_syntax'))
        for name, helper, kind in edges:
            if name in seen:
                continue
            seen.add(name)
            found.append({'symbol': name, 'node': helper, 'depth': depth + 1,
                          'caller': symbol, 'evidence': kind})
            queue.append((name, depth + 1))
            if len(found) >= max_helpers:
                break
    return found


def repair_plan(ticket, location, primary, repo, generator, limit=3):
    path, symbol = primary['bug_location']['file'], primary['span']['symbol']
    row = {'file_path': path, 'symbol_qualified_name': symbol,
           'start_line': primary['span']['line_start'], 'end_line': primary['span']['line_end']}
    raw, _, _ = source_for(ticket, location, repo, row)
    text = raw.decode('utf-8-sig')
    node = _target_function_node(text, symbol)
    # Root coordinates refer to the definition, not the body anchor.
    row.update(start_line=min([node.lineno] + [d.lineno for d in node.decorator_list]), end_line=node.end_lineno)
    plan = [{'row': row, 'origin': 'predicted_fl', 'original_rank': primary['target_rank'], 'role': 'primary'}]
    public = '\n'.join(str(ticket.get(k) or '') for k in PUBLIC_REQUIREMENT_KEYS)
    words = set(re.findall(r'[A-Za-z_]\w{2,}', public.lower())) - STOP_WORDS
    options, rejected = [], []
    edges = related_chain(text, symbol)
    incoming_rejected = []
    # A predicted caller may be relevant even when the selected root is a leaf.
    for ranked in (location.get('stage3_ranked_symbols') or [])[:5]:
        candidate_symbol = ranked.get('symbol_qualified_name')
        if ranked.get('file_path') != path or candidate_symbol == symbol:
            continue
        try:
            candidate_node = _target_function_node(text, candidate_symbol)
        except ValueError as exc:
            incoming_rejected.append({'symbol': candidate_symbol, 'reason': str(exc)})
            continue
        direct = next((edge for edge in related_chain(text, candidate_symbol, max_depth=1)
                       if edge['symbol'] == symbol), None)
        if direct:
            # Preserve and validate the actual FL coordinates before deriving a span.
            try:
                function_span(raw, ranked, max_body_lines=generator.max_body_lines,
                              expand_truncated_end=generator.expand_truncated_end)
            except (ValueError, SyntaxError) as exc:
                incoming_rejected.append({'symbol': candidate_symbol, 'reason': str(exc)})
                continue
            edges.append({'symbol': candidate_symbol, 'node': candidate_node, 'depth': 1,
                          'caller': candidate_symbol, 'evidence': 'predicted_caller_' + direct['evidence']})
    seen = {symbol}
    for edge in edges:
        if edge['symbol'] in seen:
            continue
        seen.add(edge['symbol'])
        helper = edge['node']
        helper_row = {'file_path': path, 'symbol_qualified_name': edge['symbol'],
                      'start_line': helper.lineno, 'end_line': helper.end_lineno}
        try:
            function_span(raw, helper_row, max_body_lines=generator.max_body_lines,
                          expand_truncated_end=generator.expand_truncated_end)
            tokens = set(re.findall(r'[A-Za-z_]\w{2,}', ast.unparse(helper).lower()))
            score = len(words & tokens) + (20 if helper.name.lower() in words else 0)
            original_rank = next((i for i, r in enumerate(location.get('stage3_ranked_symbols', []), 1)
                if (r.get('file_path'), r.get('symbol_qualified_name')) == (path, edge['symbol'])), None)
            options.append({'row': helper_row, 'origin': 'source_call_chain', 'role': 'related',
                'original_rank': original_rank, 'caller': edge['caller'], 'depth': edge['depth'],
                'evidence': edge['evidence'], 'score': score})
        except (ValueError, SyntaxError) as exc:
            rejected.append({'symbol': edge['symbol'], 'reason': str(exc)})
    options.sort(key=lambda item: -item['score'])
    plan.extend(options[:limit - 1])
    return plan, incoming_rejected + rejected


def compose(ticket, location, repo, entries, *, with_sources=False):
    """One diff per file, with offsets from base; never concatenate overlapping hunks."""
    by_file = {}
    for item in entries.values():
        path = item['row']['file_path']
        raw, _, _ = source_for(ticket, location, repo, item['row'])
        by_file.setdefault(path, {'raw': raw, 'edits': []})['edits'].append(item)
    patch_parts, digests, sources = [], {}, []
    for path, group in by_file.items():
        raw = group['raw']
        before = raw.decode('utf-8-sig')
        edits = sorted(group['edits'], key=lambda e: e['span']['start'])
        if any(a['span']['end'] > b['span']['start'] for a, b in zip(edits, edits[1:])):
            raise ValueError('overlapping_function_bodies')
        after = before
        for item in reversed(edits):
            span = item['span']
            middle = item['middle'].replace('\r\n', '\n')
            if span['newline'] == '\r\n':
                middle = middle.replace('\n', '\r\n')
            after = after[:span['start']] + middle + after[span['end']:]
        old, new = ast.parse(before), ast.parse(after)
        for item in edits:
            symbol = item['span']['symbol']
            check_locked_function_contract(old, new, symbol)
            check_calls(before, after, symbol)
        # Changed receiver calls must bind to selected same-class methods too.
        check_receiver_calls(before, after, [e['span']['symbol'] for e in edits])
        encoding = 'utf-8-sig' if raw.startswith(b'\xef\xbb\xbf') else 'utf-8'
        digests[path] = sha(after.encode(encoding))
        sources.append((path,after))
        # Include the BOM in diff text, matching the existing build_patch contract.
        old_lines = raw.decode('utf-8').splitlines(keepends=True)
        new_lines = after.encode(encoding).decode('utf-8').splitlines(keepends=True)
        parts = []
        for line in difflib.unified_diff(old_lines, new_lines, fromfile='a/' + path, tofile='b/' + path):
            parts.append(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
        patch_parts.append(''.join(parts))
    patch = ''.join(patch_parts)
    if not patch:
        raise ValueError('combined_no_op')
    git(repo, 'apply', '--check', '--whitespace=nowarn', data=patch.encode('utf-8'))
    return (patch,digests,sources) if with_sources else (patch,digests)


def check_receiver_calls(before, after, symbols):
    for symbol in symbols:
        if '.' not in symbol:
            continue
        old = _target_function_node(before, symbol)
        new = _target_function_node(after, symbol)
        positional = new.args.posonlyargs + new.args.args
        if not positional or new.decorator_list:
            continue
        receiver = positional[0].arg
        if _function_symbol_table(after, symbol).lookup(receiver).is_assigned():
            continue
        old_calls = {ast.dump(c, include_attributes=False) for c in direct_calls(old)}
        edges = {e['symbol']: e['node'] for e in related_chain(after, symbol, max_depth=1)}
        for call in direct_calls(new):
            fn = call.func
            if ast.dump(call, include_attributes=False) in old_calls or not (
                    isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == receiver):
                continue
            helper = edges.get(symbol.rsplit('.', 1)[0] + '.' + fn.attr)
            if helper is None or any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
                continue
            try:
                signature(helper).bind(None, *([None] * len(call.args)), **{k.arg: None for k in call.keywords})
            except TypeError as exc:
                raise ValueError('helper_call_incompatible: ' + fn.attr + ': ' + str(exc)) from exc


class MultiFunctionRepairGenerator:
    def __init__(self, generator, max_calls=3, max_functions=3, validator_factory=None):
        if max_calls not in (1, 2, 3) or isinstance(max_calls, bool):
            raise ValueError('multi-function total candidate calls must be 1..3')
        if max_functions not in (2, 3) or isinstance(max_functions, bool):
            raise ValueError('multi-function plan must allow 2 or 3 functions')
        if (generator.candidates != 1 or generator.generation_mode != 'fim'
                or not generator.fixed_signature_body or not generator.helper_implementations):
            raise ValueError('multi-function repair requires single-candidate fixed-signature FIM and helper implementations')
        self.generator, self.max_calls, self.max_functions = generator, max_calls, max_functions
        self.validator_factory = validator_factory

    def generate(self, ticket, location, repo):
        inner = self.generator
        saved = inner.extra_directive, inner.seed, inner.validator, inner.target_policy, inner.target_depth
        primary = inner.generate(ticket, location, repo, plan_only=True)
        if primary['patch_status'] != 'planned':
            return primary
        plan, rejected = repair_plan(ticket, location, primary, repo, inner, self.max_functions)
        pending = list(plan)
        entries, history, trials = {}, [], []
        feedback, stop, accepted = '', 'generation_limit', None
        validator = self.validator_factory(ticket) if self.validator_factory else saved[2]
        try:
            inner.validator = None  # Tests receive the combined patch, not isolated body diffs.
            inner.target_policy, inner.target_depth = 'strict_top1', 1
            for index in range(self.max_calls):
                if not pending:
                    break
                selected = pending.pop(0)
                current = copy.deepcopy(location)
                current['stage3_ranked_symbols'] = [selected['row']]
                current['stage3_candidate_symbols'] = []
                names = ', '.join(item['row']['symbol_qualified_name'] for item in plan)
                staged = ''.join('# Proposed body for ' + key[1] + ':\n' + ''.join('# ' + line + '\n'
                    for line in entry['middle'].splitlines()) for key, entry in entries.items())
                # Whole staged blocks only; do not crop into misleading code fragments.
                if len(staged) > 2000:
                    staged = '# Previously proposed edits exist for: ' + ', '.join(k[1] for k in entries) + '\n'
                inner.extra_directive = (saved[0] + '# Host combines independent FIM bodies into one repair for: ' + names
                    + '. Keep original signatures; output only this selected body.\n' + staged + feedback)
                inner.seed = saved[1] + index
                result = inner.generate(ticket, current, repo)
                result.update(repair_round=index + 1, multi_function_selection=copy.deepcopy(selected))
                result['target_rank'] = selected['original_rank']
                result['target_fallback_used'] = selected['role'] != 'primary' or (selected['original_rank'] or 1) > 1
                candidates = result.get('candidates', [])
                candidate = candidates[-1] if candidates else {}
                reason = str(candidate.get('reason', ''))
                feedback = ''
                key = (selected['row']['file_path'], selected['row']['symbol_qualified_name'])
                if result['patch_status'] == 'generated':
                    entries[key] = {'row': selected['row'], 'span': result['span'], 'middle': candidate['middle']}
                    # New plan members still get generated before combined testing.
                elif result['patch_status'] != 'no_valid_candidate':
                    stop = 'blocked_context'
                    history.append(copy.deepcopy(result))
                    break
                elif reason.startswith(('context_budget_exceeded', 'tokenizer_verification')):
                    stop = 'budget_or_tokenizer_error'
                    history.append(copy.deepcopy(result))
                    break
                elif any(word in reason for word in ('10061', 'Connection refused', 'timed out', 'Remote end closed')):
                    stop = 'backend_environment_error'
                    history.append(copy.deepcopy(result))
                    break
                elif not candidates:
                    stop = 'no_feedback'
                    history.append(copy.deepcopy(result))
                    break
                else:
                    result['component_rejection'] = reason
                    # Try linked plan members before repeating a failing body.
                    if not pending and index + 1 < self.max_calls:
                        pending.append(selected)
                        feedback = feedback_block(candidate, compact=True)
                # A recovered body may expose a runtime failure. Test it while
                # there is still budget for feedback, before exhausting the plan.
                early_test = inner.fixed_body_extraction and validator and result['patch_status'] == 'generated'
                if entries and (not pending or early_test or index + 1 == self.max_calls):
                    try:
                        patch, digests, sources = compose(ticket, location, repo, entries,with_sources=True)
                        from .generation_records import logic_fingerprint,LOGIC_VERSION,EXECUTABLE_VERSION
                        result.update(combined_logic_sha256=logic_fingerprint(sources),logic_fingerprint_version=LOGIC_VERSION,
                            combined_executable_logic_sha256=logic_fingerprint(sources,executable=True),executable_fingerprint_version=EXECUTABLE_VERSION)
                        result['combined_patch'] = patch
                        result['combined_apply_status'] = 'passed'
                        from .source_api_integration import check_candidate
                        api_candidate={}
                        if not check_candidate(inner,api_candidate,patch):
                            result['source_api_contract']=api_candidate['source_api_contract']
                            raise ValueError('source_api_call_incompatible: '+api_candidate['failure_feedback'])
                        if api_candidate:result['source_api_contract']=api_candidate['source_api_contract']
                        try:
                            report = validator(patch, repo) if validator else {'status': 'not_run'}
                        except (OSError, subprocess.SubprocessError) as exc:
                            report = {'status': 'error', 'reason': 'validator_environment_error', 'feedback': str(exc)}
                        result['combined_validation'] = report
                        if isinstance(report.get('official_resolved'), bool):
                            trials.append(copy.deepcopy(report))
                        if validator and report.get('status') != 'plausible':
                            result['combined_rejection'] = 'validation_failed'
                            if report.get('status') == 'error':
                                stop = 'test_environment_error'
                            elif index + 1 < self.max_calls:
                                # Test traceback can steer within the existing, bounded plan.
                                detail = str(report.get('feedback') or '\n'.join(
                                    str((report.get(k) or {}).get('log') or '') for k in ('after', 'regression')))
                                matches = [(max(m.start() for m in found), p) for p in plan
                                    if (found := list(re.finditer(r'\b' + re.escape(
                                        p['row']['symbol_qualified_name'].split('.')[-1]) + r'\b', detail)))]
                                # The deepest/latest traceback frame is usually closer to
                                # the fault than the caller mentioned near its beginning.
                                repair_target = max(matches, key=lambda item: item[0])[1] if matches else (
                                    pending[0] if pending else selected)
                                if getattr(inner,'validation_retry_target_policy','trace')=='fixed_selected':repair_target=selected
                                result['validation_retry_selection'] = {
                                    'symbol': repair_target['row']['symbol_qualified_name'],
                                    'reason': 'paired_fixed_selected' if getattr(inner,'validation_retry_target_policy','trace')=='fixed_selected' else 'last_trace_name_match' if matches else 'next_plan_member'}
                                pending = [repair_target] + [p for p in pending if p['row'] != repair_target['row']]
                                feedback = feedback_block({'reason': 'combined_validation_failed', 'validation': report}, compact=True) if getattr(inner,'forward_test_feedback',True) else ''
                        else:
                            accepted = {'patch': patch, 'digests': digests, 'validation': report}
                            stop = 'tests_passed' if validator else 'static_apply_passed_without_tests'
                    except (ValueError, SyntaxError, OSError, subprocess.SubprocessError) as exc:
                        result['combined_rejection'] = str(exc)
                        if index + 1 < self.max_calls:
                            pending.append(selected)
                            feedback = feedback_block({'reason': str(exc)}, compact=True)
                history.append(copy.deepcopy(result))
                if accepted or stop == 'test_environment_error':
                    break
                if not pending:
                    stop = 'plan_exhausted_without_patch'
                    break
        finally:
            inner.extra_directive, inner.seed, inner.validator, inner.target_policy, inner.target_depth = saved
        result = copy.deepcopy(history[-1])
        result.update(multi_function_repair='multi-function-fim-v1', repair_plan=plan, plan_rejections=rejected,
            validation_schedule='after_each_changed_body' if inner.fixed_body_extraction and validator else 'after_plan',
            repair_history=history, repair_rounds_used=len(history), repair_stop_reason=stop,
            generation_call_limit=self.max_calls, changed_functions=[{'file': k[0], 'function': k[1]} for k in entries],
            target_set=[{'file': p['row']['file_path'], 'function': p['row']['symbol_qualified_name'], 'role': p['role']} for p in plan],
            official_trials=trials, official_resolved=((accepted or {}).get('validation', {}).get('official_resolved')
                if accepted else (trials[-1]['official_resolved'] if trials and stop != 'test_environment_error' else None)))
        result['prompt_version'] = result.get('prompt_version', '') + '-multi-function-fim-v1'
        result['target_policy'] = saved[3]
        result['targets_attempted'] = len({(h['multi_function_selection']['row']['file_path'],
            h['multi_function_selection']['row']['symbol_qualified_name']) for h in history})
        result['targets_generated'] = len(entries)
        combined = {'candidate_id': 1, 'status': 'generated' if accepted else 'rejected',
                    'component_rounds': len(history), 'reason': None if accepted else stop}
        tested = next((h for h in reversed(history) if 'combined_validation' in h), None)
        if tested:
            combined.update(apply_check_status='passed', validation=tested['combined_validation'])
        result.update(candidates=[combined], patch_status='generated' if accepted else 'no_valid_candidate',
                      patch=accepted['patch'] if accepted else '', modified_files=list(accepted['digests']) if accepted else [],
                      apply_status='passed' if accepted or tested else 'not_run',
                      validation_status=('plausible' if accepted else 'error' if stop == 'test_environment_error' else 'failed')
                        if any('combined_validation' in h for h in history) and validator else 'not_run',
                      requires_manual_patch=not bool(accepted))
        if accepted:
            combined.update(patch=accepted['patch'], apply_check_status='passed', validation=accepted['validation'],
                            after_sha256=accepted['digests'], modified_files=result['modified_files'])
            result['selected_candidate_id'] = 1
        else:
            result.pop('selected_candidate_id', None)
        result['explanation'] = 'Combined source-related FIM bodies; official repair is unverified unless a validator resolved it.'
        from .generation_records import annotate_generation
        return annotate_generation(result)
