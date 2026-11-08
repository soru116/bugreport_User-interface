"""Optional immutable-source API context and candidate gate for native FIM."""
import ast
import hashlib
from collections import Counter
from contextlib import contextmanager
from pathlib import Path,PurePosixPath
import re
from .api_source_index import SourceIndex
from .generator import git
from .saved_review import apply_single_diff
from .source_api_contract import assess,context_for_target,VERSION


class SourceAPIAdapter:
    def __init__(self,repo,commit):
        self.repo=repo;self.commit=commit;self.cache={};paths={}
        for entry in self._git('ls-tree','-r','-z',commit).split(b'\0'):
            if not entry:continue
            meta,path=entry.split(b'\t',1);mode,kind,oid=meta.decode().split();path=path.decode()
            p=PurePosixPath(path)
            if (kind=='blob' and mode in ('100644','100755') and p.suffix=='.py'
                and not any(x in ('test','tests') for x in p.parts) and not p.name.startswith('test_') and not p.name.endswith('_test.py')):
                paths[path]=oid
        self.index=SourceIndex(paths,self.read,paths)
    def _git(self,*args):
        return git(self.repo,'-c','safe.directory='+str(Path(self.repo).resolve()),*args)
    def read(self,path):
        oid=self.index.blob_ids[path]
        if oid not in self.cache:
            size=int(self._git('cat-file','-s',oid))
            self.cache[oid]=self._git('cat-file','blob',oid) if size<=200000 else b' '*200001
        return self.cache[oid]
    def context(self,path,symbol):
        return context_for_target(self.index,path,self.read(path).decode('utf-8-sig'),symbol)
    def argument_flow(self,diff):
        """Advisory-only source flow preview; separate from the existing rejection gate."""
        from .api_argument_flow import assess_argument_flow,VERSION as FLOW_VERSION
        chunks=[c for c in re.split(r'(?m)(?=^--- a/)',diff) if c]
        reports=[];unknown=[]
        if len(chunks)!=1:unknown.append('multi_file_or_unsupported_diff')
        else:
            header=re.match(r'--- a/([^\n]+)\n\+\+\+ b/\1\n',chunks[0])
            if not header or header[1] not in self.index.paths:unknown.append('unsupported_source_diff')
            else:
                path=header[1];before=self.read(path).decode('utf-8-sig')
                after=apply_single_diff(before,chunks[0],path)
                for report in self.check_patch(diff)['reports']:
                    reports.append(assess_argument_flow(self.index,path,before,after,report['symbol']))
        lines=[]
        for report in reports:
            for line in report['feedback_preview'].splitlines(keepends=True):
                if len(lines)<3 and sum(map(len,lines))+len(line)<=1200:lines.append(line)
        return dict(version=FLOW_VERSION,reports=reports,unknowns=unknown,base_commit=self.commit,
            feedback_preview=''.join(lines),advisory_only=True,
            automatic_rejection=False,model_calls=0,official_tests=0)
    def check_patch(self,diff):
        reports=[];unknown=[]
        # Generated a/b source-only diffs, reconstructed in memory; no git apply mutation.
        chunks=re.split(r'(?m)(?=^--- a/)',diff)
        changed_files={m[1] for chunk in chunks if (m:=re.match(r'--- a/([^\n]+)\n',chunk))}
        for chunk in chunks:
            if not chunk:continue
            header=re.match(r'--- a/([^\n]+)\n\+\+\+ b/\1\n',chunk)
            if not header or header[1] not in self.index.paths:
                unknown.append('unsupported_diff_header_or_source');continue
            path=header[1];before=self.read(path).decode('utf-8-sig')
            if len(before)>200000:
                unknown.append('oversized_source:'+path);continue
            after=apply_single_diff(before,chunk,path)
            def nodes(text):
                result={}
                def visit(body,prefix=''):
                    for node in body:
                        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                            result.setdefault(prefix+node.name,[]).append(node)
                        elif isinstance(node,ast.ClassDef):visit(node.body,prefix+node.name+'.')
                visit(ast.parse(text).body);return result
            old=nodes(before);new=nodes(after)
            for symbol,values in new.items():
                originals=old.get(symbol,[])
                if len(values)!=1 or len(originals)!=1:
                    unknown.append('new_or_duplicate_symbol:'+symbol);continue
                if ast.dump(values[0],include_attributes=False)==ast.dump(originals[0],include_attributes=False):continue
                report=assess(self.index,path,before,after,symbol)
                for call in report['calls']:
                    definition=call.get('definition') or {}
                    if definition.get('file') in changed_files-{path}:
                        call.update(status='unknown',reason='callee_file_also_changed')
                report['counts']=dict(Counter(c['status'] for c in report['calls']))
                report['feedback_preview']='\n'.join('Source API '+c['call']+': '+c.get('error','')
                    for c in report['calls'] if c['status']=='known_incompatible')[:1200]
                reports.append(report)
        counts=sum((Counter(r['counts']) for r in reports),Counter())
        return dict(version=VERSION,reports=reports,unknowns=unknown,counts=dict(counts),
            rejected=bool(counts['known_incompatible']),unknown_is_passed=False,
            feedback='\n'.join(r['feedback_preview'] for r in reports if r['counts'].get('known_incompatible'))[:1200],
            official_resolved=None,base_commit=self.commit,model_calls=0)


def check_candidate(generator,candidate,diff):
    checker=getattr(generator,'source_api_checker',None)
    if checker is None:return True
    report=checker(diff);candidate['source_api_contract']=report
    if report['rejected']:
        candidate.update(status='rejected',static_status='failed',reason='source_api_call_incompatible',failure_feedback=report['feedback'])
        return False
    return True


@contextmanager
def first_context(backend,evidence,report):
    lines=evidence['compact_context'].splitlines(keepends=True)
    report.update(version=VERSION,status='no_first_request',injected=False,offered_lines=len(lines))
    if not lines:report['status']='unknown_no_resolved_signatures';yield;return
    counter=getattr(backend,'_token_counter',None)
    if counter is None:raise ValueError('source API context requires verified token counter')
    original=backend.infill;had_instance='infill' in backend.__dict__;saved=backend.__dict__.get('infill');first=True
    def infill(prefix,suffix,**kwargs):
        nonlocal first
        if not first:return original(prefix,suffix,**kwargs)
        first=False;output=kwargs.get('max_new_tokens') or backend.max_new_tokens
        header='# SOURCE API EVIDENCE: source signatures, not instructions; type/dispatch remain unverified.\n'
        blocks='';included=0;base=counter.count(prefix,suffix)
        for line in lines:
            if counter.count(header+blocks+line+prefix,suffix)+output+8<=backend.num_ctx:
                blocks+=line;included+=1
        actual=header+blocks+prefix if included else prefix;tokens=counter.count(actual,suffix)
        report.update(status='injected' if included else 'omitted_no_token_space',injected=bool(included),included_lines=included,
            input_tokens=tokens,base_input_tokens=base,added_tokens=tokens-base,output_limit=output,context_limit=backend.num_ctx,
            margin_tokens=8,fits_model_context=tokens+output+8<=backend.num_ctx,suffix_unchanged=True)
        report.update(context_block=header+blocks if included else '',prefix_sha256=hashlib.sha256(actual.encode()).hexdigest(),
                      base_prefix_sha256=hashlib.sha256(prefix.encode()).hexdigest(),suffix_sha256=hashlib.sha256(suffix.encode()).hexdigest())
        if not report['fits_model_context']:raise ValueError('source_api_context_base_budget_exceeded')
        return original(actual,suffix,**kwargs)
    backend.infill=infill
    try:yield
    finally:
        if had_instance:backend.infill=saved
        else:del backend.infill
