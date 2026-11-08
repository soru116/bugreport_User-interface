"""Conservative public-requirement/AST scope evidence; never executes issue code."""
import ast
import hashlib
import re
from .scope_planning import public_text

VERSION = 'requirement-hole-compatibility-v2'


def assess_scope(raw, symbol, ticket, spans):
    from .generator import _target_function_node
    text = raw.decode('utf-8-sig')
    node = _target_function_node(text, symbol)
    public = public_text(ticket)
    report = dict(version=VERSION, status='unknown', evidence=[],
                  source_sha256=hashlib.sha256(raw).hexdigest(),
                  origin='public_requirement_plus_locked_predicted_function_ast',
                  semantic_compatibility_verified=False, recommended_scope=None,
                  model_calls=0)
    # An explicit dependency-removal clause, not mere token overlap, is required.
    attributes = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
    avoided = {}
    for attribute in attributes:
        pattern = (r'\b(?:avoid(?:ing)?|without|no longer (?:use|using)|'
                   r'remov(?:e|ing) (?:the )?(?:use of|dependency on))'
                   r'[^.\n]{0,60}(?<!\w)' + re.escape(attribute) + r'(?!\w)')
        match = re.search(pattern, public, re.I)
        if match:
            avoided[attribute] = match.group(0)
    lines = text.splitlines(keepends=True)

    def bounds(n):
        def offset(line, col):
            return sum(map(len, lines[:line-1])) + len(lines[line-1].encode('utf-8')[:col].decode('utf-8'))
        return offset(n.lineno, n.col_offset), offset(n.end_lineno, n.end_col_offset)

    # Local straight-line assignment def-use only; nested functions are excluded.
    taint = set()
    dependencies = []
    for statement in node.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
            value = statement.value
            attrs = sorted({n.attr for n in ast.walk(value) if isinstance(n, ast.Attribute)} & set(avoided))
            reads = {n.id for n in ast.walk(value) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            writes = {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)}
            if attrs or reads & taint:
                taint.update(writes)
                if attrs:
                    dependencies.append(dict(line=statement.lineno, attributes=attrs,
                                             public_excerpts=[avoided[a] for a in attrs]))
            else:
                taint.difference_update(writes)
        if isinstance(statement, ast.If):
            reads = {n.id for n in ast.walk(statement.test) if isinstance(n, ast.Name)}
            direct = {n.attr for n in ast.walk(statement.test) if isinstance(n, ast.Attribute)} & set(avoided)
            if not (reads & taint or direct):
                continue
            begin, end = bounds(statement)
            outside = [scope for scope, span in spans.items()
                       if not (span['start'] <= begin and end <= span['end'])]
            if outside:
                report['evidence'].append(dict(kind='avoided_dependency_controls_outside_hole',
                    branch_lines=[statement.lineno, statement.end_lineno],
                    dependent_names=sorted(reads & taint), direct_attributes=sorted(direct),
                    dependency_assignments=list(dependencies), incompatible_scopes=outside))
    if report['evidence']:
        body = spans.get('body')
        sufficient = body and all('body' not in e['incompatible_scopes'] for e in report['evidence'])
        report.update(status='narrow_scope_conflict', recommended_scope='body' if sufficient else None,
                      reason='explicit_dependency_removal_reaches_preserved_conditional',
                      action='start_at_body' if sufficient else 'stop_no_covering_scope')
    else:
        report.update(action='retain_order_with_unknown', reason='no_proven_scope_conflict')
    from .target_context import assess_target_context
    report['target_context']=assess_target_context(text,symbol,public)
    if report['target_context']['risk']:
        report['target_review_required']=True
        if report['status']=='unknown':
            report.update(status='target_context_risk',action='retain_order_with_target_warning',recommended_scope=None,
                reason='target_owner_context_needed; body expansion alone does not resolve cross-function uncertainty')
    return report
