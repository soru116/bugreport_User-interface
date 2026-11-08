from datetime import date
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'experiments/generalization_v1'))
from review_next_independent import assess, verify_freeze, DIMENSIONS


class ExposureReviewTests(unittest.TestCase):
    def fixture(self, clear=False):
        row = dict(ticket_id='repo__name-1', repo='repo/name', checks={})
        for dimension in DIMENSIONS:
            status = ('not_reserved' if dimension == 'reserved_holdout_membership' else 'not_used') if clear else 'unknown'
            row['checks'][dimension] = dict(status=status, reviewer='owner' if clear else '',
                reviewed_at='2026-10-10' if clear else '', evidence=['meeting-record#1'] if clear else [])
        return dict(schema_version=1, candidates=[row])

    def test_unknown_is_not_clear(self):
        review = self.fixture()
        result = assess(review, review)
        self.assertEqual(result['eligible_cases'], 0)
        self.assertEqual(len(result['cases'][0]['blockers']), 3)

    def test_external_unknown_disclosed_not_falsely_certified(self):
        template = self.fixture(); review = self.fixture(True)
        review['candidates'][0]['checks']['known_external_project_use']['status'] = 'unknown'
        result = assess(template, review, date(2026, 10, 10))
        self.assertEqual(result['eligible_cases'], 1)
        self.assertFalse(result['strict_unseen_verified'])
        self.assertFalse(result['inference_allowed'])

    def test_exposed_or_reserved_blocks(self):
        for dimension, status in [('team_prompt_repair', 'viewed'),
                                  ('prior_fault_localization', 'used_for_tuning'),
                                  ('reserved_holdout_membership', 'reserved')]:
            review = self.fixture(True)
            review['candidates'][0]['checks'][dimension]['status'] = status
            self.assertEqual(assess(self.fixture(), review)['eligible_cases'], 0)

    def test_confirmation_requires_attribution_and_evidence(self):
        for field, value in [('reviewer', ''), ('evidence', []), ('evidence', [''])]:
            review = self.fixture(True)
            review['candidates'][0]['checks']['team_prompt_repair'][field] = value
            with self.assertRaises(ValueError): assess(self.fixture(), review)

    def test_future_date_rejected(self):
        review = self.fixture(True)
        with self.assertRaises(ValueError): assess(self.fixture(), review, date(2026, 10, 9))

    def test_missing_duplicate_and_replaced_cases_rejected(self):
        for kind in ('missing', 'duplicate', 'replacement', 'repo'):
            review = self.fixture()
            if kind == 'missing': review['candidates'] = []
            if kind == 'duplicate': review['candidates'] *= 2
            if kind == 'replacement': review['candidates'][0]['ticket_id'] = 'other-2'
            if kind == 'repo': review['candidates'][0]['repo'] = 'other/repo'
            with self.assertRaises(ValueError): assess(self.fixture(), review)

    def test_invalid_status_and_missing_dimension_rejected(self):
        review = self.fixture()
        review['candidates'][0]['checks']['team_prompt_repair']['status'] = 'true'
        with self.assertRaises(ValueError): assess(self.fixture(), review)
        review = self.fixture()
        del review['candidates'][0]['checks']['known_external_project_use']
        with self.assertRaises(ValueError): assess(self.fixture(), review)

    def test_frozen_bytes_and_path_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); cohort = root / 'cohort'; cohort.mkdir()
            file = root / 'method.py'; file.write_bytes(b'original')
            freeze = dict(implementation_sha256={'method.py': hashlib.sha256(b'original').hexdigest()},
                          reference_sha256={}, prepared_artifact_sha256={})
            def save(): (cohort / 'freeze.json').write_text(json.dumps(freeze))
            save(); verify_freeze(cohort, root)
            file.write_bytes(b'changed')
            with self.assertRaises(ValueError): verify_freeze(cohort, root)
            freeze['implementation_sha256'] = {'../outside.py': 'irrelevant'}
            save()
            with self.assertRaises(ValueError): verify_freeze(cohort, root)


if __name__ == '__main__': unittest.main()
