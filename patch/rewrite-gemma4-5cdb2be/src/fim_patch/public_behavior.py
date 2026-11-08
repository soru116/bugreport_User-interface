"""Conservative public expectation evidence for the single-hole FIM path."""
import ast
import io
import re
import tokenize

VERSION = 'public-behavior-v1'
FEEDBACK_VERSION = 'compact-public-feedback-v1'
FIELDS = ('expected_behavior', 'description', 'problem_statement', 'body',
          'bug_report', 'steps_to_reproduce', 'summary')
REPL = re.compile(r'^\s*(?:>>>\s?|In\s*\[\d+\]:\s*)')
EXPECTATION = re.compile(r'^(?:should\b|expected\s*(?:result|output|behavior|behaviour)?\s*[:=])', re.I)


def expectation_evidence(ticket, *, max_chars=400, max_items=3):
    """Quote whole explicit units, never infer a result or consult test fields.

    Support expected fields/headings and expectation comments in fenced Python
    or REPL examples. Incomplete/ambiguous snippets remain unknown.
    """
    from .generator import _headings, EXPECTED_TITLE
    found, seen = [], set()

    def add(field, line, kind, value):
        value = value.strip()
        if (not value or value in seen or len(found) >= max_items or
                sum(len(row['text']) for row in found) + len(value) > max_chars):
            return
        seen.add(value)
        found.append(dict(field=field, line=line, kind=kind, text=value))

    def comments(field, rows):
        code = '\n'.join(REPL.sub('', line) for _, line in rows) + '\n'
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return
        for token in tokens:
            if token.type != tokenize.COMMENT or not EXPECTATION.match(token.string[1:].strip()):
                continue
            source = code.splitlines()[token.start[0]-1]
            expression = source[:token.start[1]].strip()
            try:
                tree = ast.parse(expression)
            except SyntaxError:
                continue
            if len(tree.body) != 1 or not isinstance(tree.body[0], (ast.Expr, ast.Assign, ast.AnnAssign)):
                continue
            # A quoted sample or docstring is not an executable reproducer.
            if isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
                continue
            add(field, rows[token.start[0]-1][0], 'inline_expected_comment', source.strip())

    for field in FIELDS:
        value = ticket.get(field)
        if not isinstance(value, str):
            continue
        # Preserve line numbering while excluding hidden issue-template comments.
        value = re.sub(r'<!--.*?-->', lambda m: '\n' * m.group().count('\n'), value, flags=re.S)
        lines = value.splitlines()
        if field == 'expected_behavior':
            for number, line in enumerate(lines, 1):
                add(field, number, 'explicit_expected_field', line)
            continue
        in_expected, fence, code_rows = False, None, []
        for number, (line, heading) in enumerate(_headings(lines), 1):
            marker = re.match(r'^\s*(`{3,}|~{3,})(.*)$', line)
            if marker:
                if fence is None:
                    fence = (marker.group(1), marker.group(2).strip().lower())
                    code_rows = []
                elif marker.group(1)[0] == fence[0][0] and len(marker.group(1)) >= len(fence[0]):
                    if fence[1] in ('', 'python', 'py', 'pycon'):
                        comments(field, code_rows)
                    fence = None
                continue
            if fence is not None:
                code_rows.append((number, line))
                continue
            if heading is not None:
                in_expected = bool(EXPECTED_TITLE.match(heading))
                continue
            if in_expected:
                add(field, number, 'explicit_expected_heading', line)
            if REPL.match(line):
                comments(field, [(number, line)])
    return found


def evidence_text(evidence):
    return ['Public evidence ('+row['field']+':'+str(row['line'])+'): '+row['text'] for row in evidence]


def diagnose_expression(candidate, original, ticket):
    from .behavior_feedback import expression_preview
    parsed = expression_preview(candidate.get('middle') or '')
    unchanged = None
    if parsed:
        try:
            unchanged = ast.dump(parsed[1]) == ast.dump(ast.parse(original, mode='eval'))
        except SyntaxError:
            pass
    return dict(version=VERSION, evidence=expectation_evidence(ticket),
                first_expression_ast_unchanged=unchanged, diagnostic_only=True,
                candidate_accepted=False, semantic_repair_verified=False)


def feedback_text(diagnostic, max_chars=600):
    messages = []
    if diagnostic.get('first_expression_ast_unchanged') is True:
        messages.append('no_op: the first complete expression matches the original AST; no repair logic was produced in it.')
    tail = 'Change the reported behavior within the selected hole; do not repeat the original expression. Output one complete expression only.'
    for message in evidence_text(diagnostic.get('evidence', [])):
        if len('\n'.join(messages + [message, tail])) <= max_chars:
            messages.append(message)
    return '\n'.join(messages + [tail])[:max_chars]
