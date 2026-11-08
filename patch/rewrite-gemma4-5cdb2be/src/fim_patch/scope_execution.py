"""Bounded opt-in additive signature + behavior FIM, composed in memory."""
import ast
import copy
import io
import re
import tokenize

from .scope_planning import plan_scope, public_text

VERSION = 'two-hole-fim-v6'


def _signature_hole(text, symbol):
    from .generator import _target_function_node
    node = _target_function_node(text, symbol)
    lines = text.splitlines(keepends=True)
    def offset(pos):
        return sum(len(s) for s in lines[:pos[0]-1]) + pos[1]
    started = False
    depth = 0
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.NAME and token.string == 'def' and token.start[0] == node.lineno:
            started = True
        if not started or token.type != tokenize.OP:
            continue
        if token.string == '(':
            depth += 1
        elif token.string == ')':
            depth -= 1
            if depth == 0:
                return offset(token.start), node
    raise ValueError('signature_hole_unavailable')


def signature_anchor(text, symbol):
    """Insert a host-owned separator at a token boundary, before trailing trivia."""
    close, node = _signature_hole(text,symbol)
    if node.args.kwarg:
        raise ValueError('signature_anchor_unavailable: **kwargs requires a different insertion scope')
    lines = text.splitlines(keepends=True)
    def offset(pos):return sum(len(s) for s in lines[:pos[0]-1])+pos[1]
    tokens = [t for t in tokenize.generate_tokens(io.StringIO(text).readline)
              if t.type not in {tokenize.NL,tokenize.NEWLINE,tokenize.COMMENT,
                                tokenize.INDENT,tokenize.DEDENT,tokenize.ENDMARKER}
              and offset(t.end)<=close]
    previous=tokens[-1]
    empty=previous.string=='('
    comma=previous.string==','
    already_keyword_only=bool(node.args.vararg or node.args.kwonlyargs)
    separator=('' if empty else ' ' if comma else ', ')
    if not already_keyword_only:separator+='*, '
    return offset(previous.end),separator,node


def scope_brief(ticket,plan,max_chars=1600):
    """Prioritize complete public proposal/call evidence, then whole prose units."""
    public=public_text(ticket)
    units=[]
    for evidence in plan.get('evidence',[]):
        if not evidence.get('explicit_add_parameter'):continue
        intent=evidence.get('intent_excerpt','')
        line=next((line.strip() for line in public.splitlines()
                   if intent and intent.casefold() in line.casefold()),intent)
        for unit in (line,evidence.get('call','')):
            if unit and unit not in units:units.append(unit)
    def render(values):return ''.join('# '+line+'\n' for value in values for line in value.splitlines())
    if len(render(units))>max_chars:
        raise ValueError('scope_public_evidence_exceeds_bound')
    mandatory=len(units)
    # Remove complete fenced blocks from supplementary prose, so unrelated
    # alternatives and half-open examples cannot leak into the parameter hole.
    prose=re.sub(r'```[^\n]*\n(?:.*?```|.*\Z)','',public,flags=re.S)
    prose_lines=[]
    seen_lines=set()
    for line in prose.splitlines():
        if line.lstrip().startswith('#'):continue
        key=re.sub(r'^#+\s*','',line.strip()).strip()
        if not key:
            prose_lines.append('')
        elif key not in seen_lines:
            seen_lines.add(key)
            prose_lines.append(line)
    prose='\n'.join(prose_lines)
    seen={re.sub(r'^#+\s*','',u).strip() for u in units}
    omitted=0
    for paragraph in re.split(r'\n\s*\n',prose):
        paragraph=' '.join(line.strip() for line in paragraph.splitlines()).strip()
        if not paragraph:continue
        for unit in re.split(r'(?<=[.!?。！？])\s+',paragraph):
            key=re.sub(r'^#+\s*','',unit).strip()
            if not key or key in seen:continue
            seen.add(key)
            if len(render(units+[key]))<=max_chars:units.append(key)
            else:omitted+=1
    return render(units),{'strategy':'complete_public_evidence_v2','max_chars':max_chars,
                           'chars':len(render(units)),'mandatory_units':mandatory,
                           'omitted_units':omitted,'mid_unit_truncation':False}


def parameter_fragment(middle):
    """One complete parameter list, never closing the host-owned header."""
    value=middle.replace('\r\n','\n').strip()
    if not value or '\r' in value:
        raise ValueError('signature_fragment_contract_violation: empty or invalid newline')
    try:
        tokens=list(tokenize.generate_tokens(io.StringIO(value).readline))
        if any(t.type==tokenize.COMMENT for t in tokens):
            raise ValueError('signature_fragment_contract_violation: no comments')
        tree=ast.parse('def _fragment(*, '+value+'):\n    pass\n')
        if len(tree.body)!=1 or not isinstance(tree.body[0],ast.FunctionDef):
            raise ValueError('signature_fragment_contract_violation: escaped parameter list')
        if ast.dump(tree.body[0].body[0])!=ast.dump(ast.Pass()) or len(tree.body[0].body)!=1:
            raise ValueError('signature_fragment_contract_violation: no function body')
        if tree.body[0].args.kwarg:
            raise ValueError('signature_fragment_contract_violation: no variadic parameter')
    except (SyntaxError,tokenize.TokenError,IndentationError) as exc:
        raise ValueError('signature_fragment_contract_violation: expected only parameter definitions') from exc
    return value


def _closure_tokens(suffix):
    """Read the original outer ')' through header ':', ignoring source trivia."""
    tokens=[]
    stack=[]
    pairs={')':'(',']':'[','}':'{'}
    for token in tokenize.generate_tokens(io.StringIO(suffix).readline):
        if token.type in {tokenize.NL,tokenize.NEWLINE,tokenize.COMMENT,
                          tokenize.INDENT,tokenize.DEDENT,tokenize.ENDMARKER}:continue
        if not tokens:
            if token.type!=tokenize.OP or token.string!=')':
                raise ValueError('signature_boundary_unavailable: suffix must close the original parameter list')
            tokens.append((token.type,token.string))
            continue
        tokens.append((token.type,token.string))
        if token.type==tokenize.OP:
            if token.string in '([{':stack.append(token.string)
            elif token.string in ')]}':
                if not stack or stack.pop()!=pairs[token.string]:
                    raise ValueError('signature_boundary_unavailable: mismatched header delimiters')
            elif token.string==':' and not stack:return tokens
    raise ValueError('signature_boundary_unavailable: no complete header closure')


def extract_signature(middle,suffix):
    """Extract only at a token-witnessed copy of the original header closure.

    Never infer missing syntax or evaluate defaults. A successful extraction
    still needs the full additive/AST/scope gate before becoming a draft.
    """
    ignored={tokenize.NL,tokenize.NEWLINE,tokenize.COMMENT,
             tokenize.INDENT,tokenize.DEDENT,tokenize.ENDMARKER}
    try:
        expected=_closure_tokens(suffix)
        lines=middle.splitlines(keepends=True)
        def offset(pos):return sum(len(s) for s in lines[:pos[0]-1])+pos[1]
        stack=[]
        pairs={')':'(',']':'[','}':'{'}
        boundary=None
        matched=[]
        for token in tokenize.generate_tokens(io.StringIO(middle).readline):
            if token.type in ignored:continue
            if boundary is not None:
                matched.append((token.type,token.string))
            elif token.type==tokenize.OP:
                if token.string in '([{':stack.append(token.string)
                elif token.string in ')]}':
                    if token.string==')' and not stack:
                        boundary=offset(token.start)
                        matched.append((token.type,token.string))
                    elif not stack or stack.pop()!=pairs[token.string]:
                        raise ValueError('signature_boundary_unavailable: mismatched parameter delimiters')
            if matched:
                if matched!=expected[:len(matched)]:
                    raise ValueError('signature_boundary_unavailable: closure differs from original header')
                if len(matched)==len(expected):
                    fragment=parameter_fragment(middle[:boundary])
                    return fragment,{'version':'signature-token-boundary-v1','status':'extracted',
                        'boundary_offset':boundary,'boundary_end':offset(token.end),
                        'closure_tokens':[text for _,text in expected],
                        'omitted_characters':len(middle)-boundary,'synthesized_parameters':False}
    except (tokenize.TokenError,IndentationError,SyntaxError) as exc:
        raise ValueError('signature_boundary_unavailable: incomplete token stream') from exc
    raise ValueError('signature_boundary_unavailable: no witnessed complete header closure')


def check_additive_signature(before, after, symbol, allowed, *, signature_only=False):
    """Only new literal-default keyword-only parameters; old contract intact."""
    from .generator import _target_function_node, check_locked_function_contract
    old = _target_function_node(before, symbol)
    new = _target_function_node(after, symbol)
    expected = set(allowed)
    old_names = {a.arg for a in old.args.kwonlyargs}
    additions = [(a, d) for a, d in zip(new.args.kwonlyargs, new.args.kw_defaults)
                 if a.arg not in old_names]
    if not expected or len(expected) > 2 or {a.arg for a, _ in additions} != expected:
        raise ValueError('additive_signature_violation: only planned keyword-only names allowed')
    for arg, default in additions:
        if arg.annotation is not None or default is None:
            raise ValueError('additive_signature_violation: new parameters must be optional and unannotated')
        try:
            ast.literal_eval(default)
        except (ValueError, TypeError, SyntaxError) as exc:
            raise ValueError('additive_signature_violation: new default must be a literal') from exc
    old_tree, new_tree = ast.parse(before), ast.parse(after)
    target = _target_function_node(after, symbol)
    # Find the equivalent node in the mutable tree, rather than mutating source.
    target = next(n for n in ast.walk(new_tree) if isinstance(n, type(target))
                  and n.lineno == target.lineno and n.name == target.name)
    retained = [(a, d) for a, d in zip(target.args.kwonlyargs, target.args.kw_defaults)
                if a.arg not in expected]
    target.args.kwonlyargs = [a for a, _ in retained]
    target.args.kw_defaults = [d for _, d in retained]
    check_locked_function_contract(old_tree, new_tree, symbol)
    if signature_only and ast.dump(old_tree,include_attributes=False)!=ast.dump(new_tree,include_attributes=False):
        raise ValueError('signature_fragment_changed_body_or_outside_target')


def _rebase_span(span, draft, insertion, delta):
    """Reconstruct coordinates from the unchanged body, after header insertion."""
    text = draft.decode('utf-8-sig')
    if insertion > span['start']:
        raise ValueError('signature_hole_overlaps_behavior')
    rebased = dict(span)
    start, end = span['start']+delta, span['end']+delta
    if text[start:end] != span['original_middle']:
        raise ValueError('behavior_anchor_changed_by_signature')
    rebased.update(start=start, end=end, prefix=text[:start], suffix=text[end:],
                   original_middle=text[start:end], line_start=text[:start].count('\n')+1,
                   line_end=text[:end].count('\n')+(0 if text[:end].endswith('\n') else 1))
    return rebased


def execute_scope(generator, ticket, location, repo, row, raw, commit, path, span, result):
    """Return None to retain default scope, otherwise return one atomic result."""
    from .generator import (build_patch, body_token_budget, function_span, git, sha,
                            source_for, check_body_retention)
    from .local_fim import prompt_context
    from .helper_contracts import context, check_calls
    from .generation_records import annotate_generation
    from .backend import GenerationError
    from .behavior_feedback import (diagnose_behavior, retry_feedback, public_expectations,
                                    assess_option_usage, option_retry_feedback)
    from .inner_expression import narrow_container_span, check_inner_reconstruction
    import subprocess

    plan = plan_scope(raw, span, ticket, path)
    result['scope_plan'] = plan
    result['scope_execution'] = VERSION
    if plan['action'] in {'retain_scope', 'retain_scope_with_diagnostic'}:
        return None
    result.update(base_commit=commit, source_file_sha256={path:sha(raw)},
                  bug_location={'file':path,'function':span['symbol']},
                  prompt_version=VERSION, model_calls=0, scope_history=[],
                  span={k:v for k,v in span.items() if k not in {'prefix','suffix','original_middle'}})
    if plan['action'] != 'propose_signature_and_behavior_fim':
        result.update(patch_status='blocked_scope_expansion', explanation=plan.get('reason'))
        return annotate_generation(result)
    errors = (ValueError, SyntaxError, OSError, subprocess.SubprocessError)
    before = raw.decode('utf-8-sig')
    encoding = 'utf-8-sig' if span['bom'] else 'utf-8'
    newline = span['newline']
    candidate = {'candidate_id':1, 'status':'rejected', 'seed':generator.seed}
    result['candidates'].append(candidate)
    selected = None
    try:
        insertion, separator, node = signature_anchor(before, span['symbol'])
        # Never run imports/defaults/source to examine the contract.
        line_start = sum(len(s) for s in before.splitlines(keepends=True)[:node.lineno-1])
        header = before[line_start:insertion]+separator
        if len(header)>1800:
            raise ValueError('signature_context_exceeds_bound')
        if getattr(generator,'fim_context_mode','local')=='file':header=before[:insertion]+separator
        brief, result['scope_public_context'] = scope_brief(ticket,plan)
        request = ('# Add only these optional keyword-only parameters: '+', '.join(plan['proposed_parameters'])+
                   '. The separators and * are already in Prefix. Output only name=literal-default definitions; '
                   'no leading comma, *, closing parenthesis, function body or other function.\n')
        from .generator import _function_suite_start_line
        suite=node.lineno+_function_suite_start_line(''.join(before.splitlines(keepends=True)[node.lineno-1:]))-1
        header_end=sum(len(s) for s in before.splitlines(keepends=True)[:suite-1])
        suffix = before[insertion:header_end] + ' '*(node.col_offset+4)+'pass\n'
        if getattr(generator,'fim_context_mode','local')=='file':suffix=before[insertion:]
        result['signature_anchor']={'version':'keyword-only-anchor-v2','insertion':insertion,
                                    'host_separator':separator,'middle_contract':'parameter_definitions_only'}
        # Prefer a short brief without discarding mandatory public evidence.
        try:
            behavior_brief, behavior_context = scope_brief(ticket,plan,max_chars=600)
        except ValueError as exc:
            if str(exc) != 'scope_public_evidence_exceeds_bound':
                raise
            behavior_brief, behavior_context = scope_brief(ticket,plan,max_chars=1600)
            behavior_context['fallback'] = 'mandatory_public_evidence_exceeds_preferred_600_chars'
        result['behavior_public_context'] = behavior_context
        expectations = public_expectations(ticket,plan)
        expected_context = ''.join('# Public expectation: "'+unit+'".\n' for unit in expectations)
        result['behavior_public_expectations'] = expectations
        model = generator.backend.describe()
        result['model'] = model
        def infill(kind, prefix, suffix, budget, feedback=''):
            if result['model_calls'] >= 3:
                raise ValueError('generation_call_limit_exceeded')
            record = {'hole':kind,'call':result['model_calls']+1,
                      'prompt_prefix':prefix,'prompt_suffix':suffix,'max_new_tokens':budget,
                      'feedback':feedback}
            result['scope_history'].append(record)
            result['model_calls'] += 1
            try:
                generated = generator.backend.infill(prefix, suffix,
                    seed=generator.seed+result['model_calls']-1, max_new_tokens=budget)
            except GenerationError as exc:
                record.update(status='failed',reason=str(exc),backend_response=exc.response)
                raise
            record['generation'] = copy.deepcopy(generated)
            if kind == 'behavior':
                record['behavior_diagnostic'] = diagnose_behavior(
                    generated['middle'], rebased['original_middle'],
                    rebased.get('generation_scope', 'function_body'),
                    plan['proposed_parameters'], ticket, plan)
            if kind=='signature' and generated.get('stop_reason') in {'length','model_eos','visible_eot'}:
                try:
                    # A real model terminator still permits normal parameter-only output.
                    if generated.get('stop_reason')!='length':
                        parameter_fragment(generated['middle'])
                    else:
                        raise ValueError('length needs a witnessed boundary')
                except ValueError:
                    try:
                        extracted,boundary=extract_signature(generated['middle'],suffix)
                    except ValueError as exc:
                        record['signature_extraction']={'status':'not_extracted','reason':str(exc)}
                    else:
                        record.update(status='returned',signature_extraction=boundary,
                                      untrimmed_middle=generated['middle'],normalized_middle=extracted)
                        return extracted,record
            if generated.get('stop_reason') not in {'model_eos','visible_eot'}:
                raise ValueError('incomplete_'+kind+': requires model end marker')
            record['status'] = 'returned'
            return generated['middle'], record
        fragment, signature_record = infill('signature',brief+request+header, suffix,
                                            min(128,generator.backend.max_new_tokens))
        fragment = parameter_fragment(fragment)
        insertion_text = (separator+fragment).replace('\n',newline)
        draft_text = before[:insertion]+insertion_text+before[insertion:]
        ast.parse(draft_text)
        check_additive_signature(before,draft_text,span['symbol'],plan['proposed_parameters'],signature_only=True)
        signature_record.update(status='validated_draft',draft_sha256=sha(draft_text.encode(encoding)))
        draft = draft_text.encode(encoding)
        rebased = _rebase_span(span,draft,insertion,len(insertion_text))
        rebased, result['behavior_anchor'] = narrow_container_span(draft, rebased)
        result['behavior_span'] = {k:v for k,v in rebased.items()
                                  if k not in {'prefix','suffix','original_middle'}}
        if getattr(generator,'fim_context_mode','local')=='file':
            prefix,suffix=rebased['prefix'],rebased['suffix']
            context_info=dict(strategy='complete_file_fim_context',truncated=False)
        else:prefix, suffix, context_info = prompt_context(draft,rebased,retain_docstring=True)
        result['prompt_context'] = context_info
        # Keep the full public brief for signature planning, but give behavior
        # attempts a concise brief and explicit expected result from attempt 1.
        feedback = ''
        for attempt in range(2):
            if getattr(generator.backend,'remaining',1)==0:
                result['scope_stop_reason']='shared_generation_budget_exhausted'
                break
            record = None
            if candidate.get('apply_check_status') == 'passed':
                # Preserve the first static candidate even if the soft retry
                # later fails, repeats it, or encounters a backend error.
                candidate = {'candidate_id':len(result['candidates'])+1,
                             'status':'rejected','seed':generator.seed+result['model_calls']}
                result['candidates'].append(candidate)
            try:
                instruction = ('# Repair '+span['symbol']+' using the new optional parameter(s). '
                               'Change executable behavior; output only the '+rebased.get('generation_scope','function_body')+'.\n')
                if rebased.get('inner_expression_contract'):
                    instruction += '# Output only one container element expression; outer delimiters are already in Prefix/Suffix.\n'
                prompt = behavior_brief+expected_context+context(draft_text,span['symbol'])+instruction
                if feedback:
                    prompt += '# Previous attempt feedback: '+feedback[:600].replace('\n',' ')+'\n'
                middle, record = infill('behavior',prompt+prefix,suffix,
                    body_token_budget(span,generator.backend.max_new_tokens),feedback)
                middle = check_inner_reconstruction(rebased,middle)
                if rebased.get('generation_scope')!='python_expression':
                    if middle and not middle.endswith('\n'):
                        middle += '\n'
                        record['trailing_newline_added'] = True
                    check_body_retention(middle,rebased,generator.min_body_retention)
                # This gate compares behavior against the signature draft, so
                # signature-only edits cannot satisfy executable-change checks.
                _, after = build_patch(draft,rebased,middle,path)
                after_text = after.decode('utf-8-sig')
                check_additive_signature(before,after_text,span['symbol'],plan['proposed_parameters'])
                candidate['helper_call_check'] = check_calls(draft_text,after_text,span['symbol'])
                full = function_span(raw,row,expand_truncated_end=True,include_signature=True)
                delta = len(after_text)-len(before)
                replacement = after_text[full['start']:full['end']+delta]
                patch, rebuilt = build_patch(raw,full,replacement,path)
                if rebuilt != after:
                    raise ValueError('atomic_reconstruction_mismatch')
                try:
                    source_for(ticket,location,repo,row)
                except (ValueError,OSError) as exc:
                    raise OSError('source_snapshot_recheck_failed: '+str(exc)) from exc
                git(repo,'apply','--check','--whitespace=nowarn',data=patch.encode('utf-8'))
                record['status'] = 'combined_validated'
                candidate.update(status='generated',static_status='passed',apply_check_status='passed',
                                 patch=patch,after_sha256=sha(after),modified_files=[path],origin_call=record['call'])
                assessment = assess_option_usage(after_text,span['symbol'],plan['proposed_parameters'],expectations)
                if getattr(generator,'source_none_probe',False):
                    from .none_contract import assess_none_contract
                    candidate['source_contract_check']=assess_none_contract(before,after_text,span['symbol'])
                    if candidate['source_contract_check']['status']=='failed':raise ValueError('source_none_regression')
                candidate['behavior_assessment'] = assessment
                record['behavior_assessment'] = assessment
                from .generation_records import logic_fingerprint,LOGIC_VERSION
                candidate.update(logic_sha256=logic_fingerprint([(path,after)]),logic_fingerprint_version=LOGIC_VERSION,
                    executable_logic_sha256=logic_fingerprint([(path,after)],executable=True),executable_fingerprint_version='python-executable-ast-v1')
                from .source_api_integration import check_candidate
                if not check_candidate(generator,candidate,patch):
                    raise ValueError('source_api_call_incompatible: '+candidate['failure_feedback'])
                if getattr(generator,'test_first_scope',False) and not generator.validator:
                    selected=candidate
                    break
                if getattr(generator,'test_first_scope',False) and generator.validator:
                    try:report=generator.validator(patch,repo)
                    except errors as exc:report=dict(status='error',reason='validator_environment_error',feedback=str(exc))
                    candidate['validation']=report;record['validation']=report
                    if report.get('status')!='plausible':
                        candidate.update(status='rejected',reason='validation_failed')
                        if report.get('status')=='error':break
                        from .iterative import feedback_block
                        feedback=feedback_block(candidate,compact=True) if getattr(generator,'forward_test_feedback',True) else ''
                        record.update(status='test_failed_behavior_retry',retry_feedback=feedback)
                        if attempt==0:continue
                        break
                    selected=candidate
                    break
                # Prefer evidence of parameter use, not merely a later output.
                # This heuristic is explicitly not a semantic correctness claim.
                if selected is None or len(assessment['parameters_not_referenced_in_target_body']) < len(
                        selected['behavior_assessment']['parameters_not_referenced_in_target_body']):
                    selected = candidate
                if assessment['retry_recommended'] and attempt == 0 and result['model_calls']<3:
                    feedback = option_retry_feedback(assessment)
                    record.update(status='static_valid_behavior_retry',retry_feedback=feedback)
                    continue
                break
            except errors as exc:
                reason = str(exc)
                if record is None and result['scope_history'] and result['scope_history'][-1]['hole']=='behavior':
                    record = result['scope_history'][-1]
                if record is not None:
                    record.update(status='rejected',reason=reason)
                feedback = retry_feedback(reason, record.get('behavior_diagnostic') if record else None)
                if record is not None:
                    record['retry_feedback'] = feedback
                candidate['reason'] = reason
                # Infrastructure/backend errors do not trigger semantic retries.
                if isinstance(exc,(OSError,subprocess.SubprocessError,GenerationError)):
                    break
        if selected is not None:
            # A source change during the extra infill must not make a stale
            # candidate the final selected result. Its static evidence remains.
            try:
                source_for(ticket,location,repo,row)
            except (ValueError,OSError) as exc:
                result.update(patch_status='no_valid_candidate',patch='',modified_files=[],
                              requires_manual_patch=True,explanation='source_snapshot_recheck_failed: '+str(exc))
                return annotate_generation(result)
            patch = selected['patch']
            assessment = selected['behavior_assessment']
            result.update(patch_status='generated',patch=patch,apply_status='passed',
                          selected_candidate_id=selected['candidate_id'],modified_files=[path],
                          requires_manual_patch=False,behavior_assessment=assessment,
                          behavior_retry_performed=any(h.get('status')=='static_valid_behavior_retry' for h in result['scope_history']),
                          selection_reason=('first candidate passing configured tests' if getattr(generator,'test_first_scope',False) and generator.validator else 'first static candidate, unverified without tests' if getattr(generator,'test_first_scope',False) else 'first static candidate, replaced only when a later static candidate references more planned parameters'),
                          explanation='Static/apply candidate retained; requested behavior and official repair remain unverified.')
            if generator.validator:
                try:
                    report = selected.get('validation') if getattr(generator,'test_first_scope',False) else None
                    if report is None:report = generator.validator(patch,repo)
                except errors as exc:
                    report = {'status':'error','feedback':str(exc),'environment_error':True}
                selected['validation'] = report
                result['validation_status'] = report.get('status','not_run')
                if report.get('status')!='plausible':
                    selected.update(status='rejected',reason='validation_failed')
                    result.update(patch_status='no_valid_candidate',patch='',modified_files=[],requires_manual_patch=True)
            return annotate_generation(result)
        result.update(patch_status='no_valid_candidate',patch='',modified_files=[],requires_manual_patch=True,
                      explanation=candidate.get('reason','two-hole candidate rejected'))
        if getattr(generator,'test_first_scope',False) and candidate.get('validation'):
            result['validation_status']=candidate['validation'].get('status','not_run')
    except errors as exc:
        candidate['reason'] = str(exc)
        if result['scope_history']:
            result['scope_history'][-1].update(status='rejected',reason=str(exc))
        result.update(patch_status='no_valid_candidate',explanation=str(exc))
    return annotate_generation(result)
