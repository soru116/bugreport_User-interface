"""Offline, public-evidence-only repair scope planning; never generates a patch."""
import ast
import hashlib
import re
from pathlib import PurePosixPath

from .local_fim import PUBLIC_FIELDS

VERSION = 'scope-compatibility-plan-v1'
ADD_PARAMETER = re.compile(
    r'\b(?:add|adding|introduce|introducing|new|additional)\b[^\n.]{0,100}'
    r'\b(?:parameters?|keyword\s+arguments?|arguments?)\b', re.I)


def public_text(ticket):
    """Exclude benchmark labels and preserve only unique public text fields."""
    parts=[]
    for key in PUBLIC_FIELDS:
        value=ticket.get(key)
        if isinstance(value,str):
            value=value.replace('\r\n','\n').strip()
            if value and value not in parts:parts.append(value)
    return '\n'.join(parts)


def _snippets(public):
    """Parse fenced Python examples without executing imports or expressions."""
    found=[]
    for match in re.finditer(r'```(?:python|py)?[ \t]*\n(.*?)```',public,re.S|re.I):
        code=match.group(1)
        try:
            found.append((ast.parse(code),code,match.start(1)))
        except SyntaxError:
            # In mixed traceback/doctest examples, accept only independently
            # parseable prompt lines, never join incomplete code into a guess.
            offset=match.start(1)
            for line in code.splitlines(keepends=True):
                candidate=re.sub(r'^\s*>>>\s?','',line)
                try:tree=ast.parse(candidate)
                except SyntaxError:pass
                else:found.append((tree,candidate,offset))
                offset+=len(line)
    return found


def _root_name(expression):
    while isinstance(expression,ast.Attribute):expression=expression.value
    return expression.id if isinstance(expression,ast.Name) else None


def plan_scope(raw,span,ticket,path,max_added_parameters=2):
    """Describe bounded options; absence of evidence is not semantic compatibility.

    Only explicit additive-parameter proposals with a target-matching example
    can propose a signature hole. Unexpected-keyword errors alone cannot do so.
    """
    from .generator import _target_function_node,_function_suite_start_line
    text=raw.decode('utf-8-sig')
    node=_target_function_node(text,span['symbol'])
    public=public_text(ticket)
    snippets=_snippets(public)
    target=node.name
    class_name=span['symbol'].split('.')[-2] if '.' in span['symbol'] else None
    aliases={class_name} if class_name else set()
    # Bind public receivers only when the example names the predicted class,
    # including e.g. object = Class.read(...). Resolve aliases across snippets.
    for tree,_,_ in snippets:
        for statement in ast.walk(tree):
            if isinstance(statement,ast.Assign) and isinstance(statement.value,ast.Call):
                if _root_name(statement.value.func) in aliases:
                    aliases.update(n.id for n in statement.targets if isinstance(n,ast.Name))
    accepted={a.arg for a in node.args.args+node.args.kwonlyargs}
    all_arguments=accepted|{a.arg for a in node.args.posonlyargs}
    evidence=[]
    for tree,code,base_offset in snippets:
        lines=code.splitlines(keepends=True)
        for call in ast.walk(tree):
            if not isinstance(call,ast.Call):continue
            name=call.func.id if isinstance(call.func,ast.Name) else call.func.attr if isinstance(call.func,ast.Attribute) else None
            if name!=target:continue
            if class_name and (not isinstance(call.func,ast.Attribute) or _root_name(call.func.value) not in aliases):continue
            if not class_name and isinstance(call.func,ast.Attribute):
                qualifier=call.func.value.id if isinstance(call.func.value,ast.Name) else call.func.value.attr if isinstance(call.func.value,ast.Attribute) else None
                if qualifier!=PurePosixPath(path.replace('\\','/')).stem:continue
            unknown=[k.arg for k in call.keywords if k.arg and k.arg not in accepted]
            if not unknown or node.args.kwarg:continue
            offset=base_offset+sum(len(line) for line in lines[:call.lineno-1])
            context=public[max(0,offset-400):offset]
            # A new alternative/heading resets intent from an earlier option.
            boundaries=list(re.finditer(r'(?m)^\s*(?:#\s*)?(?:Option\s+[A-Z]:|#{1,6}\s)',context))
            if boundaries:context=context[boundaries[-1].start():]
            intent=ADD_PARAMETER.search(context)
            evidence.append({'call':ast.get_source_segment(code,call) or ast.unparse(call),
                             'unknown_keywords':unknown,'explicit_add_parameter':bool(intent),
                             'intent_excerpt':intent.group(0) if intent else '',
                             'positional_only_keyword_conflict':sorted(set(unknown)&all_arguments)})
    qualified=[e for e in evidence if e['explicit_add_parameter'] and not e['positional_only_keyword_conflict']]
    proposal_sets={tuple(sorted(e['unknown_keywords'])) for e in qualified}
    plan={'version':VERSION,'file':path,'symbol':span['symbol'],
          'origin':'predicted_fl_plus_public_api_evidence',
          'source_sha256':hashlib.sha256(raw).hexdigest(),
          'current_scope':span.get('generation_scope','function_body'),
          'current_signature_locked':bool(span.get('lock_signature')),
          'evidence':evidence,'status':'no_explicit_conflict','action':'retain_scope',
          'semantic_compatibility_verified':False,'executable':False,'model_calls':0,
          'note':'Plan only. No gold patch, model inference, source edits or automatic repair.'}
    if evidence and not qualified:
        plan.update(status='needs_requirement_review',action='retain_scope_with_diagnostic',
                    reason='unknown_keyword_without_unambiguous_additive_api_request')
    if len(proposal_sets)>1:
        plan.update(status='ambiguous_public_proposals',action='stop_scope_expansion',
                    reason='multiple_different_parameter_sets_for_target')
    elif len(proposal_sets)==1:
        names=list(next(iter(proposal_sets)))
        if len(names)>max_added_parameters:
            plan.update(status='expansion_limit_exceeded',action='stop_scope_expansion',
                        reason='more_than_'+str(max_added_parameters)+'_new_parameters')
        elif span.get('lock_signature'):
            suite=node.lineno+_function_suite_start_line(''.join(text.splitlines(keepends=True)[node.lineno-1:]))-1
            plan.update(status='incompatible_with_requested_option',action='propose_signature_and_behavior_fim',
                        reason='public_option_requires_parameters_outside_locked_scope',
                        proposed_parameters=names,proposal_is_not_mandatory=True,
                        proposal={
                            'max_new_optional_parameters':max_added_parameters,
                            'generation_call_limit':3,'repair_round_limit':2,
                            'holes':[
                                {'kind':'optional_parameter_fragment','file':path,'symbol':span['symbol'],
                                 'header_lines':[node.lineno,suite-1],'allowed_new_names':names,'generation_call_budget':1},
                                {'kind':'behavior','file':path,'symbol':span['symbol'],
                                 'original_scope':span.get('generation_scope','function_body'),
                                 'lines':[span['line_start'],span['line_end']],'generation_call_budget':1}],
                            'retry':'At most one behavior retry after combined validation failure.',
                            'gates':['Preserve existing parameters/defaults/decorators/return annotation.',
                                     'New parameters must be optional; preserve old call forms.',
                                     'Require executable body change; signature-only/no-op is not success.',
                                     'Merge both holes atomically; partial edits are not candidates.',
                                     'Validate full AST, scope, names, helper calls and git apply before tests.'],
                            'fallback':'Stop if preserving old calls or the two-hole bound cannot express the repair.'})
    return plan


def diagnose_expression_output(candidate,original_expression):
    """Describe saved first-line no-op/continuation; never salvage or accept it."""
    raw=candidate.get('raw_response') or (candidate.get('backend_response') or {}).get('response') or ''
    lines=raw.splitlines()
    diagnostic={'action':'diagnostic_only','candidate_accepted':False,
                'first_line_expression_available':False}
    if not lines:return diagnostic
    try:
        generated=ast.parse(lines[0],mode='eval')
        original=ast.parse(original_expression,mode='eval')
    except (SyntaxError,TypeError):return diagnostic
    same=ast.dump(generated,include_attributes=False)==ast.dump(original,include_attributes=False)
    diagnostic.update(first_line_expression_available=True,first_line_no_op=same,
                      continuation_after_first_line=any(line.strip() for line in lines[1:]))
    if same and diagnostic['continuation_after_first_line']:
        diagnostic['feedback']='The first complete expression is unchanged from the original; subsequent statements escape the expression hole. No executable repair was made. Review requirement/scope compatibility before retrying.'
    elif same:
        diagnostic['feedback']='The expression is unchanged from the original; no executable repair was made.'
    return diagnostic
