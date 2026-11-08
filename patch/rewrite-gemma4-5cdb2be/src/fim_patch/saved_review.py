"""Recheck saved generated patches in memory; never invent an inference result."""
import ast
import re
import subprocess
from pathlib import Path
from .generator import (git,sha,check_executable_change,check_new_undefined_names,
                        _target_function_node,check_locked_function_contract)
from .helper_contracts import check_calls
from .none_contract import assess_none_contract
from .scope_planning import plan_scope
from .scope_execution import check_additive_signature


def apply_single_diff(before,patch,path):
    lines=patch.splitlines(keepends=True);source=before.splitlines(keepends=True)
    # Generated patches use one validated a/b file header, never an arbitrary path.
    if len(lines)<3 or lines[:2]!=['--- a/'+path+'\n','+++ b/'+path+'\n']:
        raise ValueError('unsupported_or_mismatched_saved_diff_header')
    output=[];cursor=0;index=2
    while index<len(lines):
        match=re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*\n',lines[index])
        if not match:raise ValueError('invalid_saved_diff_hunk')
        old_start,old_count,new_start,new_count=[int(x) if x is not None else 1 for x in match.groups()]
        start=old_start-1 if old_count else old_start
        if start<cursor or start>len(source):raise ValueError('saved_diff_coordinate_mismatch')
        output.extend(source[cursor:start]);cursor=start;index+=1;removed=added=0
        if len(output)!=(new_start-1 if new_count else new_start):raise ValueError('saved_diff_new_coordinate_mismatch')
        while index<len(lines) and not lines[index].startswith('@@ '):
            line=lines[index];kind=line[:1];value=line[1:]
            if kind in (' ','-'):
                if cursor>=len(source) or source[cursor]!=value:raise ValueError('saved_diff_context_mismatch')
                cursor+=1;removed+=1
            if kind in (' ','+'):output.append(value);added+=1
            if kind not in (' ','-','+'):raise ValueError('unsupported_saved_diff_line')
            index+=1
        if removed!=old_count or added!=new_count:raise ValueError('saved_diff_hunk_count_mismatch')
    output.extend(source[cursor:]);return ''.join(output)


def review_saved(ticket,location,repo,saved,policy):
    if saved.get('generation_mode')!='fim' or saved.get('base_commit')!=ticket['base_commit']:
        raise ValueError('saved_result_provenance_or_base_mismatch')
    patches=[];seen=set()
    candidates=saved.get('generated_candidates') or [c for h in (saved.get('repair_history') or [saved])
                    for c in h.get('candidates',[]) if c.get('apply_check_status')=='passed']
    for candidate in candidates:
        patch=candidate.get('patch','')
        if not patch or sha(patch.encode()) in seen:continue
        seen.add(sha(patch.encode()))
        item=dict(patch_sha256=sha(patch.encode()),status='rejected',mode='stored_patch_reevaluation',official_resolved=None)
        try:
            path=saved['bug_location']['file'];symbol=saved['span']['symbol']
            from .generator import source_for,function_span,build_patch
            raw,_,_=source_for(ticket,location,repo,dict(file_path=path,symbol_qualified_name=symbol))
            if sha(raw)!=saved['source_file_sha256'][path]:raise ValueError('saved_source_fingerprint_mismatch')
            before=raw.decode('utf-8-sig');after=apply_single_diff(before,patch,path)
            old_tree,new_tree=ast.parse(before),ast.parse(after)
            node=_target_function_node(before,symbol)
            span=function_span(raw,dict(symbol_qualified_name=symbol,start_line=min([node.lineno]+[d.lineno for d in node.decorator_list]),end_line=node.end_lineno))
            proposal=plan_scope(raw,dict(span,lock_signature=True),ticket,path)
            full=function_span(raw,dict(symbol_qualified_name=symbol,start_line=min([node.lineno]+[d.lineno for d in node.decorator_list]),end_line=node.end_lineno),include_signature=True)
            # Allow trailing whitespace inside the target; preserve exact outside bytes.
            if not after.startswith(full['prefix']) or not after.endswith(full['suffix']):
                raise ValueError('saved_patch_outside_planned_target')
            stop=len(after)-len(full['suffix']) if full['suffix'] else len(after)
            replacement=after[len(full['prefix']):stop]
            _,bounded=build_patch(raw,full,replacement,path)
            if bounded.decode('utf-8-sig')!=after:raise ValueError('saved_patch_outside_planned_target')
            check_executable_change(before,after);check_new_undefined_names(before,after,symbol)
            try:check_locked_function_contract(old_tree,new_tree,symbol)
            except ValueError:
                if proposal['action']!='propose_signature_and_behavior_fim':raise
                check_additive_signature(before,after,symbol,proposal['proposed_parameters'])
            item['helper_check']=check_calls(before,after,symbol)
            item['none_check']=assess_none_contract(before,after,symbol)
            from .operation_contract import diagnose
            item['operation_diagnostic']=diagnose(before,after,symbol,ticket,adapters=policy.domain_adapters)
            git(repo,'apply','--check','--whitespace=nowarn',data=patch.encode())
            if item['none_check']['status']=='failed':raise ValueError('source_none_regression')
            item.update(status='replay_static_passed',replay_apply_check_status='passed',semantic_repair_verified=False)
        except (ValueError,SyntaxError,OSError,subprocess.SubprocessError) as exc:item['reason']=str(exc)
        patches.append(item)
    return dict(mode='stored_patch_reevaluation',ticket_id=ticket.get('ticket_id') or ticket['instance_id'],
                model_calls=0,official_tests_run=False,candidates=patches,
                replay_static_candidate_count=sum(p['status']=='replay_static_passed' for p in patches),
                note='Rechecks saved diffs, not raw-response replay or a new-model generation rate; multi-file diffs remain unsupported.')
