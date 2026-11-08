# Read-only diagnosis: never accepts or rewrites a patch.
import ast
import io
import re
import textwrap
import tokenize
import hashlib
from pathlib import Path
from .generator import clean_issue_text, issue_sections


def diagnose_candidate(ticket, result, candidate, repo):
    """Record known facts, never infer the desired fix or change localization."""
    reason = candidate.get('reason', '')
    if not (reason.startswith(('no_op', 'no_executable_change', 'incomplete_function'))
            or 'outside the target function' in reason):
        return None
    sections = issue_sections(clean_issue_text('\n\n'.join(
        str(ticket.get(key) or '') for key in ('description', 'problem_statement'))))
    fields = {'expected':ticket.get('expected_behavior') or sections['expected'],
              'actual':ticket.get('actual_behavior') or ticket.get('error_message') or sections['actual'],
              'reproduce':ticket.get('steps_to_reproduce') or sections['reproduce']}
    if result.get('public_behavior') and not fields['expected']:
        from .public_behavior import evidence_text
        fields['expected'] = '\n'.join(evidence_text(result['public_behavior']['evidence']))
    diagnostic = {'original_gate_reason':reason,
        'requirement_audit':{'title':str(ticket.get('title') or '')[:300],
            'public_excerpts':{key:str(value)[:400] for key,value in fields.items() if value},
            'not_explicitly_stated':[key for key,value in fields.items() if not value]},
        'target_audit':{'file':(result.get('bug_location') or {}).get('file'),
                        'symbol':(result.get('span') or {}).get('symbol'),
                        'target_rank':result.get('target_rank'),
                        'relevance':'not_verified; check public behaviour against target code'},
        'instruction':'Check the stated behaviour against the selected function. Do not invent requirements or change unrelated code.'}
    if reason.startswith('no_op'):
        diagnostic.update(target_unchanged=True,finding='The target implementation is unchanged; no repair logic was produced.')
        return diagnostic
    if reason.startswith('no_executable_change'):
        diagnostic.update(executable_logic_unchanged=True,finding='Only comments/docstrings differ; no executable repair logic was produced.')
        return diagnostic
    try:
        span = result['span']
        if not span.get('include_signature'):
            raise ValueError('boundary diagnosis requires a whole-function output')
        middle, evidence = extract_function(candidate.get('middle') or candidate.get('raw_response',''),span['symbol'].split('.')[-1])
        root = Path(repo).resolve()
        source = (root/result['bug_location']['file']).resolve()
        if not source.is_relative_to(root):
            raise ValueError('target file outside repository')
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=result['source_file_sha256'][result['bug_location']['file']]:
            raise ValueError('source fingerprint mismatch')
        text = raw.decode('utf-8-sig')
        if span['newline']=='\r\n':
            middle = middle.replace('\n','\r\n')
        rebuilt = (text[:span['start']]+middle+text[span['end']:]).encode('utf-8-sig' if span['bom'] else 'utf-8')
        diagnostic.update(boundary_evidence=evidence,target_unchanged=rebuilt==raw)
        diagnostic['finding'] = ('The complete target function was found, but reconstructs a byte-identical original file. '
                                 'No repair logic was produced; extra output hid the unchanged target.'
                                 if rebuilt==raw else 'A complete target was found with changes; the original gate rejection still applies.')
    except (ValueError,SyntaxError,OSError,KeyError) as exc:
        diagnostic.update(finding='Cannot verify a complete unchanged target; original rejection remains.',diagnosis_unavailable=str(exc))
    return diagnostic

def extract_function(text, name):
    """Require a matching top-level function and a witnessed output boundary."""
    lines = text.splitlines(keepends=True)
    definitions, markers = [], []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.NAME and token.string == 'def':
                definitions.append(token.start)
            elif token.type == tokenize.COMMENT and re.search(r'=+\s*END\b',token.string):
                markers.append(token.start)
    except (tokenize.TokenError, IndentationError):
        # A later sibling can be truncated; earlier complete token boundaries
        # remain usable. Never manufacture closing quotes/brackets/statements.
        pass
    target = next(((line,col) for line,col in definitions
                   if re.match(r'\s*def\s+'+re.escape(name)+r'\s*\(',lines[line-1])),None)
    if target is None:
        raise ValueError('matching_target_definition_not_found')
    start, indent = target
    boundaries = [(line,'next_sibling_definition') for line,col in definitions if line>start and col==indent]
    boundaries += [(line,'explicit_end_comment') for line,col in markers if line>start and col==indent]
    # If a sibling has decorators, omit them with the sibling instead of
    # leaving an orphan decorator after the target.
    for end, evidence in sorted(boundaries):
        while end>start and (not lines[end-2].strip() or lines[end-2].lstrip().startswith('@')):
            end -= 1
        prefix = ''.join(lines[:end-1])
        try:
            tree = ast.parse(textwrap.dedent(prefix))
        except SyntaxError:
            continue
        if len(tree.body)!=1 or not isinstance(tree.body[0],(ast.FunctionDef,ast.AsyncFunctionDef)):
            continue
        node = tree.body[0]
        if node.name != name:
            continue
        first = min([node.lineno]+[d.lineno for d in node.decorator_list])
        middle = ''.join(lines[first-1:node.end_lineno]).rstrip('\r\n')+'\n'
        ast.parse(textwrap.dedent(middle))
        return middle, {'boundary_evidence':evidence,'kept_start_line':first,
                        'kept_end_line':node.end_lineno,'removed_lines':len(lines)-(node.end_lineno-first+1)}
    raise ValueError('no_safe_complete_target_boundary')
