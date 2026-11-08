"""Bounded diagnostic evidence for FIM retries; never a patch acceptance gate."""
import ast
import io
import re
import textwrap
import tokenize

from .scope_planning import public_text

VERSION = 'behavior-feedback-v1'
FEEDBACK_LIMIT = 600


def assess_option_usage(text, symbol, parameters, expectations):
    """A soft retry hint, not proof of repair or a new static acceptance gate."""
    from .generator import _target_function_node
    node = _target_function_node(text, symbol)
    nodes = [child for statement in node.body for child in ast.walk(statement)]
    reads = {child.id for child in nodes if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)}
    missing = [name for name in parameters if name not in reads]
    indirect = sorted({child.func.id for child in nodes if isinstance(child, ast.Call)
                       and isinstance(child.func, ast.Name)
                       and child.func.id in {'locals','globals','vars','eval','exec'}})
    status = ('no_direct_reference_gap' if missing and expectations and not indirect else
              'unknown_indirect_usage' if missing and indirect else
              'unknown_expected_behavior' if missing else 'direct_parameter_reference_present')
    return {'version':'public-option-usage-v1','status':status,
            'parameters_not_referenced_in_target_body':missing,
            'indirect_usage_calls':indirect,'public_expectations':expectations,
            'retry_recommended':status=='no_direct_reference_gap',
            'semantic_repair_verified':False,'static_candidate_rejected':False}


def option_retry_feedback(assessment):
    messages = ['public_option_behavior_gap: target body has no direct reference to '+
                ', '.join(assessment['parameters_not_referenced_in_target_body'])+
                '. Static candidate retained; verify and implement the requested option behavior.']
    for unit in assessment['public_expectations']:
        message = 'Public expectation: "'+unit+'".'
        if len(' '.join(messages+[message]))<=FEEDBACK_LIMIT-65:
            messages.append(message)
    messages.append('Output only the selected hole; no unchanged argument rewrite.')
    return ' '.join(messages)[:FEEDBACK_LIMIT]


def expression_preview(value):
    """Read one complete expression before a witnessed logical line boundary.

    A partial expression is unknown, never completed or guessed. A complete
    first expression followed by invalid/unrelated code is diagnostic only.
    """
    value = value.replace('\r\n', '\n').lstrip()
    lines = value.splitlines(keepends=True)
    try:
        for token in tokenize.generate_tokens(io.StringIO(value).readline):
            if token.type in {tokenize.NEWLINE, tokenize.ENDMARKER}:
                end = sum(len(line) for line in lines[:token.start[0]-1]) + token.start[1]
                fragment = value[:end].strip()
                if not fragment:
                    return None
                return fragment, ast.parse(fragment, mode='eval')
    except (SyntaxError, tokenize.TokenError, IndentationError):
        pass
    return None


def public_expectations(ticket, plan, max_chars=240):
    """Whole public units, prioritizing explicit behavior and the chosen call.

    Only public_text's allowlisted fields are consulted. In particular, inline
    comments on the public call are preserved; no reference/test patch is read.
    """
    units = []
    expected = ticket.get('expected_behavior')
    if isinstance(expected, str):
        units.extend(re.split(r'(?<=[.!?。！？])\s+|\n+', expected.strip()))
    public = public_text(ticket)
    for evidence in plan.get('evidence', []):
        call = evidence.get('call', '')
        if not call:
            continue
        try:
            expected_call = ast.parse(call, mode='eval').body
        except SyntaxError:
            continue
        if not isinstance(expected_call, ast.Call):
            continue
        for line in public.splitlines():
            source = line.strip()
            if source.startswith('>>> '):
                source = source[4:]
            try:
                tree = ast.parse(source)
                if len(tree.body)!=1 or not isinstance(tree.body[0], (ast.Assign, ast.AnnAssign, ast.Expr)):
                    continue
                if not any(isinstance(node, ast.Call) and ast.dump(node)==ast.dump(expected_call)
                           for node in ast.walk(tree)):
                    continue
                comments = [t.string.lstrip('#').strip() for t in
                            tokenize.generate_tokens(io.StringIO(source).readline)
                            if t.type == tokenize.COMMENT]
            except (SyntaxError, tokenize.TokenError, IndentationError):
                continue
            units.extend(comments)
    # A title or parameter proposal is context, not a proven expected result.
    result = []
    for unit in units:
        unit = ' '.join(unit.split())
        if unit and unit not in result and len('; '.join(result + [unit])) <= max_chars:
            result.append(unit)
    return result


def diagnose_behavior(middle, original, scope, parameters, ticket, plan):
    """Separate AST no-op, parameter reference evidence, and public expectation.

    Parameter absence is a hint: locals(), forwarding, and other indirect uses
    may be legitimate. AST change alone never proves behavioral improvement.
    """
    result = {'version': VERSION, 'candidate_accepted': False,
              'diagnostic_only': True, 'scope': scope,
              'semantic_change_verified': False,
              'public_expectations': public_expectations(ticket, plan)}
    if scope == 'python_expression':
        parsed = expression_preview(middle)
        try:
            old = ast.parse(original.strip(), mode='eval')
        except SyntaxError:
            old = None
        result['preview_kind'] = 'first_complete_expression'
    else:
        try:
            value = textwrap.dedent(middle)
            parsed = value, ast.parse(value)
            old = ast.parse(textwrap.dedent(original))
        except SyntaxError:
            parsed, old = None, None
        result['preview_kind'] = 'complete_statement_sequence'
    if parsed is None:
        result.update(status='unknown_incomplete_or_invalid', no_op=None,
                      parameters_referenced=None, parameters_not_referenced=None)
        return result
    value, tree = parsed
    referenced = {node.id for node in ast.walk(tree)
                  if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    no_op = ast.dump(tree) == ast.dump(old) if old is not None else None
    def callees(node):
        return sorted({ast.unparse(call.func) for call in ast.walk(node)
                       if isinstance(call, ast.Call) and isinstance(call.func, (ast.Name, ast.Attribute))})
    old_calls = callees(old) if old is not None else []
    new_calls = callees(tree)
    result.update(status='diagnosed', preview=value, no_op=no_op,
                  parameters_referenced=[name for name in parameters if name in referenced],
                  parameters_not_referenced=[name for name in parameters if name not in referenced],
                  call_targets_original=old_calls, call_targets_preview=new_calls,
                  call_targets_unchanged=bool(old_calls) and old_calls==new_calls)
    return result


def retry_feedback(reason, diagnostic, max_chars=FEEDBACK_LIMIT):
    """Prioritize concrete diagnostics while keeping the existing feedback bound."""
    messages = []
    if diagnostic and diagnostic.get('status') == 'diagnosed':
        if diagnostic['no_op'] is True:
            messages.append('no_op: preview matches original AST; no executable change in that preview.')
        elif diagnostic['no_op'] is False:
            messages.append('AST changed; behavioral repair is not verified.')
        missing = diagnostic['parameters_not_referenced']
        if missing:
            messages.append('parameter_not_referenced: '+', '.join(missing)+
                            ' is not read in the preview; check the requested option behavior.')
        for expectation in diagnostic['public_expectations']:
            messages.append('Public expectation: "'+expectation+'".')
        if diagnostic['no_op'] is False and diagnostic.get('call_targets_unchanged') and missing:
            messages.append('Same named call targets: '+', '.join(diagnostic['call_targets_preview'])+
                            '; check against the public expectation.')
    # Reserve a compact rejection category even when a verbose gate error exceeds
    # the bound. Add only whole messages; never cut public evidence mid-sentence.
    category = reason.split(':', 1)[0].replace('\n', ' ')[:80]
    suffix = ' Output only the selected hole; end the output.'
    compact_tail = 'Rejection: '+category+'.'+suffix
    detailed_tail = 'Rejection: '+reason.replace('\n', ' ')+'.'+suffix
    selected = []
    for message in messages:
        if len(' '.join(selected+[message, compact_tail])) <= max_chars:
            selected.append(message)
    tail = detailed_tail if len(' '.join(selected+[detailed_tail])) <= max_chars else compact_tail
    return ' '.join(selected+[tail])[:max_chars]
