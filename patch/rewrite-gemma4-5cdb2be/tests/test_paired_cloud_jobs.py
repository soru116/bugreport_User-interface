from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
from evaluate_paired_saved_cloud import group_patches,summarize


def row(arm='A3',instance='case',patch='hash',base='base'):
    return dict(arm=arm,instance_id=instance,patch_sha256=patch,repo='example/repo',base_commit=base)


class PairedCloudJobsTests(unittest.TestCase):
    def test_identical_case_patch_shared_between_arms(self):
        jobs=group_patches([row(),row('A3_summary')])
        self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['arms'],['A3','A3_summary'])

    def test_different_patches_of_same_case_not_merged(self):
        self.assertEqual(len(group_patches([row(),row('A3_summary',patch='other')])),2)

    def test_same_patch_text_in_different_cases_not_merged(self):
        self.assertEqual(len(group_patches([row(),row('A3_summary',instance='other')])),2)

    def test_conflicting_identity_or_duplicate_arm_rejected(self):
        with self.assertRaises(ValueError):group_patches([row(),row('A3_summary',base='other')])
        with self.assertRaises(ValueError):group_patches([row(),row()])

    def plan(self):
        return dict(full_denominator=24,arms=['A3','A3_summary'],jobs=[dict(job_id='01',arms=['A3','A3_summary'])])

    def test_shared_success_credited_to_both_full_denominators(self):
        report=summarize(self.plan(),[dict(job_id='01',status='plausible',official_resolved=True)])
        self.assertEqual(report['formal_unique_verdicts'],1)
        for arm in report['arms'].values():
            self.assertEqual(arm['resolved_cases'],1);self.assertEqual(arm['resolved_rate_full_cohort'],1/24)

    def test_error_pending_and_false_verdict_are_distinct(self):
        pending=summarize(self.plan(),[])
        error=summarize(self.plan(),[dict(job_id='01',status='error',official_resolved=None)])
        failed=summarize(self.plan(),[dict(job_id='01',status='failed',official_resolved=False)])
        self.assertEqual(pending['arms']['A3']['pending_jobs'],1)
        self.assertEqual(error['formal_unique_verdicts'],0)
        self.assertEqual(failed['formal_unique_verdicts'],1)
        self.assertEqual(error['arms']['A3']['errors'],1)

    def test_duplicate_or_unregistered_job_results_rejected(self):
        result=dict(job_id='01',status='failed',official_resolved=False)
        with self.assertRaises(ValueError):summarize(self.plan(),[result,result])
        with self.assertRaises(ValueError):summarize(self.plan(),[dict(result,job_id='other')])


if __name__=='__main__':unittest.main()
