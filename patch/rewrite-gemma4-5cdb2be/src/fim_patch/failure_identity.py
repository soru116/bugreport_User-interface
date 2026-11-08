"""Stable failure evidence; original reports stay intact for auditing."""
import re

VERSION='semantic-failure-identity-v1'
TRANSPORT_FIELDS={'work_dir','report_path','workflow_url','workflow_run_id','run_id','artifact_id','feedback_provenance'}
CLOUD_CONTEXT={'instance_id','dataset','split','repository','ref','head_sha'}


def normalize_text(value):
    # Only known ephemeral directories, never arbitrary source paths/numbers/assert values.
    lines=[]
    for line in value.splitlines(keepends=True):
        stripped=line.strip()
        frame=(stripped.startswith('File "') or re.match(r'^(?:[A-Za-z]:[\\/]|/|[\w.])[\S]*\.py:\d+',stripped))
        if frame:
            line=re.sub(r'(?i)(/tmp/|/private/tmp/)(pip-build-env-[A-Za-z0-9_-]+|tmp[A-Za-z0-9_]{6,})(?=[/\\\s\"\']|$)',r'\1[temp]',line)
            line=re.sub(r'(?i)([A-Z]:[\\/][^\n\"\']*?[\\/]Temp[\\/])(pip-build-env-[A-Za-z0-9_-]+|tmp[A-Za-z0-9_]{6,})(?=[/\\\s\"\']|$)',r'\1[temp]',line)
        lines.append(line)
    return ''.join(lines)


def failure_evidence(value,*,_metadata=True,_text=False):
    if isinstance(value,dict):
        return {key:failure_evidence({k:v for k,v in item.items() if k in CLOUD_CONTEXT},_metadata=False)
                if _metadata and key=='cloud_request' and isinstance(item,dict)
                else failure_evidence(item,_metadata=key in ('after','regression','validation','cloud_stage_diagnostic'),
                                      _text=key in ('feedback','log','stderr','stdout'))
                for key,item in value.items() if not (_metadata and key in TRANSPORT_FIELDS)}
    if isinstance(value,list):return [failure_evidence(item,_metadata=False,_text=_text) for item in value]
    if isinstance(value,str):return normalize_text(value) if _text else value
    return value
