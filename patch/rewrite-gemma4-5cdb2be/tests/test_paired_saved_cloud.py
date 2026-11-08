import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from scripts.evaluate_paired_saved_cloud import inventory,summarize


class PairedCloudTests(unittest.TestCase):
    def fixture(self,root):
        (root/'run_config.json').write_text(json.dumps(dict(selected_ids=['a','b'],arms=['A3','A3_summary'])))
        rows=[]
        for key in ('a','b'):
            for arm in ('A3','A3_summary'):
                path=root/'tickets'/key/arm;path.mkdir(parents=True)
                (path/'selected.diff').write_text('--- a/a.py\n+++ b/a.py\n'+key+(arm if key=='b' else ''))
                rows.append(dict(ticket_id=key,arm=arm,selected_candidate=True))
        (root/'records.json').write_text(json.dumps(rows))

    def test_dedup_preserves_both_arm_lineages(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root);manifest=inventory(root)
            self.assertEqual(len(manifest['jobs']),3)
            self.assertEqual(len(manifest['selected']),4)
            shared=manifest['selected'][0]['job_key']
            result=summarize(manifest,{shared:dict(status='plausible',official_resolved=True)})
            for group in result['per_arm'].values():
                self.assertEqual(group['official_resolved_cases'],1)
                self.assertEqual(group['full_case_denominator'],2)

    def test_error_not_false_and_pending_not_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root);manifest=inventory(root)
            key=manifest['selected'][0]['job_key']
            result=summarize(manifest,{key:dict(status='error')})
            self.assertIsNone(result['per_arm']['A3']['confirmed_resolved_rate_full_denominator'])
            self.assertEqual(result['per_arm']['A3']['officially_tested_cases'],0)
            self.assertEqual(result['per_arm']['A3']['error_cases'],1)

    def test_missing_records_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root)
            records=json.loads((root/'records.json').read_text());records.pop()
            (root/'records.json').write_text(json.dumps(records))
            with self.assertRaises(ValueError):inventory(root)


if __name__=='__main__':unittest.main()
