"""Source-preserving singleton-container FIM holes, without repair answers."""
import ast
import copy

VERSION = 'singleton-element-fim-v1'


def narrow_container_span(raw, span):
    """Select the sole existing AST element; ambiguous/unsupported roots retain scope."""
    audit = {'version': VERSION, 'action': 'retain_existing_scope'}
    if span.get('generation_scope') != 'python_expression':
        audit['reason'] = 'not_expression_scope'
        return span, audit
    text = raw.decode('utf-8-sig')
    original = text[span['start']:span['end']]
    if original != span['original_middle']:
        raise ValueError('inner_expression_source_mismatch')
    try:
        node = ast.parse(original, mode='eval').body
    except SyntaxError:
        audit['reason'] = 'expression_not_standalone'
        return span, audit
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)) or len(node.elts) != 1 or isinstance(node.elts[0], ast.Starred):
        audit['reason'] = 'requires_single_nonstarred_container_element'
        return span, audit
    element = node.elts[0]
    lines = original.splitlines(keepends=True)
    def offset(line, column):
        return sum(len(value) for value in lines[:line-1]) + len(lines[line-1].encode('utf-8')[:column].decode('utf-8'))
    relative_start = offset(element.lineno, element.col_offset)
    relative_end = offset(element.end_lineno, element.end_col_offset)
    start, end = span['start']+relative_start, span['start']+relative_end
    leading, trailing = original[:relative_start], original[relative_end:]
    # Bare tuples have no host-owned outer boundary. Do not claim a narrower
    # delimiter contract for them or an element that covers the complete RHS.
    if not leading.strip() or not trailing.strip():
        audit['reason'] = 'no_host_owned_outer_delimiters'
        return span, audit
    value = text[start:end]
    try:
        if ast.dump(ast.parse(value, mode='eval').body) != ast.dump(element):
            raise SyntaxError('element is not a complete standalone expression')
    except SyntaxError:
        audit['reason'] = 'element_not_standalone'
        return span, audit
    updated = dict(span)
    updated.update(start=start, end=end, prefix=text[:start], suffix=text[end:], original_middle=value,
                   line_start=text[:start].count('\n')+1, line_end=text[:end].count('\n')+1,
                   body_lines=text[start:end].count('\n')+1,
                   expression_prefix_fragment=span.get('expression_prefix_fragment','')+leading,
                   expression_suffix_fragment=trailing+span.get('expression_suffix_fragment',''),
                   inner_expression_contract={'version': VERSION, 'container_kind': type(node).__name__,
                                              'prefix': leading, 'suffix': trailing,
                                              'original_container': original})
    audit.update(action='single_container_element', container_kind=type(node).__name__,
                 original_expression=original, original_element=value,
                 host_prefix=leading, host_suffix=trailing,
                 original_start=span['start'], original_end=span['end'],
                 element_start=start, element_end=end)
    return updated, audit


def check_inner_reconstruction(span, middle):
    """A valid expression must remain exactly one element in the same container."""
    from .local_fim import normalize_expression
    contract = span.get('inner_expression_contract')
    if not contract:
        return middle
    value = normalize_expression(middle)
    try:
        expression = ast.parse(value, mode='eval').body
        reconstructed = ast.parse(contract['prefix']+value+contract['suffix'], mode='eval').body
        original = ast.parse(contract['original_container'], mode='eval').body
    except SyntaxError as exc:
        raise ValueError('inner_expression_contract_violation: invalid container reconstruction') from exc
    if type(reconstructed) is not type(original) or len(reconstructed.elts) != 1:
        raise ValueError('inner_expression_contract_violation: outer container or element count changed')
    expected = copy.deepcopy(original)
    expected.elts[0] = expression
    if ast.dump(expected) != ast.dump(reconstructed):
        raise ValueError('inner_expression_contract_violation: replacement escaped element boundary')
    return value
