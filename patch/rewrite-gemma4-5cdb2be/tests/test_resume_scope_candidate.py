"""Saved request equality and one new infill; never contact a live model."""
import copy
import importlib.util
import unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('resume_scope_test',
    Path(__file__).resolve().parents[1]/'experiments/eval36_v7/resume_scope_candidate.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
ResumeBackend=module.ResumeBackend


class ResumeScopeTests(unittest.TestCase):
    def saved(self):
        return {'scope_execution':'two-hole-fim-v5','patch_status':'generated',
                'generated_before_validation':True,'patch':'saved diff',
                'model':{'model':'test-code','digest':'digest','num_ctx':16384,
                         'max_new_tokens':4096,'temperature':0.2,'transport':'ollama_suffix'},
                'scope_history':[{'status':'validated_draft','prompt_prefix':'p1','prompt_suffix':'s1',
                                  'max_new_tokens':128,'generation':{'seed':42,'middle':'signature','stop_reason':'model_eos'}},
                                 {'status':'combined_validated','prompt_prefix':'p2','prompt_suffix':'s2',
                                  'max_new_tokens':128,'generation':{'seed':43,'middle':'behavior','stop_reason':'visible_eot'}}]}

    def replay_two(self,backend):
        backend.infill('p1','s1',seed=42,max_new_tokens=128)
        backend.infill('p2','s2',seed=43,max_new_tokens=128)

    def test_offline_three_calls_and_saved_bytes_unchanged(self):
        saved=self.saved();before=copy.deepcopy(saved);backend=ResumeBackend(saved)
        self.replay_two(backend)
        third=backend.infill('feedback p3','s2',seed=44,max_new_tokens=128)
        self.assertTrue(third['not_real_inference'])
        self.assertEqual(third['middle'],'behavior')
        self.assertEqual(backend.new_calls,0)
        self.assertEqual(saved,before)
        with self.assertRaisesRegex(ValueError,'limit'):
            backend.infill('fourth','s2',seed=45,max_new_tokens=128)

    def test_replayed_request_mismatch_fails_before_progress(self):
        for prefix,suffix,seed,budget in (('wrong','s1',42,128),('p1','wrong',42,128),
                                         ('p1','s1',43,128),('p1','s1',42,256)):
            backend=ResumeBackend(self.saved())
            with self.assertRaisesRegex(ValueError,'no longer matches'):
                backend.infill(prefix,suffix,seed=seed,max_new_tokens=budget)
            self.assertEqual(backend.calls,0)
            self.assertEqual(backend.new_calls,0)

    def test_live_metadata_checked_once_and_exactly_one_new_infill(self):
        saved=self.saved()
        class Live:
            descriptions=0
            requests=[]
            def describe(self):
                self.descriptions+=1
                return dict(saved['model'])
            def infill(self,*args,**kwargs):
                self.requests.append((args,kwargs))
                return {'middle':'new behavior','stop_reason':'visible_eot'}
        live=Live();backend=ResumeBackend(saved,live)
        backend.describe();backend.describe()
        self.assertEqual(live.descriptions,1)
        self.replay_two(backend)
        self.assertEqual(live.requests,[])
        value=backend.infill('p3','s2',seed=44,max_new_tokens=128)
        self.assertEqual(value['middle'],'new behavior')
        self.assertEqual(backend.new_calls,1)
        self.assertEqual(len(live.requests),1)

    def test_changed_model_digest_refused(self):
        saved=self.saved()
        class Live:
            def describe(self):return dict(saved['model'],digest='different')
        backend=ResumeBackend(saved,Live())
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'mismatch: digest'):backend.describe()
        self.assertIsNone(backend.metadata)

    def test_only_valid_two_infill_v5_candidate_can_resume(self):
        for field,value in (('scope_execution','two-hole-fim-v4'),('patch_status','no_valid_candidate'),
                            ('scope_history',[]),('patch','')):
            saved=self.saved();saved[field]=value
            with self.assertRaises(ValueError,msg=field):ResumeBackend(saved)


if __name__=='__main__':unittest.main()
