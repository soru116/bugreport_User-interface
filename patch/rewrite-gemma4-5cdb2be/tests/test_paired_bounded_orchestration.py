from contextlib import ExitStack
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
import run_paired_bounded as module
from paired_dispatch_store import atomic_json


class OrchestrationTests(unittest.TestCase):
    def exercise(self,root,interrupt=False):
        sends=[];state={'interrupted':False};show=root/'show.json';show.write_text('{}')
        keys=['a','b'];arms=['A3','A3_summary']
        model=dict(model='fake-test-only',expected_digest='digest')
        policy=SimpleNamespace(max_generation_calls=3,seed=42)
        manifest=dict(model=model,policy=policy,tokenizer_json=str(root/'unused-tokenizer'),model_show_json=str(show),snapshot_cache_dir=str(root))
        base=dict(model=model,policy={},selected_ids=keys,source_sha256={})
        config=dict(planned=dict(**base,arms=arms),manifest=str(root/'unused-manifest'),
            per_unit_request_cap=3,total_request_cap=12,probe_cap=1,full_denominator=2,seed=42,pinned_ollama_version='test-version')
        tickets=[dict(ticket_id=k,repo='synthetic/repo',base_commit='0'*40) for k in keys]
        class FakeBackend:
            def __init__(self,**kwargs):self.model=kwargs['model'];self.url='http://test.invalid';self.timeout=1
            def _tags(self):return dict(models=[dict(name=self.model,digest='digest')])
            def describe(self):
                self.post('generate',dict(prompt='probe',suffix='end',options={'num_predict':1}))
                return dict(digest='digest')
            def post(self,endpoint,payload):
                sends.append(payload)
                if interrupt and payload['prompt']=='a:A3' and not state['interrupted']:
                    state['interrupted']=True;raise KeyboardInterrupt()
                return dict(response='synthetic smoke only',done=True,done_reason='stop',prompt_eval_count=1,eval_count=1)
        class FakeEngine:
            def __init__(self,backend,arm,policy):self.backend=backend;self.arm=arm
            def inner(self,*args,**kwargs):
                def call(ticket,location,repo):
                    self.backend.post('generate',dict(prompt=ticket['ticket_id']+':'+self.arm,suffix='end',options={'seed':42}))
                    return dict(patch_status='no_valid_candidate',patch='',generated_before_validation=False)
                return SimpleNamespace(generate=call)
            def generate(self,*args):return self.inner(self.backend,scope='body').generate(*args)
        def write(directory,ticket,location,result):atomic_json(directory/'result.json',result)
        with ExitStack() as stack:
            for name,value in [('verify',lambda p:config),('config_for',lambda p:(manifest,tickets,{k:{} for k in keys},{},base)),
                ('OllamaFIM',FakeBackend),('PairedRepairGenerator',FakeEngine),('verified_counter',lambda *a:None),
                ('snapshot_path',lambda *a:root),('write_run',write),('urlopen',lambda *a,**k:BytesIO(json.dumps({'version':'test-version'}).encode()))]:
                stack.enter_context(patch.object(module,name,value))
            output=root/'generated'
            if interrupt:
                with self.assertRaises(KeyboardInterrupt):module.generate(root/'freeze',output,True)
            module.generate(root/'freeze',output,True)
            if interrupt:
                paused=json.loads((output/'summary.json').read_text())
                self.assertEqual(paused['recorded_arm_cases'],1)
                self.assertIn('stop_reason',paused)
                module.generate(root/'freeze',output,True)
            before=len(sends);module.generate(root/'freeze',output,True)
            self.assertEqual(len(sends),before)
            summary=json.loads((output/'summary.json').read_text())
            self.assertEqual(summary['recorded_arm_cases'],4)
            self.assertEqual(summary['repair_dispatches'],4)
            self.assertEqual(summary['capability_probes'],1)
            return sends,summary

    def test_complete_run_resume_skips_all_units_and_network(self):
        with tempfile.TemporaryDirectory() as temp:
            sends,summary=self.exercise(Path(temp))
            self.assertEqual(len(sends),5)
            self.assertEqual(summary['uncertain_requests'],0)

    def test_mid_request_interrupt_is_not_resent_and_other_units_finish(self):
        with tempfile.TemporaryDirectory() as temp:
            sends,summary=self.exercise(Path(temp),interrupt=True)
            self.assertEqual(sum(p['prompt']=='a:A3' for p in sends),1)
            self.assertEqual(summary['uncertain_requests'],1)


if __name__=='__main__':unittest.main()
