import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'experiments/generalization_v1'),str(ROOT/'src'),str(ROOT)]
import generalization_readiness as readiness
from core import sha
from fim_patch.unified_repair import RepairPolicy


def cohort():
    return dict(candidate_ids=['other__project-1'],candidate_facts=[
        dict(ticket_id='other__project-1',repo='other/project',saved_output_exposure=False,
             prior_local_references=[])],skipped_original_history=[],unreadable_current_history=[])


def review(status='unknown', documented=False):
    return dict(schema_version=1,candidates=[dict(ticket_id='other__project-1',repo='other/project',
        checks={name:dict(status=status,reviewer='team member' if documented else '',
                         reviewed_at='2026-10-09' if documented else '',
                         evidence=['team-ledger.md:12'] if documented else [],note='')
                for name in readiness.DIMENSIONS})])


class ReadinessTests(unittest.TestCase):
    def test_unknown_and_tentative_no_remain_unknown(self):
        for document in (review(),review('not_used')):
            document['candidates'][0]['checks']['team_prompt_repair']['note']='好像沒有'
            result=readiness.assess_review(cohort(),document)
            self.assertEqual(result['counts'],{'unknown':1})
            self.assertEqual(result['final_test_eligible_cases'],0)

    def test_attestation_is_not_a_sealed_unseen_test_set(self):
        result=readiness.assess_review(cohort(),review('not_used',True))
        self.assertEqual(result['counts'],{'attested_not_used':1})
        self.assertFalse(result['records'][0]['final_test_eligible'])
        self.assertFalse(result['strict_unseen_verified'])

    def test_local_evidence_overrides_no_exposure_claim(self):
        for field in ('saved_output_exposure','prior_local_references'):
            data=cohort();data['candidate_facts'][0][field]=True if field=='saved_output_exposure' else ['plan']
            result=readiness.assess_review(data,review('not_used',True))
            self.assertEqual(result['counts'],{'development_exposed':1})

    def test_viewed_or_tuned_case_promoted_without_dropping_denominator(self):
        for status in ('viewed','used_for_tuning'):
            result=readiness.assess_review(cohort(),review(status,True))
            self.assertEqual(result['counts'],{'development_exposed':1})
            self.assertEqual(result['full_candidate_denominator'],1)

    def test_missing_duplicate_extra_case_and_repo_changes_refused(self):
        for mutation in ('missing','duplicate','extra','repo'):
            document=review()
            if mutation=='missing':document['candidates']=[]
            if mutation=='duplicate':document['candidates']*=2
            if mutation=='extra':document['candidates'][0]['ticket_id']='different'
            if mutation=='repo':document['candidates'][0]['repo']='wrong/repo'
            with self.assertRaises(ValueError):readiness.assess_review(cohort(),document)

    def test_missing_dimensions_or_invalid_state_or_boolean_evidence_refused(self):
        for mutation in ('dimension','state','evidence'):
            document=review();checks=document['candidates'][0]['checks']
            if mutation=='dimension':checks.pop(readiness.DIMENSIONS[0])
            if mutation=='state':checks[readiness.DIMENSIONS[0]]['status']='passed'
            if mutation=='evidence':checks[readiness.DIMENSIONS[0]]['evidence']=[True]
            with self.assertRaises(ValueError):readiness.assess_review(cohort(),document)

    def test_no_outcome_based_fields_in_review(self):
        document=review();document['candidates'][0]['official_resolved']=True
        with self.assertRaises(ValueError):readiness.assess_review(cohort(),document)

    def test_unreadable_history_does_not_disappear(self):
        data=cohort();data['skipped_original_history']=['unreadable']
        result=readiness.assess_review(data,review('not_used',True))
        self.assertTrue(result['skipped_history_unknown'])

    def test_invalid_review_date_refused(self):
        document=review('not_used',True)
        document['candidates'][0]['checks'][readiness.DIMENSIONS[0]]['reviewed_at']='yesterday'
        with self.assertRaises(ValueError):readiness.assess_review(cohort(),document)

    def test_freeze_rejects_artifact_environment_input_and_source_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);file=root/'data.json';file.write_text('{}')
            frozen=dict(version=readiness.VERSION,implementation_sha256={},input_sha256={},
                source_sha256={},prepared_artifact_sha256={'data.json':sha(file.read_bytes())},
                environment=readiness.environment())
            def save(): (root/'freeze.json').write_text(json.dumps(frozen))
            save();self.assertEqual(readiness.verify(root)['status'],'verified')
            file.write_text('changed')
            with self.assertRaises(ValueError):readiness.verify(root)
            file.write_text('{}');frozen['environment']['python']='different';save()
            with self.assertRaises(ValueError):readiness.verify(root)
            frozen['environment']=readiness.environment()
            for group in ('input_sha256','source_sha256'):
                frozen[group]={str(file):sha(b'previous')};save()
                with self.assertRaises(ValueError):readiness.verify(root)
                frozen[group]={}

    def test_outside_artifact_path_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):readiness.inside(Path(temp),'../other.json')

    def test_method_preserves_budget_and_shared_arm_settings(self):
        manifest=dict(model=dict(model='codellama:13b-code',expected_digest='a'*64),
            policy=RepairPolicy(max_generation_calls=2),tokenizer_json='tokenizer.json',model_show_json='show.json')
        method=readiness.method_config(manifest,'source_summary')
        self.assertEqual(method['policy']['max_generation_calls'],2)
        settings=method['arm_settings'];control=dict(settings['A3']);treatment=dict(settings['A3_summary'])
        control.pop('source_summary');treatment.pop('source_summary')
        self.assertEqual(control,treatment)
        manifest['policy']=RepairPolicy(domain_adapters=('tolerance',))
        with self.assertRaises(ValueError):readiness.method_config(manifest,'source_summary')

    def test_candidate_ledger_tamper_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            artifacts={'candidate_public_tickets.jsonl':'{"ticket_id":"other__project-1","repo":"other/project"}\n',
                       'candidate_checklist.jsonl':'{"ticket_id":"other__project-1"}\n',
                       'summary.json':'{}','exposure_ledger.json':'{}'}
            for name,text in artifacts.items():(root/name).write_text(text)
            (root/'freeze.json').write_text(json.dumps(dict(prepared_artifact_sha256={
                name:sha((root/name).read_bytes()) for name in artifacts})))
            self.assertEqual(len(readiness.check_candidate_ledger(root)[0]),1)
            (root/'candidate_checklist.jsonl').write_text('[]')
            with self.assertRaises(ValueError):readiness.check_candidate_ledger(root)

    def test_prepare_verify_and_review_use_no_backend_or_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);candidates=root/'candidates';candidates.mkdir()
            artifacts={
                'candidate_public_tickets.jsonl':'{"ticket_id":"other__project-1","repo":"other/project"}\n',
                'candidate_checklist.jsonl':'{"ticket_id":"other__project-1"}\n',
                'summary.json':'{}','exposure_ledger.json':'{"references":{},"skipped":[]}'}
            for name,text in artifacts.items():(candidates/name).write_text(text)
            (candidates/'freeze.json').write_text(json.dumps(dict(prepared_artifact_sha256={
                name:sha((candidates/name).read_bytes()) for name in artifacts})))
            manifest_path=root/'manifest.json';manifest_path.write_text('{}')
            tickets=root/'tickets.jsonl';tickets.write_text(json.dumps(dict(ticket_id='dev__project-2',repo='dev/project',base_commit='a'*40))+'\n')
            locations=root/'locations.jsonl';locations.write_text('')
            tokenizer=root/'tokenizer.json';tokenizer.write_text('{}')
            show=root/'show.json';show.write_text('{}')
            manifest=dict(model=dict(model='codellama:13b-code',expected_digest='a'*64),policy=RepairPolicy(),
                tickets=str(tickets),localizations=str(locations),tokenizer_json=str(tokenizer),
                model_show_json=str(show),snapshot_cache_dir=str(root/'snapshots'))
            output=root/'bundle'
            with patch.object(readiness,'load_manifest',return_value=manifest), \
                    patch.object(readiness,'history_exposure',return_value=(set(),[],[])), \
                    patch.object(readiness,'verified_counter') as counter, \
                    patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')):
                counter.return_value.sha256='test-only-verification'
                result=readiness.prepare(output,manifest_path,candidates)
                self.assertEqual(result['model_calls'],0)
                self.assertEqual(result['development_source_pending'][0]['reason'],'missing_source_fingerprints')
                with self.assertRaises(ValueError):readiness.prepare(output,manifest_path,candidates)
            self.assertEqual(readiness.verify(output)['status'],'verified')
            original=(output/'exposure_review_template.json').read_bytes()
            form=root/'review.json';form.write_bytes(original)
            result=readiness.review_bundle(output,form,root/'result.json')
            self.assertEqual(result['counts'],{'unknown':1})
            self.assertEqual((output/'exposure_review_template.json').read_bytes(),original)
            with self.assertRaises(ValueError):readiness.review_bundle(output,form,output/'review.json')
            before=manifest_path.read_bytes();manifest_path.write_text('changed')
            with self.assertRaises(ValueError):readiness.verify(output)
            manifest_path.write_bytes(before)
            impl=readiness.load(output/'freeze.json')
            name=next(iter(impl['implementation_sha256']))
            impl['implementation_sha256'][name]='tampered'
            (output/'freeze.json').write_text(json.dumps(impl))
            with self.assertRaises(ValueError):readiness.verify(output)


if __name__=='__main__':unittest.main()
