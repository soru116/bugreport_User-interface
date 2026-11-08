"""Bind verified secondary diagnostic evidence to an exact saved FIM candidate."""
import copy
import hashlib
import json
import zipfile
from .cloud_log_evidence import read_archive_feedback

VERSION = 'verified-diagnostic-fim-feedback-v1'


def replay_candidate(candidate, context=None):
    """Restore an outer first-request context only from matching saved hashes."""
    result = copy.deepcopy(candidate)
    context = context or {}
    if context.get('injected') is True:
        prompt, suffix = candidate['prompt'], candidate['suffix']
        restored = context['context_block'] + prompt
        for text, key in ((prompt, 'base_prefix_sha256'), (suffix, 'suffix_sha256'), (restored, 'prefix_sha256')):
            if hashlib.sha256(text.encode('utf8')).hexdigest() != context.get(key):
                raise ValueError('saved outer context mismatch: ' + key)
        result['prompt'] = restored
    return result


def bind_diagnostic(candidate, archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        if archive.namelist().count('verdict.json') != 1 or archive.getinfo('verdict.json').file_size > 512000:
            raise ValueError('invalid diagnostic parent verdict')
        verdict = json.loads(archive.read('verdict.json'))
        original = (candidate.get('validation') or {}).get('cloud_request') or {}
        if verdict.get('status') != 'failed' or verdict.get('official_resolved') is not False:
            raise ValueError('diagnostic parent is not an official failure')
        for key in ('instance_id', 'patch_sha256', 'dataset', 'split'):
            if not original.get(key) or verdict.get(key) != original[key]:
                raise ValueError('diagnostic candidate identity mismatch: ' + key)
        patch = candidate.get('patch')
        if not isinstance(patch, str) or hashlib.sha256(patch.encode('utf8')).hexdigest() != verdict['patch_sha256']:
            raise ValueError('saved candidate patch differs from diagnostic patch')
        derived, _, _ = read_archive_feedback(archive, verdict, allow_diagnostic=True)
        if (derived.get('feedback_source') != 'diagnostic_test_output'
                or derived.get('diagnostic_failure_details_verified') is not True):
            raise ValueError('verified diagnostic failure details unavailable')
    result = copy.deepcopy(candidate)
    result['validation'] = dict(verdict, **derived, cloud_request={
        key: verdict[key] for key in ('run_id', 'instance_id', 'patch_sha256', 'dataset', 'split')})
    result['validation']['diagnostic_feedback_version'] = VERSION
    result['reason'] = 'validation_failed'
    return result


def provenance_text(validation):
    if validation.get('feedback_source') != 'diagnostic_test_output':
        return ''
    if validation.get('diagnostic_failure_details_verified') is not True:
        return 'Diagnostic details unverified; actual failure remains unknown.'
    return ('Verified secondary test diagnostic; primary official result remains failed. '
            'Traceback is observed failure data, not a repair answer or an official pass.')
