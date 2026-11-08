"""Explicit research ablations over the same native-FIM repair machinery."""
import copy
from dataclasses import dataclass,asdict,replace
from .unified_repair import UnifiedRepairGenerator

VERSION='paired-fim-arms-v1'

@dataclass(frozen=True)
class Arm:
    adaptive_scope:bool=True
    context:str='local'
    extraction:bool=False
    diagnostics:bool=False
    test_feedback:bool=False
    source_summary:bool=False
    requirement_conflicts:bool=False
    existing_tests:bool=False
    source_api_contracts:bool|None=None

ARMS={
    'A0':Arm(adaptive_scope=False,context='file'),
    'A1':Arm(),
    'A2':Arm(extraction=True),
    'A3':Arm(extraction=True,diagnostics=True),
    'A4':Arm(extraction=True,diagnostics=True,test_feedback=True),
    'A3_file':Arm(context='file',extraction=True,diagnostics=True),
    'A3_summary':Arm(extraction=True,diagnostics=True,source_summary=True),
    'A3_conflicts':Arm(extraction=True,diagnostics=True,source_summary=True,requirement_conflicts=True),
    'A3_tests':Arm(extraction=True,diagnostics=True,existing_tests=True),
    'A4_api_off':Arm(extraction=True,diagnostics=True,test_feedback=True,source_api_contracts=False),
    'A4_api_on':Arm(extraction=True,diagnostics=True,test_feedback=True,source_api_contracts=True),
}
PAIRS={'context':('A3_file','A3'),'scope_context':('A0','A1'),
       'parser':('A1','A2'),'diagnostics':('A2','A3'),'test_feedback':('A3','A4'),
       'source_summary':('A3','A3_summary'),'requirement_conflicts':('A3_summary','A3_conflicts'),
       'existing_tests':('A3','A3_tests'),'source_api_contracts':('A4_api_off','A4_api_on')}

class PairedRepairGenerator(UnifiedRepairGenerator):
    def __init__(self,backend,arm,policy=None,validator_factory=None):
        if arm not in ARMS:raise ValueError('unknown paired arm')
        super().__init__(backend,policy,validator_factory)
        if self.policy.domain_adapters:raise ValueError('generic paired arms require domain adapters disabled')
        self.arm_name=arm;self.arm=ARMS[arm];self.forward_test_feedback=self.arm.test_feedback
        if self.arm.source_api_contracts is not None:
            self.policy=replace(self.policy,source_api_contracts=self.arm.source_api_contracts)

    def inner(self,backend,scope='body',ceiling=None,validator=None,multi=False):
        inner=super().inner(backend,scope,ceiling,validator,multi)
        inner.source_summary_enabled=self.arm.source_summary
        inner.requirement_conflicts_enabled=self.arm.requirement_conflicts
        inner.fim_context_mode=self.arm.context
        inner.safe_body_feedback=False
        inner.body_boundary_extraction=self.arm.extraction and scope=='body'
        inner.source_none_probe=self.arm.diagnostics and scope=='body'
        inner.controlled_body_options_requested=True
        inner.helper_contracts=self.arm.diagnostics or multi
        inner.operation_contract_preview=self.arm.diagnostics and scope=='body'
        inner.forward_test_feedback=self.forward_test_feedback
        # Keep test-failure target choice equal; only forwarding test text differs.
        inner.validation_retry_target_policy='fixed_selected'
        return inner

    def plan(self,ticket,location,repo):
        result=super().plan(ticket,location,repo)
        if result['patch_status']!='planned' or self.arm.adaptive_scope:return result
        choice=result['unified_plan'];body=self.inner(self.backend).generate(ticket,choice['locked_location'],repo,plan_only=True)
        if (choice['api_plan']['action'] in ('propose_signature_and_behavior_fim','stop_scope_expansion')
            or body['patch_status']!='planned' or body['span']['body_lines']>self.policy.max_body_lines):
            result.update(patch_status='blocked_invalid_context',explanation='fixed_body_control_cannot_express_request_or_exceeds_policy')
            choice['scope_order']=[];return result
        choice.update(route='single',scope_order=['body'],scope_plans={'body':copy.deepcopy(body)})
        return result

    def generate(self,*args,**kwargs):
        if self.arm.existing_tests and not kwargs.get('plan_only',False):
            evidence=getattr(self,'existing_test_evidence',None)
            if evidence is None:raise ValueError('existing test arm requires verified evidence')
            from .existing_test_context import first_context
            context={}
            with first_context(self.backend,evidence,context):result=super().generate(*args,**kwargs)
            result['existing_test_context']=context
        else:result=super().generate(*args,**kwargs)
        result.update(paired_arm=self.arm_name,paired_arm_version=VERSION,paired_arm_settings=asdict(self.arm),
            test_retry_target_policy='fixed_selected',test_feedback_forwarded=self.forward_test_feedback)
        return result
