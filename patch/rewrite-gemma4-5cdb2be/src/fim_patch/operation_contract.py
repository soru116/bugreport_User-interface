"""Opt-in source contract diagnostics; never accept, reject or execute a patch."""
import ast
from .public_behavior import expectation_evidence

VERSION = 'operation-contract-diagnostic-v1'
PREVIEW_VERSION = 'operation-contract-preview-v1'
RETRY_VERSION = 'contract-feedback-retry-v1'


def feedback_block(diagnostic):
    text = ''.join('# '+line+'\n' for line in diagnostic['feedback_preview'].splitlines())
    for token in ('<PRE>','<MID>','<SUF>','<EOT>','<FILL_ME>'):
        text = text.replace(token,'[marker removed]')
    return text


def diagnose(before, after, symbol, ticket, *, adapters=()):
    from .domain_adapters import validate_adapters,VERSIONS
    adapters=validate_adapters(adapters)
    from .generator import _target_function_node
    old = _target_function_node(before, symbol)
    new = _target_function_node(after, symbol)
    def calls(node):
        return sorted({ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)})
    facts = dict(removed_calls=sorted(set(calls(old))-set(calls(new))),
                 added_calls=sorted(set(calls(new))-set(calls(old))),
                 target_ast_changed=ast.dump(old, include_attributes=False) != ast.dump(new, include_attributes=False))
    def handlers(node):
        return {ast.unparse(n.type) if n.type is not None else 'bare except'
                for n in ast.walk(node) if isinstance(n, ast.ExceptHandler)}
    facts['removed_exception_handlers'] = sorted(handlers(old)-handlers(new))
    parameters = {a.arg for a in old.args.posonlyargs+old.args.args+old.args.kwonlyargs} - {'self','cls'}
    multiplications = []
    for node in ast.walk(new):
        if isinstance(node, (ast.BinOp, ast.AugAssign)) and isinstance(node.op, ast.Mult):
            factor = node.right if isinstance(node, ast.BinOp) else node.value
            if isinstance(factor, ast.Name) and factor.id in parameters:
                multiplications.append(dict(line=node.lineno, expression=ast.unparse(node), parameter=factor.id))
    facts['parameter_multiplications'] = multiplications
    from .domain_adapters import conversion_purpose
    label = conversion_purpose(before,symbol,old) if 'conversion' in adapters else None
    facts['related_source_purpose'] = label
    facts['candidate_docstring'] = ast.get_docstring(new)
    conflict = bool(label and multiplications and facts['target_ast_changed'])
    public = expectation_evidence(ticket)
    diagnostic = dict(version=VERSION, status='potential_operation_drift' if conflict else 'structural_evidence_only',
                      domain_adapters={name:VERSIONS[name] for name in adapters},
                      source_facts=facts, public_expected_evidence=public,
                      diagnostic_only=True, automatic_rejection=False,
                      official_tests_run=False, semantic_repair_verified=False,
                      unknowns=['Runtime/operator dispatch, API semantics and expected outputs are not verified.'])
    messages = ['Unverified contract diagnostic data.']
    for item in public:
        messages.append('Public expected ('+item['field']+':'+str(item['line'])+'): '+item['text'])
    if conflict:
        messages.append('Potential operation drift. Source comment, line '+str(label['line'])+': '+label['text'])
        messages.append('Candidate multiplies directly by parameter(s): '+', '.join(sorted({m['parameter'] for m in multiplications}))+'.')
    if facts['removed_calls']:
        messages.append('Removed calls: '+', '.join(facts['removed_calls'])+'. Removal alone is not proof of failure.')
    if facts['removed_exception_handlers']:
        messages.append('Removed handlers: '+', '.join(facts['removed_exception_handlers'])+'. Verify fallback contracts.')
    tail = 'Check operation contracts and fix the public issue. Preserve unrelated behavior. Output only the fixed-signature FIM body.'
    # Retain whole messages; never cut a requirement mid-sentence.
    feedback = []
    for message in messages:
        if len('\n'.join(feedback+[message,tail])) <= 600:
            feedback.append(message)
    diagnostic['feedback_preview'] = '\n'.join(feedback+[tail])
    return diagnostic


def capture(before, after, symbol, ticket, prefix, suffix, output_limit, *, counter=None, context_limit=None, adapters=()):
    """Return diagnostics/previews without any backend request or verdict change."""
    diagnostic = dict(version=VERSION,status='unknown',diagnostic_only=True,
                      automatic_rejection=False,official_tests_run=False,semantic_repair_verified=False)
    try:
        diagnostic = diagnose(before,after,symbol,ticket,adapters=adapters)
    except Exception as exc:
        diagnostic['reason'] = 'diagnostic_unavailable:'+type(exc).__name__
        return diagnostic, dict(version=PREVIEW_VERSION,status='unavailable',not_dispatched=True)
    preview = dict(version=PREVIEW_VERSION,status='unavailable',not_dispatched=True)
    try:
        marker = '# Source context follows; fill the missing code.\n'
        if prefix.count(marker) != 1:
            preview['reason'] = 'unverified_insertion_boundary'
            return diagnostic, preview
        feedback = feedback_block(diagnostic)
        proposed = prefix.replace(marker,feedback+marker)
        preview.update(status='ready',prefix=proposed,suffix=suffix,feedback=diagnostic['feedback_preview'],
                       feedback_chars=len(diagnostic['feedback_preview']),output_limit=output_limit,
                       suffix_unchanged=True,original_input_tokens=None,proposed_input_tokens=None,
                       fits_model_context=None,budget_status='unmeasured')
        if counter is not None:
            original_tokens = counter.count(prefix,suffix)
            proposed_tokens = counter.count(proposed,suffix)
            preview.update(original_input_tokens=original_tokens,proposed_input_tokens=proposed_tokens,
                           budget_status='verified_tokenizer',
                           fits_model_context=proposed_tokens+output_limit<=context_limit if context_limit is not None else None)
    except Exception as exc:
        preview.update(status='unavailable',reason='preview_unavailable:'+type(exc).__name__,
                       original_input_tokens=None,proposed_input_tokens=None,budget_status='unmeasured')
        preview.pop('prefix',None)
        preview.pop('suffix',None)
    return diagnostic, preview
