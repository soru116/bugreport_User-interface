"""Offline AST scope and public-example string provenance; advisory, never edits code."""
import ast
import json
import re
import textwrap
from .public_behavior import expectation_evidence
from .source_api_contract import safe_line

VERSION='output-scope-example-literals-v1'
PUBLIC_FIELDS=('description','bug_report','problem_statement','body','steps_to_reproduce')


def string_facts(tree):
    """Ignore comments and docstrings; keep lexical usage without type inference."""
    result=[]
    class Visit(ast.NodeVisitor):
        def visit_Expr(self,n):
            if isinstance(n.value,ast.Constant) and isinstance(n.value.value,str):return
            self.generic_visit(n)
        def visit_Subscript(self,n):
            if isinstance(n.slice,ast.Constant) and isinstance(n.slice.value,str):
                result.append(dict(value=n.slice.value,line=n.slice.lineno,usage='subscript_key'))
            self.visit(n.value)
            if not isinstance(n.slice,ast.Constant):self.visit(n.slice)
        def visit_Constant(self,n):
            if isinstance(n.value,str):result.append(dict(value=n.value,line=n.lineno,usage='string_literal'))
    Visit().visit(tree)
    return result


def examples(ticket):
    facts=[];unknown=[];seen=set()
    for field in PUBLIC_FIELDS:
        value=ticket.get(field)
        if not isinstance(value,str) or value in seen:continue
        if len(value)>200000:unknown.append('oversized_public_field');continue
        seen.add(value)
        for match in re.finditer(r'```(?:python|py)?[ \t]*\r?\n(.*?)```',value,re.S|re.I):
            code=match.group(1);line=value[:match.start(1)].count('\n')+1
            if len(code)>20000:unknown.append('oversized_public_example');continue
            try:tree=ast.parse(code)
            except SyntaxError:
                inputs=[re.sub(r'^\s*(?:>>>|\.\.\.) ?', '',s) for s in code.splitlines() if re.match(r'^\s*(?:>>>|\.\.\.) ',s)]
                try:
                    if not inputs:raise SyntaxError()
                    tree=ast.parse('\n'.join(inputs))
                except SyntaxError:unknown.append('unparseable_public_example');continue
                # REPL line maps are approximate; do not invent original code line numbers.
                line=None
            for item in string_facts(tree):facts.append(dict(item,field=field,snippet_start_line=line,example_line=item['line']))
    return facts,sorted(set(unknown))


def diagnose(middle,span,original_source,ticket):
    if not isinstance(middle,str) or len(middle)>200000:raise ValueError('unsupported output size')
    if not isinstance(original_source,str) or len(original_source)>200000:raise ValueError('unsupported base source size')
    generation_scope=span.get('generation_scope');scope=dict(generation_scope=generation_scope,
        original_statement_kind=span.get('statement_kind'),runtime_behavior_verified=False)
    tree=None
    if generation_scope not in ('issue_statement','function_body','python_expression','python_expression_inner'):
        scope.update(status='unknown',reason='unsupported_scope_parser')
    else:
        try:
            tree=ast.parse(textwrap.dedent(middle),mode='eval' if generation_scope.startswith('python_expression') else 'exec')
            if generation_scope=='issue_statement':
                kinds=[type(n).__name__ for n in tree.body]
                scope.update(status='scope_count_compatible' if len(kinds)==1 else 'scope_count_mismatch',expected_statements=1,
                             observed_statements=len(kinds),statement_kinds=kinds,statement_kind_change_verified_safe=False)
            else:scope.update(status='parsed_only',reason='syntax observation does not prove scope or semantic correctness')
        except SyntaxError as exc:scope.update(status='syntax_error',line=exc.lineno,reason=exc.msg[:160])
    base_values={f['value'] for f in string_facts(ast.parse(original_source))}
    example_facts,unknown=examples(ticket);example_values={f['value'] for f in example_facts}
    expectations=expectation_evidence(ticket,max_chars=2000,max_items=8)
    literals=[]
    if tree is not None:
        for fact in string_facts(tree):
            value=fact['value']
            if value in base_values:continue
            mentioned=any(re.search(r'(?<!\w)'+re.escape(value)+r'(?!\w)',e['text']) for e in expectations) if value else False
            overlap=value in example_values
            status=('public_expectation_mention_unknown' if mentioned else
                    'example_derived_literal_risk' if overlap and fact['usage']=='subscript_key' else
                    'example_overlap_unknown' if overlap else 'new_literal_support_unknown')
            literals.append(dict(fact,status=status,seen_in_public_example=overlap,mentioned_in_public_expectation=mentioned,
                base_source_contains_literal=False,api_requirement_verified=False,
                public_example_locations=[{k:f[k] for k in ('field','snippet_start_line','example_line','usage')} for f in example_facts if f['value']==value][:3]))
    lines=[]
    if scope['status']=='scope_count_mismatch':
        lines.append('# Output scope evidence: expected one complete replacement statement; observed '+str(scope['observed_statements'])+' top-level statements ('+', '.join(scope['statement_kinds'])+'). Siblings exceed the current hole; no automatic scope expansion.\n')
    elif scope['status']=='syntax_error':lines.append('# Output syntax evidence: '+safe_line(scope['reason'])+'; complete parse unavailable.\n')
    for item in literals:
        if item['status']!='example_derived_literal_risk':continue
        quoted=json.dumps(item['value'],ensure_ascii=True)
        for marker in ('<PRE>','<SUF>','<MID>','<EOT>','<FILL_ME>'):quoted=quoted.replace(marker,'[marker removed]')
        if len(quoted)>160:quoted='[long literal omitted]'
        lines.append('# Literal provenance evidence: new subscript key '+quoted+' occurs in public example code, absent from inspected base source and extracted expectation text. Example-specific hardcoding risk; API necessity remains unknown. Do not treat the example as a repair specification.\n')
    return dict(version=VERSION,scope=scope,literals=literals[:50],omitted_literal_records=max(0,len(literals)-50),public_example_unknowns=unknown,
        feedback_preview=''.join(lines[:4]),automatic_rejection=False,patch_modified=False,
        prompt_integration_enabled=False,model_calls=0,official_tests=0,source_executed=False,
        scope_limit='lexical provenance in inspected source and public examples, not semantic necessity; no API inference, reference patch or test fields')
