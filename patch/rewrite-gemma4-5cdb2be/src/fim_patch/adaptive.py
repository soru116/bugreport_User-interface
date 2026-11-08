"""Public-issue/source-based context and target routing, without model calls."""
import ast
import copy
import re

from .helper_contracts import eligible_calls


def call_chain(text, symbol, max_depth=3, max_helpers=8):
    queue, seen, found = [(symbol, 0)], {symbol}, []
    while queue and len(found) < max_helpers:
        current, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        for call, helper in eligible_calls(text, current, include_comprehensions=True):
            if helper.name in seen:
                continue
            seen.add(helper.name)
            found.append((helper, depth + 1))
            queue.append((helper.name, depth + 1))
            if len(found) >= max_helpers:
                break
    return found


def implementation_blocks(text, symbol, max_chars=5000, public_text=""):
    blocks, audit, used = [], [], 0
    chain = call_chain(text, symbol)
    if public_text:
        words = set(re.findall(r'[A-Za-z_]\w{2,}', public_text.lower())) - STOP_WORDS
        chain.sort(key=lambda item: -(len(words & set(re.findall(r'[A-Za-z_]\w{2,}', ast.unparse(item[0]).lower())))
                     + (20 if item[0].name.lower() in words else 0)))
    for node, depth in chain:
        rendered = copy.deepcopy(node)
        if (rendered.body and isinstance(rendered.body[0], ast.Expr)
                and isinstance(rendered.body[0].value, ast.Constant)
                and isinstance(rendered.body[0].value.value, str)):
            rendered.body = rendered.body[1:] or [ast.Pass()]
        code = ast.unparse(rendered)
        block = '# Same-file helper implementation (source evidence):\n' + ''.join(
            '# ' + line + '\n' for line in code.splitlines())
        included = used + len(block) <= max_chars
        audit.append({'symbol': node.name, 'depth': depth, 'included': included,
                      'reason': 'within_char_cap' if included else 'char_cap'})
        if included:
            blocks.append(block)
            used += len(block)
    return blocks, audit


STOP_WORDS = set('the and for from with this that are was not can should into when then only have has return none true false table file read'.split())


def next_target(ticket, location, result, repo, excluded, generator):
    from .generator import (PUBLIC_REQUIREMENT_KEYS, _target_function_node,
                            source_for, function_span)
    public = '\n'.join(str(ticket.get(k) or '') for k in PUBLIC_REQUIREMENT_KEYS)
    words = set(re.findall(r'[A-Za-z_]\w{2,}', public.lower())) - STOP_WORDS
    candidates = []
    for rank, row in enumerate((location.get('stage3_ranked_symbols') or [])[:5], 1):
        if isinstance(row, dict):
            candidates.append((dict(row), {'origin': 'predicted_fl', 'original_rank': rank}))
    selected = (result.get('bug_location') or {})
    symbol = (result.get('span') or {}).get('symbol')
    if selected.get('file') and symbol:
        row = {'file_path': selected['file'], 'symbol_qualified_name': symbol,
               'start_line': result['span']['line_start'], 'end_line': result['span']['line_end']}
        # Read only the already verified file; source_for validates bytes/HEAD.
        raw, _, path = source_for(ticket, location, repo, row)
        for node, depth in call_chain(raw.decode('utf-8-sig'), symbol):
            candidates.append(({'file_path': path, 'symbol_qualified_name': node.name,
                                'start_line': node.lineno, 'end_line': node.end_lineno},
                               {'origin': 'same_file_call_chain', 'root': symbol, 'depth': depth}))
    choices, seen, rejected = [], set(excluded), []
    for row, provenance in candidates:
        key = (row.get('file_path'), row.get('symbol_qualified_name'))
        if key in seen:
            continue
        seen.add(key)
        try:
            raw, _, _ = source_for(ticket, location, repo, row)
            text = raw.decode('utf-8-sig')
            function_span(raw, row, expand_truncated_end=generator.expand_truncated_end,
                          max_body_lines=generator.max_body_lines)
            node = _target_function_node(text, key[1])
            tokens = set(re.findall(r'[A-Za-z_]\w{2,}', ast.unparse(node).lower()))
            overlap = sorted(words & tokens)
            explicit = key[1].split('.')[-1].lower() in words
            score = len(overlap) + (20 if explicit else 0)
            choices.append((score, row, {**provenance, 'score': score, 'overlap': overlap,
                                        'explicit_public_symbol': explicit}))
        except (ValueError, OSError, SyntaxError) as exc:
            rejected.append({'file': key[0], 'symbol': key[1], 'reason': str(exc)})
    choices.sort(key=lambda x: -x[0])  # Stable tie: predicted FL order, then BFS.
    if not choices:
        return None, {'action': 'stop', 'reason': 'no_supported_untried_target', 'rejected': rejected}
    _, row, provenance = choices[0]
    updated = copy.deepcopy(location)
    updated['stage3_ranked_symbols'] = [row]
    decision = {'action': 'reselect', 'file': row['file_path'], 'symbol': row['symbol_qualified_name'],
                **provenance, 'candidates_considered': len(choices), 'rejected': rejected}
    return updated, decision
