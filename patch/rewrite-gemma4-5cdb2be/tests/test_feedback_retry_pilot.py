import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
from feedback_retry_pilot import ReplayThenRetry,validator,canonical_config
from fim_patch.shared_budget import SharedBudgetBackend
from core import sha


class Counter:
    def count(self,p,s):return len(p)+len(s)+4


class Real:
    _token_counter=Counter();num_ctx=10000;max_new_tokens=190
    def __init__(self):self.calls=[]
    def post(self,endpoint,payload):self.calls.append(endpoint)
    def infill(self,p,s,**kwargs):self.post('generate',{});return dict(middle='new')


def saved():
    return dict(prompt='p',suffix='s',seed=42,token_budget=190,middle='trimmed',untrimmed_middle='original complete reply',
                raw_response='original complete reply',stop_reason='length',metrics={},
                validation=dict(status='failed',official_resolved=False,feedback='old',cloud_request=dict(patch_sha256=sha(b'first'))))


class PilotTests(unittest.TestCase):
    def test_frozen_json_roundtrip_does_not_reject_tuple_policy(self):
        import json
        config=dict(policy=dict(domain_adapters=(),max_generation_calls=2),arms=['old_feedback','full_feedback'])
        frozen=json.loads(json.dumps(config))
        self.assertEqual(canonical_config(config),frozen)
        changed=dict(config,policy=dict(domain_adapters=(),max_generation_calls=3))
        self.assertNotEqual(canonical_config(changed),frozen)

    def test_first_replay_and_one_new_call_keep_dispatch_accounting(self):
        real=Real();backend=ReplayThenRetry(real,dict(model='model',expected_digest='digest'),saved(),False)
        shared=SharedBudgetBackend(backend,2)
        reply=shared.infill('p','s',seed=42,max_new_tokens=190)
        self.assertEqual(reply['middle'],'original complete reply');self.assertEqual(real.calls,[])
        shared.infill('changed prompt','s',seed=43,max_new_tokens=190)
        self.assertEqual(real.calls,['generate']);self.assertEqual(shared.report()['backend_generate_dispatches'],1)
        self.assertEqual(backend.new_infill_calls,1)

    def test_preview_does_not_dispatch(self):
        real=Real();backend=ReplayThenRetry(real,dict(model='model',expected_digest='digest'),saved(),True)
        backend.infill('p','s',seed=42,max_new_tokens=190)
        with self.assertRaisesRegex(ValueError,'offline_retry_capture'):
            backend.infill('new','s',seed=43,max_new_tokens=190)
        self.assertEqual(real.calls,[])

    def test_changed_saved_prompt_is_rejected(self):
        backend=ReplayThenRetry(Real(),dict(model='model',expected_digest='digest'),saved(),True)
        with self.assertRaisesRegex(ValueError,'differs'):backend.infill('changed','s',seed=42,max_new_tokens=190)

    def test_no_invented_official_pass_for_new_retry(self):
        check=validator(saved(),dict(old_feedback='old error',new_feedback="Failed: DID NOT RAISE <class 'TypeError'>"),'full_feedback')
        self.assertIn('DID NOT RAISE',check('first',None)['feedback'])
        novel=check('new patch',None);self.assertEqual(novel['status'],'not_run');self.assertIsNone(novel['official_resolved'])


if __name__=='__main__':unittest.main()
