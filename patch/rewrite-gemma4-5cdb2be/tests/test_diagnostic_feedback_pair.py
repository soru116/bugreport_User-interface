import sys
from pathlib import Path
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
from diagnostic_feedback_pair import FrozenRetry, saved_validator
from test_feedback_retry_pilot import Real, saved
from core import sha


class PairTests(unittest.TestCase):
    def test_changed_retry_refuses_before_model_dispatch(self):
        real=Real();b=FrozenRetry(real,dict(model='m',expected_digest='d'),saved(),False,
            frozen_request=dict(prefix='expected',suffix='s',seed=43,output_limit=190))
        b.infill('p','s',seed=42,max_new_tokens=190)
        with self.assertRaisesRegex(ValueError,'changed'):b.infill('changed','s',seed=43,max_new_tokens=190)
        self.assertEqual(real.calls,[])

    def test_one_generation_then_cap_and_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            real=Real();journal=Path(temp)/'dispatch.json'
            b=FrozenRetry(real,dict(model='m',expected_digest='d'),saved(),False,
                frozen_request=dict(prefix='retry',suffix='s',seed=43,output_limit=190),journal=journal)
            b.infill('p','s',seed=42,max_new_tokens=190)
            b.infill('retry','s',seed=43,max_new_tokens=190)
            self.assertEqual(real.calls,['generate']);self.assertTrue(journal.exists())
            with self.assertRaisesRegex(ValueError,'cap exceeded'):b.infill('retry','s',seed=43,max_new_tokens=190)
            self.assertEqual(real.calls,['generate'])

    def test_plan_mode_never_requests_provider(self):
        real=Real();b=FrozenRetry(real,dict(model='m',expected_digest='d'),saved(),True)
        b.infill('p','s',seed=42,max_new_tokens=190)
        with self.assertRaisesRegex(ValueError,'offline_retry_capture'):b.infill('retry','s',seed=43,max_new_tokens=190)
        self.assertEqual(real.calls,[])

    def test_novel_patch_has_no_official_success(self):
        c=saved();c['validation']['cloud_request']['patch_sha256']=sha(b'first')
        check=saved_validator(c)
        self.assertFalse(check('first',None)['official_resolved'])
        self.assertIsNone(check('new',None)['official_resolved'])
        self.assertEqual(check('new',None)['status'],'not_run')


if __name__=='__main__':unittest.main()
