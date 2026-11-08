"""Opt-in, static source API evidence. Never import or execute repository code."""
import ast
from collections import Counter
from fim_patch.generator import _target_function_node, _function_symbol_table
from fim_patch.helper_contracts import direct_calls, signature
from .api_source_index import dotted

VERSION='source-api-contract-v1'


def global_writes(tree):
    names=set()
    class Bindings(ast.NodeVisitor):
        def visit_Name(self,n):
            if isinstance(n.ctx,(ast.Store,ast.Del)):names.add(n.id)
        def visit_FunctionDef(self,n):pass
        visit_AsyncFunctionDef=visit_FunctionDef
        def visit_ClassDef(self,n):pass
        def visit_Lambda(self,n):pass
    Bindings().visit(tree)
    return names


def resolve_call(index,module,call,table,patched_file,patched_text):
    value=dotted(call.func)
    if not value:return None,'dynamic_call_expression'
    root=value.split('.')[0]
    try:symbol=table.lookup(root)
    except KeyError:return None,'unresolved_scope'
    if not symbol.is_global() or symbol.is_assigned() or symbol.is_imported():
        return None,'local_or_dynamic_receiver'
    module_info=index.module(module)
    if root in global_writes(module_info['tree']):return None,'global_rebinding'
    refs=index.relative_reference(module,call.func)
    if len(refs)!=1:return None,'unresolved_or_ambiguous_api'
    ref=refs[0];info,node=index.node(ref)
    if not isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
        return None,'constructor_or_non_function_not_supported'
    if '.' in ref[1]:
        owner_ref=(ref[0],ref[1].rsplit('.',1)[0],0)
        _,owner=index.node(owner_ref)
        if not isinstance(owner,ast.ClassDef) or owner.decorator_list or owner.keywords:
            return None,'dynamic_class_descriptor'
        if node.name in global_writes(ast.Module(body=owner.body,type_ignores=[])):
            return None,'class_member_rebinding'
        # Only explicit class attribute access; no guessed self.unit or inherited receiver.
        if any(dotted(d) not in ('staticmethod','classmethod') for d in node.decorator_list):
            return None,'unknown_method_decorator'
        if any(dotted(d) in callee_info_names(index,ref[0]) for d in node.decorator_list):
            return None,'decorator_binding_shadowed'
        mode='class_bound' if any(dotted(d)=='classmethod' for d in node.decorator_list) else 'explicit_class_or_static_method'
    else:
        if node.decorator_list:return None,'decorated_function_unknown'
        if node.name in global_writes(info['tree']):return None,'callee_global_rebinding'
        mode='module_function'
    if info['path']==patched_file:
        try:patched_node=_target_function_node(patched_text,ref[1])
        except ValueError:return None,'callee_changed_or_missing'
        if ast.dump(node,include_attributes=False)!=ast.dump(patched_node,include_attributes=False):
            return None,'callee_also_changed'
    return (ref,info,node,mode),None


def callee_info_names(index,module):
    info=index.module(module)
    return set(info['aliases'])|set(info['definitions'])|global_writes(info['tree'])


def safe_line(text):
    text=text.replace('\r',' ').replace('\n',' ')
    for marker in ('<PRE>','<SUF>','<MID>','<EOT>'):
        text=text.replace(marker,'[FIM_'+marker[1:-1]+']')
    return text


def parameter_operations(node):
    """Observations, not inferred runtime types. They do not trigger rejection."""
    parameters={a.arg for a in (*node.args.posonlyargs,*node.args.args,*node.args.kwonlyargs)}
    facts=[]
    for call in direct_calls(node):
        if isinstance(call.func,ast.Attribute) and isinstance(call.func.value,ast.Name) and call.func.value.id in parameters:
            if call.func.attr in ('split','rsplit','startswith','endswith'):
                facts.append(dict(parameter=call.func.value.id,operation=call.func.attr,line=call.lineno,
                                  inferred_type_verified=False))
    return facts[:3]


def assess(index,file,before,after,symbol,max_context_chars=2000,include_unchanged=False):
    modules=[m for m,paths in index.modules.items() if paths==[file]]
    if len(modules)!=1:raise ValueError('ambiguous source module')
    module=modules[0];info=index.module(module)
    if info is None or info['source']!=before:raise ValueError('base source mismatch')
    old=_target_function_node(before,symbol);new=_target_function_node(after,symbol)
    table=_function_symbol_table(after,symbol)
    old_calls={ast.dump(c,include_attributes=False) for c in direct_calls(old)}
    # New global imports/definitions require a new index, never guess from old aliases.
    def globals_shape(text):
        tree=ast.parse(text)
        return [ast.dump(n,include_attributes=False) for n in tree.body
                if not isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
    globals_changed=globals_shape(before)!=globals_shape(after)
    records=[];context_lines=[];seen_context=set()
    for call in direct_calls(new):
        changed=ast.dump(call,include_attributes=False) not in old_calls
        if not changed and not include_unchanged:continue
        record=dict(call=safe_line(ast.unparse(call.func)),line=call.lineno,status='unknown',reason=None,
                    changed_call=changed,value_type_verified=False,return_type_verified=False,runtime_dispatch_verified=False)
        resolved,reason=(None,'module_bindings_changed') if globals_changed else resolve_call(index,module,call,table,file,after)
        if not resolved:
            record['reason']=reason;records.append(record);continue
        ref,callee_info,callee,mode=resolved
        description=index.describe(ref,'source_api_contract')
        record['definition']={k:description[k] for k in ('api','file','symbol','start_line','signature','git_blob_sha','normalized_source_sha256')}
        record['binding_mode']=mode
        record['parameter_operations']=parameter_operations(callee)
        api=description['api']
        line='# Source API: '+safe_line(api+'('+ast.unparse(callee.args)+')')+'; '+safe_line(callee_info['path'])+':'+str(callee.lineno)+'\n'
        if api not in seen_context and len(line)<=400 and len(context_lines)<8 and sum(map(len,context_lines))+len(line)<=max_context_chars:
            context_lines.append(line);seen_context.add(api)
        if any(isinstance(a,ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
            record['reason']='argument_unpacking_unknown'
        else:
            keywords=[k.arg for k in call.keywords]
            try:
                if len(set(keywords))!=len(keywords):raise TypeError('duplicate keyword')
                count=len(call.args)+(1 if mode=='class_bound' else 0)
                signature(callee).bind(*([None]*count),**{k:None for k in keywords})
                record.update(status='binding_compatible',reason='parameter_binding_only')
            except (TypeError,ValueError) as exc:
                record.update(status='known_incompatible',reason='source_parameter_binding',error=str(exc)[:300])
        records.append(record)
    feedback=[]
    for r in records:
        if r['status']=='known_incompatible':
            feedback.append('Source API call '+r['call']+' is incompatible: '+safe_line(r['error']))
        elif r['status']=='unknown':feedback.append('Source API call '+r['call']+' remains unknown: '+r['reason'])
        if len(feedback)>=4:break
    return dict(version=VERSION,file=file,symbol=symbol,calls=records,counts=dict(Counter(r['status'] for r in records)),
        compact_context=''.join(context_lines),feedback_preview='\n'.join(feedback),
        prompt_integration_enabled=False,model_calls=0,official_tests=0,patch_modified=False,
        scope='Static parameter binding only; unknown is not compatible; no runtime/type/return correctness proof')


def context_for_target(index,file,source,symbol,max_chars=2000):
    """Public base-source signatures for an optional first-round context preview.

    Caller must perform real tokenizer budgeting before inserting into FIM.
    No prefix/suffix rewriting or inference takes place here.
    """
    report=assess(index,file,source,source,symbol,max_context_chars=max_chars,include_unchanged=True)
    return dict(version=VERSION,compact_context=report['compact_context'],calls=report['calls'],
        counts=report['counts'],prompt_integration_enabled=False,token_budget_verified=False,
        max_chars=max_chars,model_calls=0)
