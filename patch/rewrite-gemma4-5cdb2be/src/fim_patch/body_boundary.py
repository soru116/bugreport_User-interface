"""Witnessed fixed-signature body extraction; never synthesize closing code."""
import ast
import io
import tokenize

ECHO_HEADER = '# ===== ISSUE REPORT (public text only) ====='


def extract_body_with_echo(text, prefix, header_indent, prompt):
    """Require a lexical boundary and complete source; never complete syntax.

    Prompt echo is admitted only as an outer comment-only tail with known
    labels. A comment/string inside the generated body is not a witness.
    """
    try:
        kept, proof = extract_fixed_body(text, prefix, header_indent)
    except (ValueError, SyntaxError):
        pass
    else:
        if any(marker in kept for marker in ('<PRE>','<SUF>','<MID>','```')):
            raise ValueError('body_boundary_non_code_inside_target')
        return kept, proof
    if ECHO_HEADER not in prompt or '# Title:' not in prompt or not prefix.endswith('\n'):
        raise ValueError('no_witnessed_prompt_echo_boundary')
    normalized = text.replace('\r\n', '\n')
    prefix = prefix.replace('\r\n', '\n')
    lines = normalized.splitlines(keepends=True)
    offset = prefix.count('\n')
    tokens = []
    try:
        tokens.extend(tokenize.generate_tokens(io.StringIO(prefix+normalized).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    ignored = {tokenize.INDENT,tokenize.DEDENT,tokenize.NEWLINE,tokenize.NL,tokenize.ENDMARKER,tokenize.ENCODING}
    for token in tokens:
        if (token.type != tokenize.COMMENT or token.string != ECHO_HEADER
                or token.start[0] <= offset or token.start[1] > header_indent):
            continue
        line = token.start[0]-offset
        tail = lines[line-1:]
        nonblank = [value.strip() for value in tail if value.strip()]
        if (len(nonblank)<3 or not nonblank[1].startswith('# Title:')
                or not all(value.lstrip().startswith('#') and len(value)-len(value.lstrip()) <= header_indent for value in tail if value.strip())
                or not any(value.startswith(('# --- Reproduce ---','# --- Expected behaviour ---','# --- Actual behaviour / error ---')) for value in nonblank[2:])):
            continue
        prior = [value for value in tokens if offset < value.start[0] < token.start[0]]
        if any(value.type not in ignored|{tokenize.COMMENT} and value.start[1] <= header_indent for value in prior):
            raise ValueError('body_boundary_outer_code_before_echo')
        middle = ''.join(lines[:line-1])
        if not middle.strip():raise ValueError('body_boundary_empty_target')
        ast.parse(prefix+middle)
        if any(marker in middle for marker in ('<PRE>','<SUF>','<MID>','```')):
            raise ValueError('body_boundary_non_code_inside_target')
        return middle, {'version':'prompt-echo-body-boundary-v1','boundary_evidence':'outer_comment_only_prompt_echo',
                        'boundary_line':line,'kept_lines':line-1,'removed_lines':len(tail)}
    raise ValueError('no_witnessed_prompt_echo_boundary')


def extract_fixed_body(text, prefix, header_indent):
    """Cut only at a lexical sibling definition/class, excluding its decorators.

    Tokenize the actual source Prefix plus output so strings, continuation lines,
    nested definitions and multiline headers retain their real lexical meaning.
    A truncated later sibling is allowed; a truncated target is never completed.
    """
    text = text.replace('\r\n', '\n')
    prefix = prefix.replace('\r\n', '\n')
    if not prefix.endswith('\n'):
        raise ValueError('body_boundary_prefix_must_end_at_header_line')
    lines = text.splitlines(keepends=True)
    offset = prefix.count('\n')
    tokens = []
    try:
        tokens.extend(tokenize.generate_tokens(io.StringIO(prefix + text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # extend retains tokens already yielded before a truncated later sibling.
        pass
    ignored = {tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL,
               tokenize.COMMENT, tokenize.ENDMARKER, tokenize.ENCODING}
    outside, async_line = None, None
    for token in tokens:
        line, column = token.start
        if line <= offset or token.type in ignored:
            continue
        if token.type == tokenize.NAME and token.string == 'async' and column == header_indent:
            async_line = line
        if column > header_indent and not (token.type == tokenize.NAME and token.string == 'def'
                                           and line == async_line and column == header_indent + 6):
            continue
        if outside is None:
            outside = token
        if outside.string not in ('def', 'async', 'class', '@'):
            raise ValueError('body_boundary_unsupported_outer_statement')
        if token.type != tokenize.NAME or token.string not in ('def', 'class'):
            continue
        # The first outer code must belong to this sibling, not earlier prose/code.
        start = outside.start[0] - offset
        middle = ''.join(lines[:start - 1])
        if not middle.strip():
            raise ValueError('body_boundary_empty_target')
        ast.parse(prefix + middle)
        return middle, {'version': 'fixed-body-boundary-v1',
            'boundary_evidence': 'sibling_class' if token.string == 'class' else 'sibling_definition',
            'boundary_line': start, 'witness_line': line - offset,
            'kept_lines': start - 1, 'removed_lines': len(lines) - start + 1}
    raise ValueError('no_witnessed_fixed_body_boundary')
