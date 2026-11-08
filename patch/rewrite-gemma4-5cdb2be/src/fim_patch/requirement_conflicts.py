"""Public dependency removal evidence mapped to observed AST facts, not new fixes."""
import copy
import re
from .scope_planning import public_text

VERSION='public-source-conflicts-v1'
IDENT=r'`?([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)`?'
REMOVE=re.compile(r'\b(?:avoid(?:ing)?\s+(?:(?:the\s+)?(?:use\s+of|using|dependency\s+on|dependence\s+on)\s+)?|'
                  r'without\s+(?:(?:using|relying\s+on)\s+)?|'
                  r'no\s+longer\s+(?:use|using)\s+|'
                  r'remov(?:e|ing)\s+(?:the\s+)?(?:use\s+of|dependency\s+on)\s+)'+IDENT,re.I)
KEEP=re.compile(r'\b(?:keep\s+(?:using\s+)?|continue\s+using\s+|retain\s+|preserve\s+|must\s+use\s+)'+IDENT,re.I)


def mark_conflicts(summary,ticket):
    """Only explicit token-boundary matches; ambiguity/negation stay unknown."""
    public=public_text(ticket).replace('\u200b','')
    available={a for f in summary['facts'] for a in f.get('attributes',[])}
    evidence=[];bindings={}
    for intent,pattern in (('remove',REMOVE),('keep',KEEP)):
        for match in pattern.finditer(public):
            identifier=match.group(1)
            paths=sorted(a for a in available if a==identifier or
                         ('.' not in identifier and a.split('.')[-1]==identifier))
            before=public[max(0,match.start()-45):match.start()]
            uncertain=bool(re.search(r'(?:\bnot|\bnever|\bdo not|\bdon.t|\bshould not)\s*$',before,re.I))
            uncertain=uncertain or bool(re.search(r'\b(?:might|could|perhaps|maybe|if)\b[^.!?\n]*$',before,re.I))
            item=dict(intent=intent,identifier=identifier,excerpt=match.group(0),
                      offset=match.start(),matched_source_attributes=paths,
                      status='unknown' if uncertain or len(paths)!=1 else 'explicit')
            evidence.append(item)
            if item['status']=='explicit':bindings.setdefault(paths[0],set()).add(intent)
    contradictory={a for a,intents in bindings.items() if len(intents)>1}
    removed={a for a,intents in bindings.items() if intents=={'remove'}}
    marked=copy.deepcopy(summary)
    for fact in marked['facts']:
        attrs=set(fact.get('attributes',[]))
        fact['requirement_status']=('ambiguous_public_requirements' if attrs & contradictory else
                                    'explicitly_requested_replacement' if attrs & removed else
                                    'observed_only_not_verified_preservation')
        fact['conflicting_attributes']=sorted(attrs & removed)
    audit=dict(version=VERSION,origin='public_ticket_fields_plus_original_ast',evidence=evidence,
               explicitly_removed=sorted(removed),contradictory_attributes=sorted(contradictory),
               status='ambiguous' if contradictory else 'conflict_identified' if removed else 'unknown',
               inferred_replacement=None,semantic_contract_verified=False,model_calls=0)
    # Render the same ordered fact lines; annotate, never write a replacement.
    original=summary['rendered'].splitlines(keepends=True)
    tags={'# L'+str(f['line'])+' '+f['kind']+': '+f['detail']+'\n':f['requirement_status'] for f in marked['facts']}
    header=('# PUBLIC REQUIREMENT / ORIGINAL SOURCE AUDIT:\n'
            '# REPLACE_REQUESTED marks old dependencies explicitly avoided by the issue; do not treat them as preservation requirements.\n'
            '# UNKNOWN_CONFLICT marks contradictory public requests; no replacement is inferred. Other facts are observations only.\n')
    tail='# Tagged source lines omitted for budget: {count}. Full audit saved separately.\n'
    rendered=header;omitted=0
    for line in original:
        match=re.match(r'# L(\d+) (\w+):',line)
        if not match:continue
        status=tags[line]
        tag='REPLACE_REQUESTED ' if status=='explicitly_requested_replacement' else 'UNKNOWN_CONFLICT ' if status=='ambiguous_public_requirements' else ''
        value='# '+tag+line[2:]
        if len(rendered)+len(value)+len(tail.format(count=len(marked['facts'])))<=summary['max_chars']:rendered+=value
        else:omitted+=1
    rendered+=tail.format(count=omitted+summary['omitted_facts'])
    marked.update(rendered=rendered,rendered_chars=len(rendered),requirement_conflicts=audit,
                  omitted_facts=omitted+summary['omitted_facts'],truncated=bool(omitted or summary['omitted_facts']))
    return marked
