"""Diagnostic-only pytest traceback option rewrite; no test/source changes."""
import hashlib
import re
import shlex

VERSION='diagnostic-traceback-v1'


def verbose_script(script):
    lines=script.splitlines(keepends=True);output=[];changes=[];heredocs=[]
    for number,line in enumerate(lines,1):
        if heredocs:
            terminator,strip_tabs=heredocs[0]
            value=line.rstrip('\r\n').lstrip('\t') if strip_tabs else line.rstrip('\r\n')
            if value==terminator:heredocs.pop(0)
            output.append(line);continue
        declarations=re.findall(r'<<(-?)\s*([\'\"]?)([A-Za-z_][A-Za-z_0-9]*)\2',line)
        if declarations:
            heredocs.extend((name,bool(strip)) for strip,quote,name in declarations)
            output.append(line);continue
        try:tokens=shlex.split(line,comments=True)
        except ValueError:output.append(line);continue
        command=bool(tokens) and (tokens[0]=='pytest' or tokens[:3]==['python','-m','pytest'] or tokens[:3]==['python3','-m','pytest'])
        if command and tokens.count('--tb=no')==1 and not re.search(r'[;&|`]',line):
            replaced,count=re.subn(r'(?<!\S)--tb=no(?=\s|$)','--tb=short',line)
            if count==1:
                assert replaced.replace('--tb=short','--tb=no',1)==line
                changes.append(dict(line=number,option_before='--tb=no',option_after='--tb=short'))
                output.append(replaced);continue
        output.append(line)
    if heredocs:raise ValueError('unterminated heredoc; cannot safely diagnose')
    rewritten=''.join(output)
    return rewritten,dict(version=VERSION,status='modified' if changes else 'unsupported_no_safe_pytest_traceback_option',
        changes=changes,original_script_sha256=hashlib.sha256(script.encode()).hexdigest(),
        diagnostic_script_sha256=hashlib.sha256(rewritten.encode()).hexdigest(),
        test_set_changed=False,patch_changed=False,official_result_overridden=False)
