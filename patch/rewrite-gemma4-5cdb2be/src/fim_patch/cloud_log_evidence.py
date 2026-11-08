"""Separated, bounded official log evidence; never alters grading decisions."""
import hashlib
import io
import json
import re
import zipfile
if __package__:
    from .failure_excerpt import test_failure_excerpt
else:
    from failure_excerpt import test_failure_excerpt

VERSION='separated-official-logs-v1'
MAX_LOG_BYTES=4_000_000
LOG_NAMES=('test_output.txt','run_instance.log')
IDENTITY=('run_id','instance_id','patch_sha256','dataset','split')


def capture_file(path,limit=MAX_LOG_BYTES):
    if limit<64:raise ValueError('log byte limit must be at least 64')
    if not path.is_file() or path.is_symlink():
        return b'',dict(present=False,reason='missing_or_non_regular',original_bytes=None,captured_bytes=0)
    stat=path.stat();size=stat.st_size;digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
        if size<=limit:
            stream.seek(0);raw=stream.read();ranges=[dict(offset=0,length=size)]
        else:
            marker=b'\n[OMITTED MIDDLE LOG BYTES]\n';head=(limit-len(marker))//2;tail=limit-len(marker)-head
            stream.seek(0);first=stream.read(head);stream.seek(size-tail);last=stream.read(tail)
            raw=first+marker+last;ranges=[dict(offset=0,length=head),dict(offset=size-tail,length=tail)]
    after=path.stat()
    if (size,stat.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('log changed during capture')
    return raw,dict(present=True,original_bytes=size,captured_bytes=len(raw),truncated=size>limit,
        capture_mode='head_tail' if size>limit else 'complete',ranges=ranges,
        original_sha256=digest.hexdigest(),captured_sha256=hashlib.sha256(raw).hexdigest())


def collect_logs(root,output,identity):
    logs={};metadata={}
    for name in LOG_NAMES:
        raw,info=capture_file(root/name);metadata[name]=dict(info,source=name)
        if info['present']:
            (output/name).write_bytes(raw);logs[name]=raw.decode('utf-8',errors='replace')
        elif (output/name).exists():(output/name).unlink() # Remove only this known stale output file.
    manifest=dict(version=VERSION,identity=identity,files=metadata)
    (output/'logs_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return logs,manifest


def failure_text(log):
    excerpt=test_failure_excerpt(log)
    detail=bool(re.search(r'(?m)^\s*(?:[\w.]*Error:|[\w.]*Exception:|Failed:|Warning-as-error:|Assertion evidence:)',excerpt))
    summary=bool(re.search(r'(?m)^\s*(?:FAILED|ERROR)\s+',excerpt))
    return excerpt if detail or summary else '',detail


def failed_ids(report,instance):
    record=report.get(instance,{}) if isinstance(report,dict) else {}
    tests=record.get('tests_status') or {} if isinstance(record,dict) else {}
    if not isinstance(tests,dict):return []
    lines=[]
    for category in ('FAIL_TO_PASS','PASS_TO_PASS'):
        value=tests.get(category) or {}
        failures=value.get('failure',[]) if isinstance(value,dict) else []
        for name in failures:
            if isinstance(name,str) and len(name)<=350:lines.append(category+' failed test: '+name)
            if len(lines)>=5:return lines
    return lines


def select_feedback(verdict,logs,report=None,source_verified=False):
    instance=verdict['instance_id'];test=logs.get('test_output.txt','');stage=logs.get('run_instance.log','')
    text,detail=failure_text(test)
    source='artifact_test_output'
    quality='test_failure_detail' if detail else 'test_summary_only'
    if not text:
        text,detail=failure_text(stage)
        source='artifact_run_instance';quality='stage_failure_detail' if text else 'missing_test_details'
    if not text and not source_verified:
        text,detail=failure_text(str(verdict.get('feedback','')))
        source='verdict';quality='legacy_failure_text' if text else 'missing_test_details'
    lines=failed_ids(report,instance)
    if not text:
        text='Actual failed assertion/expected value unavailable; test details remain unknown.'
        source='structured_report' if lines else 'unknown'
        # No bulk runner log tail is a substitute for a failed assertion.
    if lines:text+='\n'+'\n'.join(lines)
    if not source_verified and quality!='missing_test_details':quality='legacy_unverified_source'
    return dict(feedback=text[:2400],feedback_source=source,feedback_quality=quality,
        failure_details_verified=bool(source_verified and source=='artifact_test_output' and detail),
        log_source_separation_verified=source_verified,log_evidence_version=VERSION)


def read_archive_feedback(archive,verdict,allow_diagnostic=False):
    issues=[];logs={};raws={};manifest=None
    def read(name,limit):
        if archive.namelist().count(name)!=1 or archive.getinfo(name).file_size>limit:raise ValueError('missing/duplicate/oversize '+name)
        return archive.read(name)
    if 'logs_manifest.json' in archive.namelist():
        try:
            manifest=json.loads(read('logs_manifest.json',512000))
            if manifest.get('version')!=VERSION or any(manifest['identity'].get(k)!=verdict.get(k) for k in IDENTITY):
                raise ValueError('log manifest identity/version mismatch')
        except (ValueError,KeyError,TypeError,AttributeError) as exc:
            issues.append(str(exc));manifest=None
    separated=manifest is not None
    for name in LOG_NAMES:
        try:
            if separated and not manifest['files'][name]['present']:continue
            if name not in archive.namelist():
                if separated:raise ValueError('declared log missing: '+name)
                continue
            raw=read(name,MAX_LOG_BYTES)
            if separated:
                info=manifest['files'][name]
                if info['source']!=name or info['captured_bytes']!=len(raw) or info['captured_sha256']!=hashlib.sha256(raw).hexdigest():
                    raise ValueError('log integrity mismatch: '+name)
            raws[name]=raw;logs[name]=raw.decode('utf-8',errors='replace')
        except (ValueError,KeyError,TypeError) as exc:issues.append(str(exc))
    if 'logs_manifest.json' in archive.namelist() and not separated:
        logs={};raws={} # Invalid provenance cannot be relabelled as a legacy verified log.
    report=None
    try:report=json.loads(read('report.json',2_000_000))
    except (ValueError,KeyError):pass
    result=select_feedback(verdict,logs,report,source_verified=separated and not issues)
    result['feedback_provenance']=dict(version=VERSION,manifest=manifest,issues=issues)
    if allow_diagnostic and verdict.get('official_resolved') is False and result['feedback_quality'] in ('missing_test_details','test_summary_only'):
        try:
            diagnostic=json.loads(read('diagnostic/diagnostic.json',512000))
            transforms=diagnostic.get('transformations')
            safe_transforms=(isinstance(transforms,list) and bool(transforms) and all(
                isinstance(t,dict) and t.get('status')=='modified' and t.get('test_set_changed') is False
                and t.get('patch_changed') is False and t.get('official_result_overridden') is False
                and isinstance(t.get('changes'),list) and bool(t['changes'])
                and all(isinstance(c,dict) and c.get('option_before')=='--tb=no' and c.get('option_after')=='--tb=short'
                        and type(c.get('line')) is int and c['line']>0 for c in t['changes']) for t in transforms))
            valid=(diagnostic.get('version')=='diagnostic-traceback-v1' and diagnostic.get('status')=='completed'
                and diagnostic.get('harness_version')=='5.0.2' and safe_transforms
                and diagnostic.get('diagnostic_run_id')==verdict['run_id']+'-diagnostic'
                and diagnostic.get('official_resolved') is None and diagnostic.get('official_result_overridden') is False
                and diagnostic.get('test_set_changed') is False and diagnostic.get('primary_official_resolved') is False
                and diagnostic.get('primary_verdict_sha256')==hashlib.sha256(read('verdict.json',512000)).hexdigest()
                and all(diagnostic['identity'].get(k)==verdict.get(k) for k in IDENTITY))
            if not valid:raise ValueError('diagnostic provenance mismatch')
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,'w') as child:
                for name in (*LOG_NAMES,'logs_manifest.json'):
                    if 'diagnostic/'+name in archive.namelist():child.writestr(name,read('diagnostic/'+name,MAX_LOG_BYTES if name in LOG_NAMES else 512000))
                if report is not None:child.writestr('report.json',json.dumps(report))
            with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as child:
                detail,diagnostic_logs,_=read_archive_feedback(child,verdict)
            if detail['failure_details_verified']:
                result.update(feedback='Diagnostic replay evidence (not an official verdict):\n'+detail['feedback'],
                    feedback_source='diagnostic_test_output',feedback_quality='diagnostic_test_failure_detail',
                    diagnostic_failure_details_verified=True)
                result['feedback_provenance']['diagnostic']=dict(metadata=diagnostic,log_provenance=detail['feedback_provenance'])
                raws.update({'diagnostic/'+name:raw for name,raw in diagnostic_logs.items()})
            else:result['diagnostic_failure_details_verified']=False
        except (ValueError,KeyError,TypeError,AttributeError) as exc:
            result['feedback_provenance']['diagnostic_issue']=str(exc)[:300]
    return result,raws,report
