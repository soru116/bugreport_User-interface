"""Descriptive base-source property dependencies; offline, no runtime type proof."""
import ast
import textwrap
from .generator import _target_function_node
from .source_api_contract import global_writes,safe_line
from .api_source_index import dotted
from .source_summary import summarize

VERSION='source-property-dependencies-v1'


def receiver_reads(node):
    args=[*node.args.posonlyargs,*node.args.args]
    if not args:return [],['receiver_parameter_unavailable']
    receiver=args[0].arg;reads=[];unknown=[]
    class Visit(ast.NodeVisitor):
        def visit_FunctionDef(self,n):pass
        visit_AsyncFunctionDef=visit_FunctionDef
        def visit_ClassDef(self,n):pass
        def visit_Lambda(self,n):pass
        def visit_Name(self,n):
            if n.id==receiver and isinstance(n.ctx,(ast.Store,ast.Del)):unknown.append('receiver_reassigned')
        def visit_Attribute(self,n):
            if isinstance(n.ctx,ast.Load):
                if isinstance(n.value,ast.Name) and n.value.id==receiver:
                    reads.append(dict(attribute=n.attr,line=n.lineno,expression=receiver+'.'+n.attr))
                elif not (dotted(n) or '').startswith(receiver+'.'):
                    unknown.append('dynamic_or_other_receiver:'+safe_line(ast.unparse(n))[:100])
            self.generic_visit(n)
    visitor=Visit()
    for s in node.body:visitor.visit(s)
    if 'receiver_reassigned' in unknown:return [],unknown
    return [r for i,r in enumerate(reads) if r['attribute'] not in [x['attribute'] for x in reads[:i]]],unknown


def getter(index,ref,name,seen=()):
    if ref in seen or len(seen)>2:return None,'base_cycle_or_depth_limit'
    info,owner=index.node(ref)
    if not isinstance(owner,ast.ClassDef):return None,'class_owner_unavailable'
    if owner.decorator_list or owner.keywords:return None,'dynamic_class_or_metaclass'
    if any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in ('__getattribute__','__getattr__') for n in owner.body):
        return None,'dynamic_attribute_hooks'
    if 'property' in info['aliases'] or 'property' in info['definitions'] or 'property' in global_writes(info['tree']):
        return None,'property_decorator_shadowed'
    if info['stars']:return None,'star_import_property_binding_unknown'
    members=[n for n in owner.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name]
    if any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=='property' for n in owner.body):
        return None,'class_property_decorator_shadowed'
    if name in global_writes(ast.Module(body=owner.body,type_ignores=[])):return None,'class_member_rebound'
    candidates=[n for n in members if len(n.decorator_list)==1 and dotted(n.decorator_list[0])=='property' and isinstance(n,ast.FunctionDef)]
    other=[n for n in members if n not in candidates]
    if len(candidates)==1 and all(len(n.decorator_list)==1 and dotted(n.decorator_list[0]) in (name+'.setter',name+'.deleter') for n in other):
        if any(isinstance(n,(ast.Yield,ast.YieldFrom)) for n in ast.walk(candidates[0])):return None,'generator_property_unknown'
        return (ref,info,candidates[0]),None
    if members:return None,'not_unique_builtin_property_getter'
    if not owner.bases:return None,'member_not_a_source_property'
    if len(owner.bases)!=1:return None,'multiple_inheritance_unknown'
    refs=index.relative_reference(ref[0],owner.bases[0])
    if len(refs)!=1:return None,'external_or_ambiguous_base'
    return getter(index,refs[0],name,seen+(ref,))


def extract(index,file,source,symbol,max_properties=6,max_depth=1,max_chars=2000):
    if any(type(v) is not int for v in (max_properties,max_depth,max_chars)) or not 1<=max_properties<=12 or not 0<=max_depth<=2 or max_chars<500:
        raise ValueError('invalid property context limits')
    modules=[m for m,paths in index.modules.items() if paths==[file]]
    if len(modules)!=1:raise ValueError('ambiguous source module')
    module=modules[0];info=index.module(module)
    if not info or info['source']!=source:raise ValueError('base source mismatch')
    target=_target_function_node(source,symbol)
    unknown=[];entries=[];queue=[];seen=set()
    if '.' not in symbol:unknown.append('target_not_class_method')
    elif target.decorator_list:unknown.append('target_method_decorator_unknown')
    else:
        owner_symbol=symbol.rsplit('.',1)[0];refs=index.resolve(module+'.'+owner_symbol)
        if len(refs)!=1:unknown.append('ambiguous_class_owner')
        else:
            reads,issues=receiver_reads(target);unknown.extend(issues)
            queue.extend((refs[0],r,0,symbol) for r in reads)
    while queue and len(entries)<max_properties:
        owner,read,depth,caller=queue.pop(0);identity=(owner,read['attribute'])
        if identity in seen:continue
        seen.add(identity)
        resolved,reason=getter(index,owner,read['attribute'])
        if not resolved:unknown.append(read['expression']+':'+reason);continue
        declaring,callee_info,node=resolved
        # Dedent only the selected getter, preserving original line numbers.
        # This avoids merging setters or overloaded duplicate names into the summary.
        snippet='\n'*(node.lineno-1)+textwrap.dedent('\n'.join(callee_info['source'].splitlines()[node.lineno-1:node.end_lineno]))
        facts=summarize(snippet.encode(),node.name,1000)['facts']
        owner_api=declaring[0]+'.'+declaring[1]
        entry=dict(api=owner_api+'.'+node.name,file=callee_info['path'],line_start=node.lineno,line_end=node.end_lineno,
            git_blob_sha=index.blob_ids.get(callee_info['path']),source_sha256=__import__('hashlib').sha256(callee_info['source'].encode()).hexdigest(),
            requested_owner=owner[0]+'.'+owner[1],declaring_owner=owner_api,reference=dict(read,caller=caller),depth=depth,
            facts=facts,receiver_runtime_type_verified=False,property_runtime_dispatch_verified=False,return_type_verified=False)
        entries.append(entry)
        if depth<max_depth:
            reads,issues=receiver_reads(node);unknown.extend(issues)
            queue.extend((owner,r,depth+1,entry['api']) for r in reads)
    header=('# DEPENDENT PROPERTY SOURCE FACTS; not repair instructions or a preservation contract.\n'
            '# Requirements may supersede original behavior; dynamic receiver and return types remain unknown.\n')
    rendered=header;kept=0;omitted=0
    for entry in entries:
        heading='# Observed '+safe_line(entry['reference']['expression'])+' -> getter '+safe_line(entry['api'])+'; '+safe_line(entry['file'])+':'+str(entry['line_start'])+'\n'
        facts=sorted(entry['facts'],key=lambda f:({'return':0,'branch':1,'binding':2}.get(f['kind'],3),f['line']))
        lines=[heading]+['# L'+str(f['line'])+' '+f['kind']+': '+safe_line(f['detail'])[:180]+'\n' for f in facts[:3]]
        block=''.join(lines)
        if len(rendered)+len(block)<=max_chars:rendered+=block;kept+=1
        else:omitted+=1
    return dict(version=VERSION,file=file,symbol=symbol,status='property_source_available' if entries else 'unknown',entries=entries,
        unknowns=sorted(set(unknown))[:12],rendered=rendered if kept else '',rendered_chars=len(rendered) if kept else 0,
        omitted_rendered_properties=omitted,queued_dependencies_omitted=len(queue),limits=dict(max_properties=max_properties,max_depth=max_depth,max_chars=max_chars),
        source_executed=False,model_calls=0,test_calls=0,prompt_integration_enabled=False,patch_modified=False,
        repair_correctness_verified=False,scope='static owner/getter AST observations; runtime receiver, MRO, descriptor effects and return types not verified')
