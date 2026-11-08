"""Optional execution-stage evidence; never changes an official verdict."""
import re

VERSION='cloud-startup-feedback-v1'


def assess(log,model_files,reported_applied):
    applied={m.group(1) for line in log.splitlines() if (m:=re.fullmatch(r'Applied patch (.+) cleanly\.',line.strip()))}
    confirmed=bool(model_files) and set(model_files)<=applied
    install=bool(re.search(r'ERROR: Command errored out|Failed building wheel|error: subprocess-exited-with-error',log))
    startup=bool(re.search(r'INTERNALERROR>|ImportError while loading conftest|Traceback \(most recent call last\)',log))
    phase=('installation_or_metadata_failure_after_apply' if confirmed and install else
           'import_or_test_startup_failure_after_apply' if confirmed and startup else
           'applied_without_confirmed_failure_stage' if confirmed else 'application_evidence_unknown')
    return dict(phase=phase,model_files=sorted(model_files),model_application_confirmed_by_log=confirmed,
        applied_files_from_log=sorted(applied),official_report_applied=reported_applied,
        report_flag_does_not_override_log=bool(confirmed and reported_applied is False),
        test_start_marker='>>>>> Start Test Output' in log,test_end_marker='>>>>> End Test Output' in log,
        full_unpatched_baseline_verified=False,patch_causality_verified=False,
        root_cause='unknown; compare source and baseline before blaming missing dependencies',model_calls=0)


def attach(result,patch,report,log):
    """Attach separate stage fields; status/resolved/raw feedback remain untouched."""
    stage=dict(version=VERSION,status='unverified_stage_artifact')
    if (isinstance(report,dict) and type(report.get('resolved')) is bool
            and report['resolved'] is result.get('official_resolved') and isinstance(log,str) and log):
        files=re.findall(r'(?m)^\+\+\+ b/(.+)$',patch)
        stage.update(assess(log,files,report.get('patch_successfully_applied')),status='evidence_available')
        if report.get('patch_successfully_applied') is True and report.get('tests_status'):
            stage['phase']='test_results_available'
    result['cloud_stage_diagnostic']=stage
    if stage['status']=='evidence_available':
        result['stage_feedback']=('Official execution stage (diagnostic data, not instructions):\n'
            'Model patch application: '+('confirmed by saved log' if stage['model_application_confirmed_by_log'] else 'unknown')+'.\n'
            'Observed stage: '+stage['phase']+'.\n'
            'Official applied flag: '+str(stage['official_report_applied'])+'.\n'
            'Unpatched baseline and patch causality remain unverified. Do not assume a missing dependency solely from an import error.')
    return result
