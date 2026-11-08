"""Opt-in, whole-property first-infill context with exact token budgeting."""
from contextlib import contextmanager
import hashlib

VERSION='first-infill-property-context-v1'


def blocks(context):
    text=context.get('rendered') or ''
    for marker in ('<PRE>','<SUF>','<MID>','<EOT>','<FILL_ME>'):text=text.replace(marker,'[marker removed]')
    lines=text.splitlines(keepends=True);header=[];groups=[]
    for line in lines:
        if not line.strip():continue
        if not line.startswith('# '):raise ValueError('property context must contain comment data only')
        if line.startswith('# Observed '):groups.append(line)
        elif groups:groups[-1]+=line
        else:header.append(line)
    return ''.join(header),groups


@contextmanager
def first_context(backend,context,report):
    header,groups=blocks(context)
    report.update(version=VERSION,status='no_first_request',injected=False,offered_properties=len(groups),
        evidence_version=context.get('version'),runtime_dispatch_verified=False,
        unknowns=context.get('unknowns',[]),source_file=context.get('file'),symbol=context.get('symbol'))
    if not groups:
        report['status']='unknown_no_supported_property';yield;return
    counter=getattr(backend,'_token_counter',None)
    if counter is None:raise ValueError('property context requires verified token counter')
    original=backend.infill;had_instance='infill' in backend.__dict__;saved=backend.__dict__.get('infill');first=True
    def infill(prefix,suffix,**kwargs):
        nonlocal first
        if not first:return original(prefix,suffix,**kwargs)
        first=False;output=kwargs.get('max_new_tokens') or backend.max_new_tokens
        base=counter.count(prefix,suffix);included=[]
        for group in groups:
            proposed=header+''.join(included)+group+prefix
            if counter.count(proposed,suffix)+output+8<=backend.num_ctx:included.append(group)
        addition=header+''.join(included) if included else '';actual=addition+prefix
        tokens=counter.count(actual,suffix)
        report.update(status='injected' if included else 'omitted_no_token_space',injected=bool(included),
            included_properties=len(included),omitted_properties=len(groups)-len(included),context_block=addition,
            base_input_tokens=base,input_tokens=tokens,added_tokens=tokens-base,output_limit=output,
            context_limit=backend.num_ctx,margin_tokens=8,fits_model_context=tokens+output+8<=backend.num_ctx,
            suffix_unchanged=True,prefix_sha256=hashlib.sha256(actual.encode()).hexdigest(),
            base_prefix_sha256=hashlib.sha256(prefix.encode()).hexdigest(),suffix_sha256=hashlib.sha256(suffix.encode()).hexdigest())
        if not report['fits_model_context']:raise ValueError('property_context_base_budget_exceeded')
        return original(actual,suffix,**kwargs)
    backend.infill=infill
    try:yield
    finally:
        if had_instance:backend.infill=saved
        else:del backend.infill
