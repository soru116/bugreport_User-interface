"""Static public API source context from immutable base-tree Python files."""
import ast
from functools import lru_cache
import hashlib
from pathlib import PurePosixPath
import re

VERSION='public-api-base-context-v1'


def dotted(node):
    if isinstance(node,ast.Name):return node.id
    if isinstance(node,ast.Attribute):
        parent=dotted(node.value)
        return parent+'.'+node.attr if parent else None
    return None


def public_references(public):
    refs=[];unknown=[]
    clean=public.replace('\r\n','\n').replace('\r','\n')
    for snippet,match in enumerate(re.finditer(r'```(?:python|py)?[ \t]*\n(.*?)```',clean,re.S|re.I),1):
        code=match.group(1)
        try:tree=ast.parse(code)
        except SyntaxError:
            # Console examples: retain input prompts only, not output/traceback as code.
            prompts=[];active=False
            for line in code.splitlines():
                m=re.match(r'^\s*>>> ?(.*)$',line)
                continuation=re.match(r'^\s*\.\.\. ?(.*)$',line)
                if m:prompts.append(m.group(1));active=True
                elif continuation and active:prompts.append(continuation.group(1))
                else:active=False
            try:
                if not prompts:raise SyntaxError()
                tree=ast.parse('\n'.join(prompts))
            except SyntaxError:unknown.append('unparseable_public_example');continue
        aliases={}
        for node in tree.body:
            if isinstance(node,ast.Import):
                for a in node.names:aliases[a.asname or a.name.split('.')[0]]=a.name if a.asname else a.name.split('.')[0]
            elif isinstance(node,ast.ImportFrom) and not node.level:
                for a in node.names:
                    if a.name!='*':aliases[a.asname or a.name]=((node.module or '')+'.'+a.name).strip('.')
        shadowed={n.id for n in ast.walk(tree) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Store)}
        shadowed.update(n.arg for n in ast.walk(tree) if isinstance(n,ast.arg))
        shadowed.update(n.name for n in ast.walk(tree) if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)))
        for node in ast.walk(tree):
            if not isinstance(node,ast.Call):continue
            value=dotted(node.func)
            if not value:unknown.append('chained_or_dynamic_public_call');continue
            root,*tail=value.split('.')
            if root not in aliases or root in shadowed:unknown.append('unresolved_public_receiver_or_shadow');continue
            api='.'.join([aliases[root],*tail])
            item=dict(api=api,snippet=snippet,example_line=node.lineno,keywords=[k.arg for k in node.keywords if k.arg],positional_arguments=len(node.args))
            if not any(x['api']==api and x['keywords']==item['keywords'] for x in refs):refs.append(item)
    return refs,sorted(set(unknown))


class SourceIndex:
    def __init__(self,paths,read,blob_ids=None):
        self.paths=set(paths);self.read=read;self.blob_ids=blob_ids or {};self.modules={};self.parse_errors=set()
        for path in sorted(self.paths):
            if not path.endswith('.py'):continue
            parts=list(PurePosixPath(path).with_suffix('').parts)
            start=0
            for i in range(1,len(parts)):
                if '/'.join(parts[:i])+'/__init__.py' in self.paths:start=i-1;break
            parts=parts[start:]
            if parts[-1]=='__init__':parts.pop()
            module='.'.join(parts)
            self.modules.setdefault(module,[]).append(path)
    @lru_cache(maxsize=2048)
    def module(self,module):
        paths=self.modules.get(module,[])
        if len(paths)!=1:return None
        path=paths[0]
        try:
            raw=self.read(path)
            if len(raw)>200000:raise ValueError('oversized source')
            text=raw.decode('utf-8-sig');tree=ast.parse(text)
        except (ValueError,UnicodeError,SyntaxError):self.parse_errors.add(path);return None
        package=module.split('.') if path.endswith('/__init__.py') else module.split('.')[:-1]
        aliases={};stars=[];definitions={};all_names=None;all_dynamic=False
        for node in tree.body:
            if isinstance(node,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):
                definitions.setdefault(node.name,[]).append(node)
                aliases.pop(node.name,None)
            elif isinstance(node,ast.Import):
                for a in node.names:aliases.setdefault(a.asname or a.name.split('.')[0],[]).append(a.name if a.asname else a.name.split('.')[0])
            elif isinstance(node,ast.ImportFrom):
                prefix=node.module or ''
                if node.level:prefix='.'.join(package[:len(package)-node.level+1]+([prefix] if prefix else []))
                for a in node.names:
                    if a.name=='*':stars.append(prefix)
                    else:aliases.setdefault(a.asname or a.name,[]).append((prefix+'.'+a.name).strip('.'))
            elif isinstance(node,ast.Assign):
                names={n.id for t in node.targets for n in ast.walk(t) if isinstance(n,ast.Name)}
                if '__all__' in names:
                    try:
                        literal=ast.literal_eval(node.value)
                        if not isinstance(literal,(list,tuple)) or not all(isinstance(x,str) for x in literal):raise ValueError()
                        all_names=set(literal)
                    except (ValueError,TypeError):all_dynamic=True
                for n in names-{'__all__'}:
                    aliases.pop(n,None);definitions.pop(n,None)
            elif isinstance(node,ast.AugAssign) and isinstance(node.target,ast.Name) and node.target.id=='__all__':all_dynamic=True
        return dict(path=path,module=module,source=text,tree=tree,aliases=aliases,stars=stars,definitions=definitions,all_names=all_names,all_dynamic=all_dynamic)
    @lru_cache(maxsize=16384)
    def resolve(self,api,seen=()):
        if api in seen or len(seen)>=8:return ()
        parts=api.split('.')
        for i in range(len(parts)-1,0,-1):
            module='.'.join(parts[:i]);tail=parts[i:];info=self.module(module)
            if info is None:continue
            first=tail[0];found=[]
            for node in info['definitions'].get(first,[]):
                nodes=[node];symbol=first
                for child_name in tail[1:]:
                    nodes=[c for parent in nodes for c in getattr(parent,'body',[]) if isinstance(c,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and c.name==child_name]
                    symbol+='.'+child_name
                found.extend((module,symbol,n.lineno) for n in nodes)
            if found:return tuple(sorted(set(found)))
            for replacement in info['aliases'].get(first,[]):
                target='.'.join([replacement,*tail[1:]])
                if target!=api:found.extend(self.resolve(target,seen+(api,)))
            for star in info['stars']:
                provider=self.module(star)
                if provider is None or provider['all_dynamic']:continue
                if provider['all_names'] is not None and first not in provider['all_names']:continue
                if provider['all_names'] is None and first.startswith('_'):continue
                found.extend(self.resolve(star+'.'+'.'.join(tail),seen+(api,)))
            return tuple(sorted(set(found)))
        return ()
    def node(self,ref):
        module,symbol,line=ref;info=self.module(module);nodes=info['tree'].body;selected=None
        for part in symbol.split('.'):
            available=[n for n in nodes if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==part]
            selected=next((n for n in available if n.lineno==line),available[0] if available else None)
            if selected is None:return info,None
            nodes=getattr(selected,'body',[])
        return info,selected
    def describe(self,ref,role):
        info,node=self.node(ref)
        signature=ast.unparse(node.args) if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) else None
        lines=info['source'].splitlines();snippet='\n'.join(lines[node.lineno-1:min(node.end_lineno,node.lineno+11)])
        return dict(api=ref[0]+'.'+ref[1],file=info['path'],symbol=ref[1],start_line=node.lineno,end_line=node.end_lineno,
            kind=type(node).__name__,signature=signature,role_hint=role,source_excerpt=snippet,excerpt_truncated=node.end_lineno>node.lineno+12,
            decorators=[ast.unparse(d) for d in getattr(node,'decorator_list',[])],
            normalized_source_sha256=hashlib.sha256(info['source'].encode()).hexdigest(),git_blob_sha=self.blob_ids.get(info['path']),
            repair_correctness_verified=False)
    def relative_reference(self,module,node):
        value=dotted(node)
        if not value:return ()
        info=self.module(module);root,*tail=value.split('.');refs=[]
        for alias in info['aliases'].get(root,[]):refs.extend(self.resolve('.'.join([alias,*tail])))
        if not refs:refs.extend(self.resolve(module+'.'+value))
        return tuple(sorted(set(refs)))


def api_context(index,reference,max_candidates=10,max_depth=2):
    roots=index.resolve(reference['api']);candidates=[];bases=[];unknown=[];seen=set();queue=[(r,0) for r in roots]
    if len(roots)!=1:unknown.append('unresolved_or_ambiguous_public_api_definition')
    while queue:
        ref,depth=queue.pop(0)
        if ref in seen:continue
        seen.add(ref);info,node=index.node(ref)
        if node is None:continue
        candidates.append(index.describe(ref,'public_api_definition' if depth==0 else 'syntactic_base_definition'))
        if isinstance(node,ast.ClassDef):
            for method in node.body:
                if not isinstance(method,(ast.FunctionDef,ast.AsyncFunctionDef)):continue
                decorators=[dotted(d) for d in method.decorator_list]
                role=('constructor' if method.name in ('__init__','__new__') else
                      'input_keyword_setter' if method.name in reference['keywords'] and any(d and d.endswith('.setter') for d in decorators) else None)
                if role:candidates.append(index.describe((ref[0],ref[1]+'.'+method.name,method.lineno),role))
            for base in node.bases:
                resolved=index.relative_reference(ref[0],base)
                bases.append(dict(child=ref[0]+'.'+ref[1],base_expression=ast.unparse(base),definitions=[index.describe(r,'base_reference') for r in resolved],runtime_mro_verified=False))
                if not resolved:unknown.append('unresolved_external_or_dynamic_base')
                if depth<max_depth:queue.extend((r,depth+1) for r in resolved)
                elif resolved:unknown.append('base_depth_limit')
    candidates.sort(key=lambda c:(0 if c['role_hint']=='input_keyword_setter' else 1 if c['role_hint']=='constructor' else 2,c['file'],c['start_line']))
    candidates=candidates[:max_candidates]
    related_calls=[]
    for candidate in candidates:
        info,node=index.node((next(m for m,paths in index.modules.items() if info_path(candidate,paths)),candidate['symbol'],candidate['start_line']))
        statements=[]
        for statement in ast.walk(node):
            if not isinstance(statement,(ast.Assign,ast.AnnAssign,ast.If,ast.Raise,ast.Return)):continue
            names={n.id for n in ast.walk(statement) if isinstance(n,ast.Name)}
            attrs={n.attr for n in ast.walk(statement) if isinstance(n,ast.Attribute)}
            if not ((names|attrs)&set(reference['keywords'])):continue
            part=statement.test if isinstance(statement,ast.If) else statement
            text=ast.get_source_segment(info['source'],part)
            if text and len(text)<=400:statements.append(dict(line=statement.lineno,kind=type(statement).__name__,source=text))
            if len(statements)>=4:break
        candidate['keyword_related_statements']=statements
        arguments={a.arg for a in (*node.args.posonlyargs,*node.args.args,*node.args.kwonlyargs)} if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) else set()
        assigned={n.id for n in ast.walk(node) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Store)}
        for call in (n for n in ast.walk(node) if isinstance(n,ast.Call)):
            value=dotted(call.func);root=value.split('.')[0] if value else None
            if root in ('self','cls') and root in arguments and root not in assigned and value.count('.')==1:
                parent=candidate['symbol'].rsplit('.',1)[0]
                refs=index.resolve(info['module']+'.'+parent+'.'+value.split('.')[-1])
            elif root in arguments or root in assigned:refs=()
            else:refs=index.relative_reference(info['module'],call.func)
            related_calls.append(dict(caller=candidate['api'],line=call.lineno,call=ast.unparse(call.func),
                definitions=[index.describe(r,'direct_call_definition') for r in refs[:2]],runtime_dispatch_verified=False))
            if len(related_calls)>=12:break
        if len(related_calls)>=12:break
    return dict(public_reference=reference,status='definition_resolved' if len(roots)==1 else 'unknown',
        roots=[index.describe(r,'public_api_definition') for r in roots],candidate_locations=candidates,base_relationships=bases,
        related_calls=related_calls,unknowns=sorted(set(unknown+['static roles do not prove correct repair target','cross-file runtime MRO and dynamic receivers not executed'])),
        original_localization_changed=False)


def info_path(candidate,paths):return candidate['file'] in paths


def extract(index,public):
    refs,unknown=public_references(public)
    supported=[r for r in refs if index.resolve(r['api'])]
    entries=[api_context(index,r) for r in supported[:5]]
    return dict(version=VERSION,status='source_context_available' if any(e['status']=='definition_resolved' for e in entries) else 'unknown',
        public_references=refs,entries=entries,unknowns=unknown,parse_or_size_error_files=sorted(index.parse_errors),
        limits=dict(max_apis=5,max_base_depth=2,max_candidates_per_api=10,max_related_calls_per_api=12),
        model_calls=0,test_calls=0,localization_changed=False,repair_verified=False)
