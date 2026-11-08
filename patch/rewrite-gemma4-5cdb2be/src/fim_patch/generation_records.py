"""Account for static/apply-valid candidates separately from test acceptance."""
import copy
import hashlib
import ast
import json

VERSION = 'static-candidate-accounting-v1'
LOGIC_VERSION = 'python-ast-candidate-v1'
EXECUTABLE_VERSION = 'python-executable-ast-v1'


def logic_fingerprint(sources,*,executable=False):
    """Fingerprint complete changed Python files; never execute source.

    Ignore whitespace/comments/locations, retain constants and docstrings.
    Unsupported sources remain unknown, never fall back to a text identity.
    """
    normalized = {}
    try:
        for path, source in sources:
            if path in normalized or not str(path).endswith('.py'):
                return None
            if isinstance(source, bytes):
                source = source.decode('utf-8-sig')
            tree=ast.parse(source)
            if executable:
                from .generator import _without_docstrings
                tree=_without_docstrings(tree)
            normalized[path] = ast.dump(tree, include_attributes=False)
        if not normalized:
            return None
        payload = json.dumps(normalized, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(payload.encode()).hexdigest()
    except (SyntaxError, ValueError, UnicodeError, TypeError):
        return None


def repeated_failed_logic(history):
    """Only stop on a checked AST repeat with the same failure evidence."""
    def identity(attempt):
        candidates = attempt.get('candidates') or []
        if len(candidates) != 1:
            return None
        candidate = candidates[0]
        digest = candidate.get('logic_sha256')
        if (candidate.get('logic_fingerprint_version') != LOGIC_VERSION or not digest
                or candidate.get('apply_check_status') != 'passed'
                or candidate.get('status') != 'rejected'):
            return None
        from .failure_identity import failure_evidence
        validation = failure_evidence(candidate.get('validation') or {})
        evidence = dict(reason=candidate.get('reason'), validation=validation,
                        source_contract_check=candidate.get('source_contract_check'))
        target = ((attempt.get('bug_location') or {}).get('file'),
                  (attempt.get('span') or {}).get('symbol'))
        return target, digest, json.dumps(evidence, sort_keys=True, ensure_ascii=True)
    if len(history) < 2 or history[-1].get('patch_status') != 'no_valid_candidate':
        return None
    current = identity(history[-1])
    if current is None:
        return None
    for index, previous in enumerate(history[:-1], 1):
        if previous.get('patch_status') == 'no_valid_candidate' and identity(previous) == current:
            return dict(version=LOGIC_VERSION, status='no_logic_progress',
                        repeated_round=previous.get('repair_round', index),
                        logic_sha256=current[1], same_failure_evidence=True)
    return None

def annotate_generation(result):
    entries = {}
    def retain(patch, origin, validation=None, modified_files=None, logic=None,executable=None):
        if not isinstance(patch,str) or not patch.strip():
            return
        digest=hashlib.sha256(patch.encode('utf-8')).hexdigest()
        item=entries.setdefault(digest,{'patch_sha256':digest,'patch':patch,
            'static_status':'passed','apply_check_status':'passed',
            'modified_files':modified_files or [],'attempts':[]})
        item['attempts'].append(dict(origin,validation=copy.deepcopy(validation)))
        if logic:
            item['logic_sha256'] = logic
            item['logic_fingerprint_version'] = LOGIC_VERSION
        if executable:item.update(executable_logic_sha256=executable,executable_fingerprint_version=EXECUTABLE_VERSION)
    history=result.get('repair_history') or [result]
    for index, attempt in enumerate(history,1):
        if result.get('multi_function_repair'):
            # Component bodies are not full combined candidates. Composition
            # checks must pass before a combined diff can be counted.
            if attempt.get('combined_apply_status') == 'passed':
                retain(attempt.get('combined_patch'),{'round':attempt.get('repair_round',index),'kind':'combined'},
                       attempt.get('combined_validation'),
                       logic=attempt.get('combined_logic_sha256') if attempt.get('logic_fingerprint_version')==LOGIC_VERSION else None,
                       executable=attempt.get('combined_executable_logic_sha256') if attempt.get('executable_fingerprint_version')==EXECUTABLE_VERSION else None)
        else:
            for candidate in attempt.get('candidates',[]):
                if candidate.get('apply_check_status') == 'passed':
                    retain(candidate.get('patch'),{'round':attempt.get('repair_round',index),
                        'candidate_id':candidate.get('candidate_id'),'kind':'single'},
                        candidate.get('validation'),candidate.get('modified_files'),
                        candidate.get('logic_sha256') if candidate.get('logic_fingerprint_version') == LOGIC_VERSION else None,
                        candidate.get('executable_logic_sha256') if candidate.get('executable_fingerprint_version')==EXECUTABLE_VERSION else None)
    # Compatibility with older successful records lacking candidate details.
    if result.get('patch_status') == 'generated' and result.get('apply_status') == 'passed':
        patch=result.get('patch')
        digest=hashlib.sha256(patch.encode()).hexdigest() if isinstance(patch,str) else None
        if digest not in entries:
            retain(patch,{'kind':'accepted_result'},modified_files=result.get('modified_files'))
    result.update(generation_accounting_version=VERSION,
        generation_status='generated' if entries else 'not_generated',
        generated_before_validation=bool(entries),static_candidate_count=len(entries),
        generated_candidates=list(entries.values()))
    groups = {}
    for item in entries.values():
        digest = item.get('logic_sha256')
        if digest:
            group = groups.setdefault(digest, dict(logic_sha256=digest, patch_sha256s=[], attempts=[]))
            group['patch_sha256s'].append(item['patch_sha256'])
            group['attempts'].extend(copy.deepcopy(item['attempts']))
    unknown = sum(not item.get('logic_sha256') for item in entries.values())
    result.update(logic_accounting_version=LOGIC_VERSION,
                  logical_static_candidate_count=len(groups),
                  logic_unclassified_candidate_count=unknown,
                  logic_accounting_complete=unknown == 0,
                  logical_candidate_groups=list(groups.values()))
    result['executable_static_candidate_count']=len({c['executable_logic_sha256'] for c in entries.values() if c.get('executable_logic_sha256')})
    result['executable_accounting_version']=EXECUTABLE_VERSION
    result['executable_unclassified_candidate_count']=sum(not c.get('executable_logic_sha256') for c in entries.values())
    return result
