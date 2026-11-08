"""Offline advisory argument-flow evidence; never execute source or reject patches."""
import ast
from .generator import _target_function_node, _function_symbol_table
from .helper_contracts import direct_calls
from .source_api_contract import resolve_call, safe_line, global_writes

VERSION='source-api-argument-flow-v1'


def scoped_nodes(node):
    for child in ast.iter_child_nodes(node):
        if isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef,ast.Lambda)):continue
        yield child
        yield from scoped_nodes(child)


def operations(callee):
    params={a.arg for a in (*callee.args.posonlyargs,*callee.args.args,*callee.args.kwonlyargs)}
    facts=[]
    def writes(s):
        return {n.id for n in [s,*scoped_nodes(s)] if isinstance(n,ast.Name) and isinstance(n.ctx,(ast.Store,ast.Del))}
    def visit(body,live):
        live=set(live)
        for s in body:
            if isinstance(s,ast.If):
                live=visit(s.body,live)&visit(s.orelse,live);continue
            if isinstance(s,(ast.For,ast.AsyncFor,ast.While,ast.With,ast.AsyncWith,ast.Try)):
                live-=writes(s);continue # Callee control-flow operations remain unsupported.
            if isinstance(s,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):continue
            for n in scoped_nodes(s):
                if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and isinstance(n.func.value,ast.Name):
                    if n.func.value.id in live and n.func.attr in ('split','rsplit','startswith','endswith'):
                        facts.append(dict(parameter=n.func.value.id,operation=n.func.attr,line=n.lineno))
                if isinstance(n,ast.BinOp) and isinstance(n.op,ast.Add):
                    for value,other in ((n.left,n.right),(n.right,n.left)):
                        if isinstance(value,ast.Name) and value.id in live and isinstance(other,ast.Constant) and isinstance(other.value,str):
                            facts.append(dict(parameter=value.id,operation='add_string_literal',line=n.lineno))
            live-=writes(s) # Later operations cannot be attributed to the original argument after conversion/rebinding.
        return live
    visit(callee.body,params)
    return facts[:12]


def assess_argument_flow(index,file,before,after,symbol):
    modules=[m for m,paths in index.modules.items() if paths==[file]]
    if len(modules)!=1:raise ValueError('ambiguous source module')
    module=modules[0];info=index.module(module)
    if not info or info['source']!=before:raise ValueError('base source mismatch')
    new=_target_function_node(after,symbol);old=_target_function_node(before,symbol)
    table=_function_symbol_table(after,symbol)
    old_calls={ast.dump(c,include_attributes=False) for c in direct_calls(old)}
    def global_shape(text):
        return [ast.dump(n,include_attributes=False) for n in ast.parse(text).body if not isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
    globals_changed=global_shape(before)!=global_shape(after)
    # Dynamic namespace mutation prevents inferring even builtin producer provenance.
    dynamic=any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in ('exec','eval','globals','locals','vars') for n in ast.walk(ast.parse(after)))
    def builtin_import():
        try:s=table.lookup('__import__')
        except KeyError:return False
        return (not dynamic and not globals_changed and s.is_global() and not s.is_assigned() and not s.is_imported()
                and '__import__' not in global_writes(info['tree']) and '__import__' not in info['aliases'] and '__import__' not in info['definitions'])
    def value(node,env):
        if isinstance(node,ast.Name):return env.get(node.id,[dict(kind='unknown',origin='unresolved_name',line=node.lineno)])
        if isinstance(node,ast.Constant):return [dict(kind=type(node.value).__name__,origin='literal',line=node.lineno)]
        if isinstance(node,(ast.List,ast.Tuple,ast.Dict,ast.Set)):return [dict(kind=type(node).__name__.lower(),origin='literal_container',line=node.lineno)]
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='__import__' and builtin_import():
            return [dict(kind='module_return',origin='unshadowed_builtin_import',line=node.lineno,
                         assumption='normal Python builtin import; runtime hooks not verified')]
        return [dict(kind='unknown',origin='dynamic_expression',line=getattr(node,'lineno',None))]
    records=[]
    def inspect(call,env):
        if ast.dump(call,include_attributes=False) in old_calls:return
        record=dict(call=safe_line(ast.unparse(call.func)),line=call.lineno,status='unknown',arguments=[],runtime_type_verified=False)
        resolved,reason=(None,'module_bindings_changed') if globals_changed else resolve_call(index,module,call,table,file,after)
        if not resolved:record['reason']=reason;records.append(record);return
        ref,callee_info,callee,mode=resolved
        if mode!='module_function':record['reason']='method_binding_not_supported';records.append(record);return
        description=index.describe(ref,'argument_flow')
        record['definition']={k:description[k] for k in ('api','file','symbol','git_blob_sha','normalized_source_sha256')}
        params=[a.arg for a in (*callee.args.posonlyargs,*callee.args.args)]
        facts=operations(callee);record['parameter_operations']=facts
        star=False;bindings=[]
        for i,arg in enumerate(call.args):
            if isinstance(arg,ast.Starred):star=True;continue
            if star:continue
            if i<len(params):bindings.append((params[i],arg,'leading_positional'))
        for kw in call.keywords:
            if kw.arg in {a.arg for a in (*callee.args.posonlyargs,*callee.args.args,*callee.args.kwonlyargs)}:
                bindings.append((kw.arg,kw.value,'explicit_keyword'))
        for parameter,arg,binding in bindings:
            origins=value(arg,env);uses=[f for f in facts if f['parameter']==parameter]
            risk=bool(uses and any(o['kind'] in ('module_return','int','float','bool','NoneType','list','tuple','dict','set') for o in origins))
            record['arguments'].append(dict(parameter=parameter,expression=safe_line(ast.unparse(arg))[:160],binding=binding,
                origins=origins,operations=uses,status='potential_operation_mismatch' if risk else 'unknown',
                all_paths_verified=False,type_requirement_verified=False))
        record.update(status='potential_operation_mismatch' if any(a['status']=='potential_operation_mismatch' for a in record['arguments']) else 'unknown',
            reason='source_operation_and_value_origin_evidence' if record['arguments'] else 'no_supported_argument_binding',
            unpacking_present=star or any(k.arg is None for k in call.keywords),binding_verified=False)
        records.append(record)
    def expr(node,env):
        if node is None:return
        for call in ([node] if isinstance(node,ast.Call) else [])+[n for n in scoped_nodes(node) if isinstance(n,ast.Call)]:inspect(call,env)
    def assigned(statements):
        return {n.id for s in statements for n in [s,*scoped_nodes(s)] if isinstance(n,ast.Name) and isinstance(n.ctx,(ast.Store,ast.Del))}
    def merge(left,right):
        result={}
        for key in set(left)|set(right):
            origins=left.get(key,[dict(kind='unknown',origin='branch_unbound')])+right.get(key,[dict(kind='unknown',origin='branch_unbound')])
            result[key]=[o for i,o in enumerate(origins) if o not in origins[:i]][:6]
        return result
    def block(statements,env):
        env=dict(env)
        for s in statements:
            if isinstance(s,ast.If):
                expr(s.test,env);env=merge(block(s.body,env),block(s.orelse,env));continue
            if isinstance(s,ast.Try):
                body=block(s.body,env);combined=body
                for h in s.handlers:combined=merge(combined,block(h.body,env))
                env=block(s.finalbody,merge(combined,block(s.orelse,body)));continue
            if isinstance(s,(ast.For,ast.AsyncFor,ast.While,ast.With,ast.AsyncWith)):
                expr(s,env)
                for key in assigned([s]):env[key]=[dict(kind='unknown',origin='unsupported_control_flow',line=s.lineno)]
                continue
            if isinstance(s,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                env[s.name]=[dict(kind='unknown',origin='nested_binding',line=s.lineno)];continue
            expr(s,env)
            if isinstance(s,(ast.Assign,ast.AnnAssign)):
                targets=s.targets if isinstance(s,ast.Assign) else [s.target]
                origins=value(s.value,env) if s.value is not None else [dict(kind='unknown',origin='annotation_only')]
                for target in targets:
                    if isinstance(target,ast.Name):env[target.id]=origins
                    else:
                        for key in assigned([target]):env[key]=[dict(kind='unknown',origin='unpacking_assignment')]
            else:
                for key in assigned([s]):env[key]=[dict(kind='unknown',origin='unsupported_assignment',line=s.lineno)]
        return env
    initial={a.arg:[dict(kind='unknown',origin='function_parameter')] for a in (*new.args.posonlyargs,*new.args.args,*new.args.kwonlyargs)}
    block(new.body,initial)
    feedback=[]
    for r in records:
        for a in r['arguments']:
            if a['status']!='potential_operation_mismatch':continue
            kinds=', '.join(sorted({o['kind'] for o in a['origins']}))
            uses=', '.join(sorted({o['operation'] for o in a['operations']}))
            feedback.append('# API value-flow evidence: '+safe_line(r['call']+' parameter '+a['parameter']+' receives '+a['expression']+' ('+kinds+'); base source uses '+uses+'. Potential mismatch; runtime type and all paths unknown.')[:390]+'\n')
    return dict(version=VERSION,file=file,symbol=symbol,calls=records,potential_mismatches=sum(r['status']=='potential_operation_mismatch' for r in records),
        feedback_preview=''.join(feedback[:3]),advisory_only=True,automatic_rejection=False,prompt_integration_enabled=False,
        model_calls=0,official_tests=0,source_executed=False,patch_modified=False,
        scope='bounded local value origins and immutable callee operations; no runtime type proof, inferred repair or complete control-flow analysis')
