"""Conservative producer/validation intent versus a locked property target.

Evidence is a risk signal, not proof of the correct repair location.
"""
import ast
import re

VERSION='public-api-accessor-context-v1'


def name(node):
    if isinstance(node,ast.Name):return node.id
    if isinstance(node,ast.Attribute):
        root=name(node.value)
        return root+'.'+node.attr if root else None
    return None


def assess_target_context(source,symbol,public):
    tree=ast.parse(source);parts=symbol.split('.');nodes=tree.body;owner=None;target=None
    for part in parts:
        target=next((n for n in nodes if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==part),None)
        if target is None:break
        if isinstance(target,ast.ClassDef):owner=target
        nodes=getattr(target,'body',[])
    report=dict(version=VERSION,status='unknown',risk=False,semantic_incompatibility_verified=False,
        correct_target_verified=False,source_target=symbol,public_calls=[],evidence=[],model_calls=0)
    if not isinstance(target,(ast.FunctionDef,ast.AsyncFunctionDef)) or owner is None:return report
    decorators=[name(d) for d in target.decorator_list]
    arguments=[a.arg for a in (*target.args.posonlyargs,*target.args.args,*target.args.kwonlyargs)]
    if ('property' not in decorators or len(arguments)!=1 or arguments[0] not in ('self','cls')
            or target.args.vararg or target.args.kwarg):
        report['reason']='not_a_supported_zero_argument_property';return report
    # Extract only public expected-behavior prose, not examples/tracebacks as an intent clause.
    clean=public.replace('\r\n','\n').replace('\r','\n')
    prose=re.sub(r'```.*?```','',clean,flags=re.S)
    section=re.search(r'(?im)^#{1,6}\s*Expected behavio[u]?r\s*\n',prose)
    intent_text=prose[section.end():] if section else prose
    if section:intent_text=re.split(r'(?m)^#{1,6}\s+',intent_text,1)[0]
    clauses=[]
    patterns=[('input_validation',r'\b(?:must|should)\s+(?:raise|reject|refuse|not\s+accept)\b[^.\n]{0,140}'),
              ('output_integrity',r'\b(?:must|should)\s+never\s+(?:silently\s+)?(?:create|write|produce|return|save|emit)\b[^.\n]{0,120}\b(?:corrupt|invalid|malformed)\b[^.\n]{0,40}')]
    for line in intent_text.splitlines():
        if re.search(r'\b(?:if|unless|only when|might|maybe)\b|\?',line,re.I):continue
        for kind,pattern in patterns:
            match=re.search(pattern,line,re.I)
            if match:clauses.append(dict(kind=kind,public_excerpt=match.group(0)))
    if not clauses:
        report['reason']='no_supported_explicit_requirement';return report
    # Fenced examples: parse only, never import or execute calls.
    calls=[]
    for match in re.finditer(r'```(?:python|py)?[ \t]*\n(.*?)```',clean,re.S|re.I):
        try:example=ast.parse(match.group(1))
        except SyntaxError:continue
        aliases={}
        for node in example.body:
            if isinstance(node,ast.ImportFrom):
                for a in node.names:
                    if a.name!='*':aliases[a.asname or a.name]=((node.module or '')+'.'+a.name).strip('.')
            elif isinstance(node,ast.Import):
                for a in node.names:aliases[a.asname or a.name.split('.')[0]]=a.name if a.asname else a.name.split('.')[0]
        for node in ast.walk(example):
            if not isinstance(node,ast.Call):continue
            called=name(node.func)
            if not called:continue
            root,*tail=called.split('.')
            if root not in aliases:continue
            resolved='.'.join([aliases[root],*tail]);leaf=resolved.split('.')[-1]
            if not leaf or not leaf[0].isupper():continue
            calls.append(dict(api=resolved,constructor_name=leaf,example_line=node.lineno,
                keywords=[kw.arg for kw in node.keywords if kw.arg],positional_arguments=len(node.args),runtime_type_verified=False))
    report['public_calls']=calls
    if not calls:
        report['reason']='no_supported_imported_constructor_example';return report
    classes={n.name:n for n in tree.body if isinstance(n,ast.ClassDef)}
    def inherits(child,ancestor,seen=()):
        if child==ancestor:return True
        if child in seen or child not in classes:return False
        return any(inherits((name(base) or '').split('.')[-1],ancestor,seen+(child,)) for base in classes[child].bases)
    related=[c for c in calls if inherits(c['constructor_name'],owner.name)]
    if related:
        report.update(reason='same_or_locally_inherited_owner; property may participate in validation',status='unknown')
        return report
    # Absence of a static edge is not proof; caller/receiver discovery is required.
    report.update(status='target_context_risk',risk=True,reason='explicit_producer_requirement_with_different_owner_zero_argument_property',
        target_owner=owner.name,target_decorators=decorators,target_arguments=arguments,
        evidence=clauses,needed_context='source definition/inheritance/callers of public API; do not infer a replacement target',
        unknowns=['constructor capitalization is a static convention, not proven type',
                  'cross-file inheritance, dynamic dispatch and hidden property calls not resolved'])
    return report
