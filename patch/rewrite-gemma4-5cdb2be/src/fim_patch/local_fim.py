"""Bounded predicted-function local FIM selection and prompt-only AST context."""
import ast
import re
import io
import tokenize

VERSION = 'local-fim-v1'
MAX_LINES = 20
MAX_CHARS = 2000
CONTEXT_CHARS = 3200
EXPRESSION_VERSION = 'local-fim-expression-v2'
PUBLIC_FIELDS = ('title','summary','description','problem_statement','expected_behavior',
                 'actual_behavior','error_message','steps_to_reproduce','body','bug_report')
STOP_WORDS = {'that','this','with','from','have','will','into','should','would','could','there',
              'their','when','function','method','return','returns','expected','actual','error',
              'behaviour','behavior','issue','code','input','output','using','does','doesn','instead',
              'the','and','for','are','was','were','has','had','not','can','cannot','all','any',
              'only','also','same','than','then','but','self'}

def _doc(node):
    return isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str)

def _words(value):
    return {w.casefold() for w in re.findall(r'[A-Za-z_][A-Za-z0-9_]*',value)
            if len(w)>=3 and w.casefold() not in STOP_WORDS}

def _reads(node):
    return {n.id for n in ast.walk(node) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load)}

def _writes(node):
    values={n.id for n in ast.walk(node) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Store)}
    if isinstance(node,(ast.Import,ast.ImportFrom)):
        values.update(a.asname or (a.name.split('.')[0] if isinstance(node,ast.Import) else a.name) for a in node.names)
    return values

def _terms(node):
    words=set()
    for n in ast.walk(node):
        if isinstance(n,ast.Name):words.update(_words(n.id))
        elif isinstance(n,ast.Attribute):words.update(_words(n.attr))
        elif isinstance(n,ast.Constant) and isinstance(n.value,str):words.update(_words(n.value))
        elif isinstance(n,ast.Constant) and n.value is None:words.add('none')
    return words

def select_span(raw,function,ticket,path,max_lines=MAX_LINES,max_chars=MAX_CHARS):
    from .generator import _target_function_node
    text=raw.decode('utf-8-sig');node=_target_function_node(text,function['symbol'])
    lines=text.splitlines(keepends=True)
    public='\n'.join(str(ticket.get(k) or '') for k in PUBLIC_FIELDS)
    terms=_words(public)
    traces=[]
    for file,line in re.findall(r'File [\"\']([^\"\']+)[\"\'], line (\d+)',public):
        normalized=file.replace('\\','/')
        if normalized==path or normalized.endswith('/'+path):
            traces.append(int(line))
    choices=[]
    rejected=[]
    for statement in node.body:
        if _doc(statement) or isinstance(statement,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            continue
        count=statement.end_lineno-statement.lineno+1
        if count>max_lines:
            rejected.append({'line':statement.lineno,'reason':'block_exceeds_'+str(max_lines)+'_lines'});continue
        if len(''.join(lines[statement.lineno-1:statement.end_lineno]))>max_chars:
            rejected.append({'line':statement.lineno,'reason':'block_exceeds_'+str(max_chars)+'_chars'});continue
        # Physical-line splicing cannot isolate semicolon-separated siblings.
        if any(other is not statement and other.lineno<=statement.end_lineno and other.end_lineno>=statement.lineno
               for other in node.body):
            rejected.append({'line':statement.lineno,'reason':'shared_physical_line'});continue
        overlap=sorted(terms&_terms(statement))
        trace=any(statement.lineno<=line<=statement.end_lineno for line in traces)
        if not overlap and not trace:continue
        score=(int(trace),len(set(overlap)&{name.casefold() for name in _writes(statement)}),len(overlap))
        choices.append((score,statement,overlap,trace))
    audit={'version':VERSION,'origin':'predicted_fl_plus_public_evidence','max_lines':max_lines,'max_chars':max_chars,
           'rejected_blocks':rejected,'function_body_lines':function['body_lines']}
    if not choices:
        audit.update(action='fallback_full_body',reason='no_bounded_statement_evidence')
        return function,audit
    top=max(c[0] for c in choices);best=[c for c in choices if c[0]==top]
    if len(best)!=1:
        audit.update(action='fallback_full_body',reason='ambiguous_statement_evidence',
                     candidate_lines=[c[1].lineno for c in best])
        return function,audit
    _,statement,overlap,trace=best[0]
    start=sum(len(line) for line in lines[:statement.lineno-1])
    end=sum(len(line) for line in lines[:statement.end_lineno])
    narrowed=dict(function)
    narrowed.pop('fixed_output_budget',None)
    narrowed.update(start=start,end=end,prefix=text[:start],original_middle=text[start:end],suffix=text[end:],
        line_start=statement.lineno,line_end=statement.end_lineno,body_lines=statement.end_lineno-statement.lineno+1,
        function_body_lines=function['body_lines'],generation_scope='issue_statement',
        statement_kind=type(statement).__name__,statement_issue_terms=overlap,include_signature=False,lock_signature=True)
    audit.update(action='local_statement',reason='matching_public_trace' if trace else 'unique_public_term_overlap',
                 line_start=statement.lineno,line_end=statement.end_lineno,statement_kind=type(statement).__name__,matched_terms=overlap)
    return narrowed,audit


def anchor_span(raw, span):
    """Retain the statement's syntax and move the hole into one AST expression.

    Selection remains predicted/public. Unsupported statement kinds retain the
    v1 statement hole; ambiguous selection retains a body hole after its docstring.
    Python AST columns count UTF-8 bytes, whereas source splicing uses characters.
    """
    from .generator import _target_function_node
    text = raw.decode('utf-8-sig')
    lines = text.splitlines(keepends=True)
    node = _target_function_node(text, span['symbol'])
    updated = dict(span)
    audit = {'version': EXPRESSION_VERSION, 'action': 'retain_existing_scope'}
    if span.get('generation_scope') != 'issue_statement':
        doc = node.body[0] if node.body and _doc(node.body[0]) else None
        if doc and len(node.body)>1 and doc.end_lineno < node.body[1].lineno:
            start = sum(len(line) for line in lines[:doc.end_lineno])
            updated.update(start=start, prefix=text[:start], original_middle=text[start:span['end']],
                           line_start=doc.end_lineno+1, body_lines=span['line_end']-doc.end_lineno)
            audit.update(action='body_after_docstring', docstring_line_end=doc.end_lineno)
        return updated, audit
    statement = next(s for s in node.body if s.lineno == span['line_start'] and s.end_lineno == span['line_end'])
    expression = (statement.value if isinstance(statement,(ast.Assign,ast.AnnAssign,ast.Return)) else
                  statement.test if isinstance(statement,(ast.If,ast.While,ast.Assert)) else None)
    if expression is None:
        audit['reason'] = 'unsupported_expression_anchor'
        return updated, audit
    def offset(line, column):
        return sum(len(value) for value in lines[:line-1])+len(lines[line-1].encode('utf-8')[:column].decode('utf-8'))
    start = offset(expression.lineno, expression.col_offset)
    end = offset(expression.end_lineno, expression.end_col_offset)
    updated.update(start=start, end=end, prefix=text[:start], original_middle=text[start:end], suffix=text[end:],
                   line_start=expression.lineno, line_end=expression.end_lineno,
                   body_lines=expression.end_lineno-expression.lineno+1,
                   generation_scope='python_expression',
                   expression_parent_line_start=statement.lineno,
                   expression_parent_line_end=statement.end_lineno,
                   expression_prefix_fragment=text[span['start']:start],
                   expression_suffix_fragment=text[end:span['end']])
    audit.update(action='expression', statement_kind=type(statement).__name__,
                 expression_kind=type(expression).__name__, original_expression=text[start:end],
                 line_start=expression.lineno, line_end=expression.end_lineno)
    return updated, audit


def select_explicit_scope(raw, function, ticket, path, scope):
    """Select a scope after locking the predicted function; never retarget it."""
    if scope == 'body':
        from .generator import _target_function_node
        node = _target_function_node(raw.decode('utf-8-sig'), function['symbol'])
        if not any(not _doc(statement) for statement in node.body):
            raise ValueError('fim_scope_unavailable: target has no executable statements')
        span, expression_audit = anchor_span(raw, function)
        span = dict(span, generation_scope='function_body', lock_signature=True)
        audit = {'version':'explicit-fim-scope-v1','action':'fixed_function_body',
                 'origin':'locked_predicted_function','docstring_preserved':expression_audit.get('action')=='body_after_docstring'}
    else:
        span, audit = select_span(raw, function, ticket, path)
        if span.get('generation_scope') != 'issue_statement':
            raise ValueError('fim_scope_unavailable: no unique bounded statement in locked target')
        expression_audit = {}
        if scope == 'expression':
            span, expression_audit = anchor_span(raw, span)
            if span.get('generation_scope') != 'python_expression':
                raise ValueError('fim_scope_unavailable: statement has no supported expression anchor')
    if not span['original_middle'].strip():
        raise ValueError('fim_scope_unavailable: empty executable scope')
    return span, audit, expression_audit


def normalize_expression(middle):
    """Require one complete expression, preserving same-line suffix delimiters."""
    value = middle.replace('\r\n','\n').strip()
    if not value or '\r' in value:
        raise ValueError('expression_contract_violation: empty or invalid newline')
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO('('+value+'\n)').readline))
        if any(t.type == tokenize.COMMENT for t in tokens):
            raise ValueError('expression_contract_violation: comments are not expression output')
        try:
            ast.parse(value, mode='eval')
        except SyntaxError:
            value = '('+value+'\n)'
            ast.parse(value, mode='eval')
    except (SyntaxError, tokenize.TokenError, IndentationError) as exc:
        raise ValueError('expression_contract_violation: expected one complete expression') from exc
    return value

def prompt_context(raw,span,max_chars=CONTEXT_CHARS,retain_docstring=False):
    """Crop only prompt context; original span remains intact for reconstruction.

    Whole AST neighbours and prior definitions referenced by the hole are kept
    within a character budget. No slicing inside strings/multiline statements.
    Omitted dependencies are audited rather than claimed complete.
    """
    from .generator import _target_function_node,_function_suite_start_line
    text=raw.decode('utf-8-sig');lines=text.splitlines(keepends=True)
    node=_target_function_node(text,span['symbol'])
    suite=node.lineno+_function_suite_start_line(''.join(lines[node.lineno-1:]))-1
    first=min([node.lineno]+[d.lineno for d in node.decorator_list])
    header=''.join(lines[first-1:suite-1])
    parent_names=[]
    parent_reads=set()
    if retain_docstring:
        children=ast.parse(text).body
        headers=[]
        for name in span['symbol'].split('.')[:-1]:
            parent=next((n for n in children if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name),None)
            if parent is None:
                raise ValueError('local_fim_context_unavailable: parent header cannot be located')
            first_parent=min([parent.lineno]+[d.lineno for d in parent.decorator_list])
            child=parent.body[0]
            first_child=min([child.lineno]+[d.lineno for d in getattr(child,'decorator_list',[])])
            headers.append(''.join(lines[first_parent-1:first_child-1]))
            parent_names.append(name)
            for evidence in list(parent.decorator_list)+(list(parent.bases) if isinstance(parent,ast.ClassDef) else []):
                parent_reads.update(_reads(evidence))
            children=parent.body
        header=''.join(headers)+header
    doc_context='absent'
    if retain_docstring and node.body and _doc(node.body[0]):
        doc=node.body[0]
        block=''.join(lines[doc.lineno-1:doc.end_lineno])
        doc_context='original'
        if len(block)>1000:
            block=' '*doc.col_offset+repr(doc.value.value[:400]+'\n[documentation shortened in prompt]')+'\n'
            doc_context='shortened'
        header+=block
    fragment_prefix=span.get('expression_prefix_fragment','')
    fragment_suffix=span.get('expression_suffix_fragment','')
    mandatory=len(header)+len(fragment_prefix)+len(fragment_suffix)
    if mandatory>max_chars:
        raise ValueError('local_fim_context_unavailable: complete signature/docstring/skeleton exceeds context budget')
    body=[s for s in node.body if not _doc(s)]
    hole_start=span.get('expression_parent_line_start',span['line_start'])
    hole_end=span.get('expression_parent_line_end',span['line_end'])
    before=[s for s in body if s.end_lineno<hole_start]
    after=[s for s in body if s.lineno>hole_end]
    selected=[s for s in body if hole_start<=s.lineno and s.end_lineno<=hole_end]
    wanted=set().union(*(_reads(s) for s in selected)) if selected else set()
    required=[]
    # Follow prior binding dependencies conservatively in reverse source order.
    for statement in reversed(before):
        writes=_writes(statement)
        if wanted&writes:
            required.append(statement)
            wanted=(wanted-writes)|_reads(statement)
    neighbours=before[-2:]+after[:2]
    prefix_blocks=[];suffix_blocks=[];omitted=[];used=mandatory
    # Dependencies get budget before neighbours, retaining original source order later.
    kept=set()
    for statement in required+neighbours:
        if id(statement) in kept:continue
        block=''.join(lines[statement.lineno-1:statement.end_lineno])
        if used+len(block)>max_chars:
            omitted.append(statement.lineno);continue
        kept.add(id(statement));used+=len(block)
    for statement in before:
        if id(statement) in kept:prefix_blocks.append(''.join(lines[statement.lineno-1:statement.end_lineno]))
    for statement in after:
        if id(statement) in kept:suffix_blocks.append(''.join(lines[statement.lineno-1:statement.end_lineno]))
    names=parent_reads|wanted|set().union(*(_reads(s) for s in before+after if id(s) in kept))
    imports=[]
    for statement in ast.parse(text).body:
        if isinstance(statement,(ast.Import,ast.ImportFrom)) and names&_writes(statement):
            block=''.join(lines[statement.lineno-1:statement.end_lineno])
            if used+len(block)<=max_chars:imports.append(block);used+=len(block)
            else:omitted.append(statement.lineno)
    prefix=''.join(imports)+header+''.join(prefix_blocks)+fragment_prefix
    suffix=fragment_suffix+''.join(suffix_blocks)
    # A real sibling boundary helps FIM stop at the end of the selected function.
    if not suffix:
        for sibling in ast.walk(ast.parse(text)):
            if isinstance(sibling,(ast.FunctionDef,ast.AsyncFunctionDef)) and sibling.lineno>node.end_lineno:
                try:
                    count=_function_suite_start_line(''.join(lines[sibling.lineno-1:]))-1
                except ValueError:
                    continue
                block=''.join(lines[sibling.lineno-1:sibling.lineno-1+count])
                if used+len(block)+1<=max_chars:suffix='\n'+block
                break
    return prefix,suffix,{'strategy':'local_fim_ast_context','truncated':True,
        'context_char_limit':max_chars,'source_context_chars':len(prefix)+len(suffix),
        'prefix_block_lines':[s.lineno for s in before if id(s) in kept],
        'suffix_block_lines':[s.lineno for s in after if id(s) in kept],
        'omitted_block_lines':sorted(set(omitted)),'retained_complete_signature':True,
        **({'docstring_context':doc_context,'parent_headers':parent_names,
            'retained_expression_skeleton':bool(fragment_prefix or fragment_suffix)}
           if retain_docstring else {})}
