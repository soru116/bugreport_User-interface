"""Standalone bounded failure excerpt; standard library only."""
import re
TEST_FAILURE_EXCERPT_VERSION = "test-failure-priority-v4"

def test_failure_excerpt(log, limit=2000):
    """Keep actual test failures ahead of echoed patch/docstring examples.

    Output order is deliberate: exception, failing test IDs, stack locations.
    The compact prompt calls this again, so the format remains parseable.
    """
    if limit <= 0:
        return ''
    clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(log))
    lines, in_diff = [], False
    for line in clean.splitlines():
        if line.startswith(('diff --git ', '--- a/', '+++ b/', '@@ ')):
            in_diff = True
        if in_diff and (re.match(r'^\d{4}-\d\d-\d\d[ T]', line) or
                        (line and not line.startswith(('+', '-', ' ', '\\', '@@ ', 'diff --git ', 'index ')))):
            in_diff = False
        if not in_diff:
            lines.append(line)
    error_pattern = re.compile(
        r'^\s*(?:E\s+)?((?:[\w.]+(?:Error|Exception)|re\.error|Error|Exception|Failed)(?::.*)?)$')
    warning_pattern=re.compile(r'^\s*E\s+([\w.]*Warning(?::.*)?)$')
    errors, failed, frames = [], [], []
    for line in lines:
        assertion=re.match(r'^\s*E\s+(assert\b.*)',line)
        if assertion:errors.append('Assertion evidence: '+assertion.group(1))
        if line.strip().startswith('Assertion evidence: '):errors.append(line.strip())
        match = error_pattern.match(line)
        if match:
            errors.append(match.group(1).strip())
        warning=warning_pattern.match(line)
        if warning:errors.append('Warning-as-error: '+warning.group(1).strip())
        if line.strip().startswith('Warning-as-error: '):errors.append(line.strip())
        match = re.match(r'^\s*(FAILED|ERROR)\s+(.+)', line)
        if match:
            name,separator,detail=match.group(2).partition(' - ')
            failed.append(match.group(1)+' '+name.strip())
            if separator:
                diagnostic=error_pattern.match(detail.strip())
                if diagnostic:errors.append(diagnostic.group(1).strip())
                if re.match(r'^[\w.]*Warning:',detail.strip()):errors.append('Warning-as-error: '+detail.strip())
        if re.match(r'^\s*File "[^"\n]+", line \d+', line) or re.match(
                r'^\s*[^\n]+\.py:\d+(?::|$)', line):
            if not re.search(r'\.py:\d+:\s*[\w.]*Warning:',line):frames.append(line.strip())
    def unique(values):
        return list(dict.fromkeys(values))
    errors, failed, frames = unique(errors), unique(failed), unique(frames)
    if not (errors or failed):
        # Preserve short backend/environment diagnostics; no invented verdict.
        return '\n'.join(lines)[-limit:]
    parts = []
    def add(values, budget, clip=False):
        remaining = budget
        for value in values:
            if len(value) > remaining:
                if not clip:
                    break
                value = value[:remaining]
            if value:
                parts.append(value)
                remaining -= len(value)+1
            if remaining <= 0:
                break
    # Reserve space for traceback even when many tests fail in one run.
    add(errors[:3], max(1,limit//3), clip=True)
    add(failed, max(1,limit//3))
    # Prefer project locations over Python/runtime frames.
    frames.sort(key=lambda frame: bool(re.search(r'/lib/python|site-packages|unittest/|_pytest/', frame)))
    if frames:
        add(['Traceback context:', *frames], max(0,limit-sum(len(p)+1 for p in parts)))
    return '\n'.join(parts)[:limit]
