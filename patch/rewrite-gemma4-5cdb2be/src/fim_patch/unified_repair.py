"""One source/public-evidence router and one request budget for Python FIM."""
import copy
import re
import json
import math
from dataclasses import dataclass,asdict
from pathlib import Path

from .generator import FIMPatchGenerator,source_for,_target_function_node
from .scope_planning import plan_scope,public_text
from .shared_budget import SharedBudgetBackend
from .generation_records import annotate_generation
from .iterative import feedback_block

VERSION='unified-repair-policy-v1'


@dataclass(frozen=True)
class RepairPolicy:
    max_generation_calls:int=3
    max_functions:int=2
    max_body_lines:int=120
    seed:int=42
    output_factor:float=2.0
    output_margin:int=32
    domain_adapters:tuple=()
    scope_compatibility:bool=False
    stop_on_target_context_risk:bool=False
    source_api_contracts:bool=False
    api_argument_flow_feedback:bool=False
    property_context:bool=False
    def __post_init__(self):
        from .domain_adapters import validate_adapters
        validate_adapters(self.domain_adapters)
        if type(self.scope_compatibility) is not bool:raise ValueError('scope_compatibility must be bool')
        if type(self.source_api_contracts) is not bool:raise ValueError('source_api_contracts must be bool')
        if type(self.api_argument_flow_feedback) is not bool:raise ValueError('api_argument_flow_feedback must be bool')
        if type(self.property_context) is not bool:raise ValueError('property_context must be bool')
        if type(self.stop_on_target_context_risk) is not bool:raise ValueError('stop_on_target_context_risk must be bool')
        if self.stop_on_target_context_risk and not self.scope_compatibility:raise ValueError('target context gate requires scope_compatibility')
        integers=(self.max_generation_calls,self.max_functions,self.max_body_lines,self.seed,self.output_margin)
        if (any(type(value) is not int for value in integers)
                or type(self.output_factor) not in (int,float) or not math.isfinite(self.output_factor)
                or isinstance(self.max_generation_calls,bool) or not 1<=self.max_generation_calls<=3
                or isinstance(self.max_functions,bool) or not 1<=self.max_functions<=3
                or self.max_body_lines<1 or self.output_factor<1 or self.output_margin<0):
            raise ValueError('invalid unified repair policy')


class UnifiedRepairGenerator:
    def __init__(self,backend,policy=None,validator_factory=None):
        self.backend=backend
        self.policy=policy or RepairPolicy()
        self.validator_factory=validator_factory
    def validator_for(self,ticket):
        validator=self.validator_factory(ticket) if self.validator_factory else None
        if validator is None:return None
        def checked(diff,repo):
            report=validator(diff,repo)
            if report.get('reason')=='baseline_not_verified':
                return dict(report,status='error',environment_error=True)
            return report
        return checked
    def repair_feedback(self,candidate):
        if candidate.get('validation') and not getattr(self,'forward_test_feedback',True):return ''
        text=feedback_block(candidate,compact=True)
        if self.policy.api_argument_flow_feedback:
            text+=(candidate.get('api_argument_flow') or {}).get('feedback_preview','')
        return text
    def annotate_argument_flow(self,candidate):
        if not self.policy.api_argument_flow_feedback:return
        adapter=getattr(self,'_argument_flow_adapter',None)
        if adapter is None or not candidate.get('patch'):return
        try:report=adapter.argument_flow(candidate['patch'])
        except (ValueError,KeyError,SyntaxError,UnicodeError) as exc:
            report=dict(status='unknown',feedback_preview='',reason=str(exc)[:200],advisory_only=True,automatic_rejection=False)
        candidate['api_argument_flow']=report
    def inner(self,backend,scope='body',ceiling=None,validator=None,multi=False):
        owner=self
        class BoundedGenerator(FIMPatchGenerator):
            def generate(this,ticket,location,repo,*,plan_only=False):
                planned=super().generate(ticket,location,repo,plan_only=True)
                if planned['patch_status']!='planned':return planned
                if plan_only:return planned
                if planned['span']['body_lines']>owner.policy.max_body_lines:
                    return dict(planned,patch_status='blocked_invalid_context',explanation='scope_exceeds_shared_body_line_limit')
                this.fim_output_ceiling=owner.ceiling(planned['span'],backend)
                return super().generate(ticket,location,repo)
        inner=BoundedGenerator(backend,candidates=1,seed=self.policy.seed,research_mode=True,
            prompt_layout='compact_fim',fim_scope=scope,fim_output_ceiling=ceiling,
            safe_body_feedback=scope=='body',operation_contract_preview=scope=='body',
            domain_adapters=self.policy.domain_adapters,helper_contracts=True,helper_implementations=multi,
            fixed_signature_body=True,include_original_body=False,local_context=True,
            target_policy='first_supported_ast_top5',max_body_lines=0,validator=validator)
        if self.policy.source_api_contracts and getattr(self,'_source_api_adapter',None):
            inner.source_api_checker=self._source_api_adapter.check_patch
        return inner
    def plan(self,ticket,location,repo):
        base=self.inner(self.backend).generate(ticket,location,repo,plan_only=True)
        if base['patch_status']!='planned':return base
        path,symbol=base['bug_location']['file'],base['span']['symbol']
        node=_target_function_node((Path(repo)/path).read_text(encoding='utf-8-sig'),symbol)
        row=dict(file_path=path,symbol_qualified_name=symbol,
                 start_line=min([node.lineno]+[d.lineno for d in node.decorator_list]),end_line=node.end_lineno)
        locked=copy.deepcopy(location);locked['stage3_ranked_symbols']=[row]
        raw,_,_=source_for(ticket,location,repo,row)
        api=plan_scope(raw,base['span'],ticket,path)
        route='single';scopes=[];scope_plans={}
        if api['action']=='propose_signature_and_behavior_fim':
            route='signature_behavior';scopes=['body'];scope_plans['body']=copy.deepcopy(base)
        elif api['action']=='stop_scope_expansion':
            scopes=[]
        else:
            from .multi_function import related_chain
            public=public_text(ticket)
            relevant=[edge for edge in related_chain(raw.decode('utf-8-sig'),symbol)
                      if re.search(r'(?<!\w)'+re.escape(edge['symbol'].split('.')[-1])+r'(?!\w)',public)]
            if relevant and self.policy.max_functions>1:
                route='related_functions';scopes=['body'];scope_plans['body']=copy.deepcopy(base)
            else:
                for scope in ('expression','statement','body'):
                    planned=base if scope=='body' else self.inner(self.backend,scope).generate(ticket,locked,repo,plan_only=True)
                    if planned['patch_status']=='planned' and planned['span']['body_lines']<=self.policy.max_body_lines:
                        scopes.append(scope);scope_plans[scope]=copy.deepcopy(planned)
        if route=='signature_behavior' and self.policy.max_generation_calls<2:scopes=[]
        if route in ('signature_behavior','related_functions') and base['span']['body_lines']>self.policy.max_body_lines:scopes=[]
        compatibility=None
        if self.policy.scope_compatibility and scope_plans:
            from .scope_compatibility import assess_scope
            compatibility=assess_scope(raw,symbol,ticket,{k:v['span'] for k,v in scope_plans.items()})
            if compatibility['action']=='start_at_body' and route=='single':scopes=['body']
            elif compatibility['action']=='stop_no_covering_scope':scopes=[]
            if self.policy.stop_on_target_context_risk and compatibility.get('target_review_required'):
                scopes=[]
        if not scopes:
            reason=('target_context_unknown_under_opt_in_gate' if self.policy.stop_on_target_context_risk
                    and compatibility and compatibility.get('target_review_required') else 'no_supported_scope_within_shared_policy')
            base.update(patch_status='blocked_invalid_context',explanation=reason)
        base['unified_plan']=dict(version=VERSION,route=route,scope_order=scopes,
            source='predicted_fl_plus_source_and_public_evidence',original_fl_rank=base['target_rank'],
            locked_location=locked,row=row,api_plan=api,scope_plans=scope_plans,policy=asdict(self.policy))
        if compatibility is not None:base['unified_plan']['scope_compatibility']=compatibility
        return base
    def ceiling(self,span,budget):
        counter=getattr(budget,'_token_counter',None)
        if counter is None:raise ValueError('tokenizer_verification_unavailable')
        # One frozen formula for every selected hole, not case-specific token caps.
        tokens=counter.count(span['original_middle'],'')
        return min(budget.max_new_tokens,max(128,int(tokens*self.policy.output_factor)+self.policy.output_margin))
    def generate(self,ticket,location,repo,*,plan_only=False):
        if self.policy.property_context and not plan_only:
            from .property_context import extract
            from .property_context_integration import first_context
            from .source_api_integration import SourceAPIAdapter
            plan=self.plan(ticket,location,repo)
            if plan['patch_status']!='planned':return plan
            if plan['unified_plan']['route']!='single':
                result=self._generate_with_argument_flow(ticket,location,repo,plan_only=plan_only)
                result['property_context']=dict(status='unsupported_route',injected=False)
                return result
            adapter=SourceAPIAdapter(repo,plan['base_commit']);file=plan['bug_location']['file'];symbol=plan['span']['symbol']
            try:context=extract(adapter.index,file,adapter.read(file).decode('utf-8-sig'),symbol)
            except (ValueError,KeyError,SyntaxError,UnicodeError) as exc:
                context=dict(rendered='',unknowns=[str(exc)[:200]],file=file,symbol=symbol)
            report={}
            with first_context(self.backend,context,report):
                result=self._generate_with_argument_flow(ticket,location,repo,plan_only=plan_only)
            result['property_context']=report
            return result
        return self._generate_with_argument_flow(ticket,location,repo,plan_only=plan_only)
    def _generate_with_argument_flow(self,ticket,location,repo,*,plan_only=False):
        if self.policy.api_argument_flow_feedback and not plan_only:
            from .source_api_integration import SourceAPIAdapter
            plan=self.plan(ticket,location,repo)
            if plan['patch_status']!='planned':return plan
            if plan['unified_plan']['route']!='single':
                result=self._generate_with_source_context(ticket,location,repo,plan_only=plan_only)
                result['api_argument_flow_feedback']=dict(enabled=True,status='unsupported_route',advisory_only=True)
                return result
            self._argument_flow_adapter=SourceAPIAdapter(repo,plan['base_commit'])
            try:
                result=self._generate_with_source_context(ticket,location,repo,plan_only=plan_only)
                result['api_argument_flow_feedback']=dict(enabled=True,status='annotated_for_retry',advisory_only=True,automatic_rejection=False)
                return result
            finally:self._argument_flow_adapter=None
        return self._generate_with_source_context(ticket,location,repo,plan_only=plan_only)
    def _generate_with_source_context(self,ticket,location,repo,*,plan_only=False):
        if self.policy.source_api_contracts and not plan_only:
            from .source_api_integration import SourceAPIAdapter,first_context
            # A separate opt-in wrapper; normal defaults and the shared ledger are unchanged.
            plan=self.plan(ticket,location,repo)
            if plan['patch_status']!='planned':return plan
            path=plan['bug_location']['file'];symbol=plan['span']['symbol']
            self._source_api_adapter=SourceAPIAdapter(repo,plan['base_commit'])
            report={}
            try:evidence=self._source_api_adapter.context(path,symbol)
            except (ValueError,KeyError,SyntaxError,UnicodeError) as exc:
                evidence={'compact_context':''};report['source_context_unknown']=str(exc)[:200]
            try:
                with first_context(self.backend,evidence,report):result=self._generate(ticket,location,repo,plan_only=plan_only)
                result['source_api_context']=report
                return result
            finally:self._source_api_adapter=None
        return self._generate(ticket,location,repo,plan_only=plan_only)
    def _generate(self,ticket,location,repo,*,plan_only=False):
        plan=self.plan(ticket,location,repo)
        if plan_only or plan['patch_status']!='planned':return plan
        choice=plan['unified_plan'];budget=SharedBudgetBackend(self.backend,self.policy.max_generation_calls)
        budget.describe()
        validator=self.validator_for(ticket)
        history=[];stop='shared_generation_budget_exhausted';directive='';index=0
        if choice['route']=='signature_behavior':
            from .scope_execution import execute_scope
            row=choice['row'];raw,commit,path=source_for(ticket,location,repo,row)
            span=choice['scope_plans']['body']['span']
            inner=self.inner(budget,'body',self.ceiling(span,budget),validator)
            inner.test_first_scope=True
            span=dict(span,fixed_output_budget=self.ceiling(span,budget))
            result=copy.deepcopy(plan);result['scope_plan']=choice['api_plan']
            result=execute_scope(inner,ticket,location,repo,row,raw,commit,path,span,result)
            history=[copy.deepcopy(result)];stop='tests_passed' if result.get('validation_status')=='plausible' else 'signature_behavior_complete'
        elif choice['route']=='related_functions':
            from .multi_function import MultiFunctionRepairGenerator
            span=choice['scope_plans']['body']['span']
            inner=self.inner(budget,'body',self.ceiling(span,budget),multi=True)
            result=MultiFunctionRepairGenerator(inner,self.policy.max_generation_calls,
                self.policy.max_functions,self.validator_for if self.validator_factory else None).generate(ticket,location,repo)
            history=result.get('repair_history') or [result];stop=result.get('repair_stop_reason','related_plan_complete')
        else:
            selected=None;seen=set()
            while budget.remaining:
                scope=choice['scope_order'][index]
                span=choice['scope_plans'][scope]['span']
                inner=self.inner(budget,scope,self.ceiling(span,budget),validator)
                inner.seed=self.policy.seed+len(budget.events);inner.extra_directive=directive
                result=inner.generate(ticket,choice['locked_location'],repo)
                for candidate in result.get('candidates') or []:self.annotate_argument_flow(candidate)
                result['repair_round']=len(history)+1;result['target_rank']=choice['original_fl_rank']
                history.append(copy.deepcopy(result))
                candidates=result.get('candidates') or []
                candidate=candidates[-1] if candidates else {}
                if result['patch_status']=='generated':
                    selected=result;stop='tests_passed' if validator else 'static_candidate_without_verifier';break
                reason=str(candidate.get('reason') or result.get('explanation',''))
                validation=candidate.get('validation') or {}
                if (validation.get('status')=='error' or any(term in reason for term in
                    ('Connection refused','10061','timed out','HTTP Error','source_snapshot','tokenizer_verification','context_budget_exceeded','shared_generation_budget'))):
                    stop='environment_or_budget_error';break
                from .failure_identity import failure_evidence
                evidence=failure_evidence(validation)
                key=(candidate.get('executable_logic_sha256') or candidate.get('logic_sha256'),reason,json.dumps(evidence,sort_keys=True))
                if key[0] and key in seen:stop='no_logic_progress';break
                if key[0]:seen.add(key)
                if result['patch_status']!='no_valid_candidate' or not candidates:stop='blocked_context';break
                directive=self.repair_feedback(candidate)
                if validation.get('status')!='failed' and index+1<len(choice['scope_order']):index+=1
            if selected:result=selected
            result.update(repair_history=history,repair_rounds_used=len(history),repair_stop_reason=stop)
        result.update(unified_repair=VERSION,unified_plan=choice,shared_generation_budget=budget.report(),
                      model_calls=len(budget.events),model_call_count_kind='conservative_infill_attempts',
                      unified_stop_reason=stop,verifier_configured=validator is not None,
                      scope_selection_origin='predicted_fl_plus_source_and_public_evidence')
        if choice['route']=='signature_behavior':result['unified_attempts']=history
        reports=[c.get('validation') or {} for c in result.get('candidates',[])]
        if result.get('selected_candidate_id'):
            reports=[c.get('validation') or {} for c in result.get('candidates',[]) if c.get('candidate_id')==result['selected_candidate_id']]
        if any(r.get('official_resolved') is True for r in reports) and result.get('patch_status')=='generated':
            result['official_resolved']=True
        elif any(r.get('official_resolved') is False for r in reports):result['official_resolved']=False
        # Do not serialize full repository halves inside every plan/round.
        result['unified_plan']=copy.deepcopy(choice)
        def trim_plans(attempt):
            for key in ('prefix','suffix','original_middle'):attempt.get('span',{}).pop(key,None)
            for planned in (attempt.get('unified_plan') or {}).get('scope_plans',{}).values():trim_plans(planned)
            for field in ('repair_history','unified_attempts'):
                for child in attempt.get(field,[]):trim_plans(child)
        trim_plans(result)
        return annotate_generation(result)
