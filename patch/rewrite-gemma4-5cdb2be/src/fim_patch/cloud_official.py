"""Opt-in GitHub-hosted official validator; no local SWE-bench/Docker/LLM calls."""
import base64
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from .iterative import test_failure_excerpt, TEST_FAILURE_EXCERPT_VERSION

WORKFLOW = 'swebench-official-evaluation.yml'
MAX_PATCH_BYTES = 35000
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header('Authorization')
        return redirected

class GitHubAPI:
    def __init__(self, repository, token=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('invalid GitHub repository; expected owner/repo')
        self.repository = repository
        self.token = token or os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
        if not self.token:
            raise ValueError('github_token_missing: set GH_TOKEN or GITHUB_TOKEN (never put it in run_config)')
        self.opener = urllib.request.build_opener(SafeRedirect())

    def request(self, path, payload=None, binary=False):
        request = urllib.request.Request('https://api.github.com/repos/' + self.repository + ('/' + path if path else ''),
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization':'Bearer '+self.token, 'Accept':'application/vnd.github+json',
                     'X-GitHub-Api-Version':'2022-11-28', 'User-Agent':'fim-cloud-validator',
                     'Content-Type':'application/json'})
        try:
            with self.opener.open(request, timeout=45) as response:
                raw = response.read(MAX_ARTIFACT_BYTES + 1)
                if len(raw) > MAX_ARTIFACT_BYTES:
                    raise ValueError('github_response_too_large')
                return raw if binary else (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            # Avoid echoing authorization or sensitive request payloads.
            raise ValueError('github_api_http_'+str(exc.code)) from None
        except urllib.error.URLError:
            raise ValueError('github_api_network_error') from None

    def preflight(self, ref,diagnostic_traceback=False):
        from urllib.parse import quote
        self.request('actions/workflows/'+WORKFLOW)
        content = self.request('contents/.github/workflows/'+WORKFLOW+'?ref='+quote(ref, safe=''))
        workflow = base64.b64decode(content.get('content','')).decode('utf-8-sig')
        if not all(marker in workflow for marker in ('patch_b64:', 'patch_sha256:', 'cloud_official_bridge.py')):
            raise ValueError('cloud_workflow_not_deployed: selected ref still has old workflow')
        self.request('contents/scripts/cloud_official_bridge.py?ref='+quote(ref,safe=''))
        if diagnostic_traceback:
            if 'diagnostic_traceback:' not in workflow:raise ValueError('diagnostic_workflow_not_deployed')
            self.request('contents/scripts/cloud_traceback_diagnostic.py?ref='+quote(ref,safe=''))

class CloudOfficialRepairValidator:
    def __init__(self, instance_id, work_dir, repository, ref, dataset='SWE-bench/SWE-bench',
                 split='test', timeout=1800, wait_timeout=7500, api=None, sleep=time.sleep,
                 clock=time.monotonic, startup_feedback=False,diagnostic_traceback=False):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', instance_id):
            raise ValueError('invalid official instance ID')
        if not ref or not 1 <= timeout <= 3600:
            raise ValueError('invalid cloud ref or test timeout')
        self.instance_id, self.work_dir = instance_id, Path(work_dir)
        self.repository, self.ref, self.dataset, self.split = repository, ref, dataset, split
        self.timeout, self.wait_timeout = timeout, wait_timeout
        self.api = api or GitHubAPI(repository)
        self.sleep, self.clock = sleep, clock
        if type(startup_feedback) is not bool:raise ValueError('startup_feedback must be bool')
        self.startup_feedback=startup_feedback
        if type(diagnostic_traceback) is not bool:raise ValueError('diagnostic_traceback must be bool')
        self.diagnostic_traceback=diagnostic_traceback

    def __call__(self, patch, repo):
        run_id = 'repair-' + uuid.uuid4().hex
        root = self.work_dir / self.instance_id / run_id
        root.mkdir(parents=True, exist_ok=False)
        raw = patch.encode('utf-8')
        sha = hashlib.sha256(raw).hexdigest()
        meta = {'run_id':run_id, 'instance_id':self.instance_id, 'patch_sha256':sha,
                'repository':self.repository, 'ref':self.ref, 'dataset':self.dataset, 'split':self.split}
        (root/'request.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
        (root/'candidate.diff').write_bytes(raw)
        result = {'status':'error', 'work_dir':str(root), 'cloud_request':meta,
                  'feedback_excerpt_version':TEST_FAILURE_EXCERPT_VERSION}
        try:
            if not raw.strip() or len(raw) > MAX_PATCH_BYTES:
                raise ValueError('cloud_patch_empty_or_exceeds_35000_bytes')
            if self.diagnostic_traceback:
                if not hasattr(self.api,'preflight'):raise ValueError('diagnostic_preflight_unavailable')
                self.api.preflight(self.ref,diagnostic_traceback=True)
            inputs={
                'dataset_name':self.dataset, 'split':self.split, 'instance_id':self.instance_id,
                'run_id':run_id, 'patch_b64':base64.b64encode(raw).decode('ascii'),
                'patch_sha256':sha, 'test_timeout':str(self.timeout)}
            if self.diagnostic_traceback:inputs['diagnostic_traceback']='true'
            self.api.request('actions/workflows/'+WORKFLOW+'/dispatches', {'ref':self.ref, 'inputs':inputs})
            deadline, run = self.clock() + self.wait_timeout, None
            while self.clock() < deadline:
                if run is None:
                    runs = self.api.request('actions/workflows/'+WORKFLOW+'/runs?event=workflow_dispatch&per_page=100')['workflow_runs']
                    matches = [r for r in runs if r.get('display_title') == run_id]
                    if len(matches) > 1:
                        raise ValueError('cloud_duplicate_run_identity')
                    if matches:
                        run = matches[0]
                        meta.update(workflow_run_id=run['id'], workflow_url=run['html_url'],
                                    head_sha=run['head_sha'])
                        (root/'request.json').write_text(json.dumps(meta,indent=2),encoding='utf-8')
                if run is not None:
                    state = self.api.request('actions/runs/'+str(run['id']))
                    if state['status'] == 'completed':
                        result['workflow_conclusion'] = state.get('conclusion')
                        if state.get('conclusion') != 'success':
                            raise ValueError('cloud_workflow_'+str(state.get('conclusion')))
                        artifacts = self.api.request('actions/runs/'+str(run['id'])+'/artifacts')['artifacts']
                        artifacts = [a for a in artifacts if a['name'] == 'swebench-'+run_id and not a.get('expired')]
                        if len(artifacts) != 1:
                            raise ValueError('cloud_artifact_missing_or_ambiguous')
                        artifact = artifacts[0]
                        if artifact.get('size_in_bytes', 0) > MAX_ARTIFACT_BYTES:
                            raise ValueError('cloud_artifact_too_large')
                        data = self.api.request('actions/artifacts/'+str(artifact['id'])+'/zip', binary=True)
                        with zipfile.ZipFile(io.BytesIO(data)) as archive:
                            names = [n for n in archive.namelist() if n == 'verdict.json']
                            if len(names) != 1 or archive.getinfo(names[0]).file_size > 512000:
                                raise ValueError('cloud_verdict_missing_or_too_large')
                            verdict = json.loads(archive.read(names[0]))
                        if not isinstance(verdict,dict):
                            raise ValueError('cloud_invalid_verdict_object')
                        for key in ('run_id','instance_id','patch_sha256','dataset','split'):
                            if verdict.get(key) != meta[key]:
                                raise ValueError('cloud_verdict_identity_mismatch:'+key)
                        if verdict.get('status') == 'error':
                            result.update(reason=verdict.get('reason','cloud_harness_error'),
                                          feedback=test_failure_excerpt(str(verdict.get('feedback',''))))
                        else:
                            resolved = verdict.get('official_resolved')
                            if type(resolved) is not bool or verdict.get('status') != ('plausible' if resolved else 'failed'):
                                raise ValueError('cloud_invalid_official_verdict')
                            result.update(status=verdict['status'],official_resolved=resolved,
                                          feedback=test_failure_excerpt(str(verdict.get('feedback',''))))
                        (root/'verdict.json').write_text(json.dumps(verdict,indent=2),encoding='utf-8')
                        # Prefer the actual bounded test log over a verdict containing a truncated report dict.
                        # Official status/resolved and saved raw verdict remain unchanged.
                        result['feedback_source']='verdict'
                        raw_logs={}
                        if result['status'] in ('failed','error'):
                            try:
                                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                                    from .cloud_log_evidence import read_archive_feedback
                                    derived,raw_logs,_=read_archive_feedback(archive,verdict,allow_diagnostic=self.diagnostic_traceback)
                                    result.update(derived)
                                    for name,raw_log in raw_logs.items():
                                        (root/name).parent.mkdir(parents=True,exist_ok=True)
                                        (root/name).write_bytes(raw_log)
                                    (root/'feedback_provenance.json').write_text(json.dumps(derived['feedback_provenance'],indent=2),encoding='utf-8')
                            except (ValueError,KeyError,zipfile.BadZipFile):pass
                        if self.startup_feedback and result['status'] in ('failed','plausible'):
                            stage_report,stage_log=None,None
                            try:
                                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                                    if result['status']=='plausible':
                                        from .cloud_log_evidence import read_archive_feedback
                                        _,raw_logs,_=read_archive_feedback(archive,verdict)
                                    if archive.namelist().count('report.json')==1 and archive.getinfo('report.json').file_size<=2000000:
                                        stage_report=json.loads(archive.read('report.json')).get(self.instance_id)
                                    name='run_instance.log' if 'run_instance.log' in raw_logs else 'test_output.txt'
                                    if name in raw_logs and len(raw_logs[name])<=200000:
                                        stage_log=raw_logs[name].decode('utf-8',errors='replace')
                            except (ValueError,AttributeError,KeyError,zipfile.BadZipFile):pass
                            from .cloud_stage_feedback import attach
                            attach(result,patch,stage_report,stage_log)
                        break
                self.sleep(15)
            else:
                raise ValueError('cloud_wait_timeout: inspect request.json before resubmitting')
        except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
            result.update(reason='cloud_official_environment_error',feedback=str(exc)[:2000])
        (root/'validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        return result

def add_cloud_arguments(parser):
    parser.add_argument('--repair-cloud-repo', help='Use GitHub Actions official tests: owner/repo')
    parser.add_argument('--repair-cloud-ref', default='wp2-target-selection', help='Branch containing deployed cloud workflow')
    parser.add_argument('--repair-cloud-startup-feedback',action='store_true',help='Opt-in saved-log execution-stage feedback; no verdict changes')

def cloud_factory(args, work_dir, dataset='SWE-bench/SWE-bench', split='test', timeout=1800):
    api = GitHubAPI(args.repair_cloud_repo)
    api.preflight(args.repair_cloud_ref)
    def factory(ticket):
        return CloudOfficialRepairValidator(ticket.get('instance_id') or ticket['ticket_id'],
            work_dir, args.repair_cloud_repo, args.repair_cloud_ref, dataset, split, timeout, api=api,
            startup_feedback=getattr(args,'repair_cloud_startup_feedback',False))
    return factory
