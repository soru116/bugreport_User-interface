"""Offline paired measurements and no-leakage cohort/denominator contracts."""
import copy
import importlib.util
from pathlib import Path
import sys
import json
import tempfile
import contextlib
import io
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'experiments/generalization_v1'),str(ROOT/'src'),str(ROOT)]
from core import public_ticket,select_stratified,summarize_cases,evaluate_parser,saved_middle,sha,check_freeze,dump
from tests import test_fim
from fim_patch.generator import function_span
from fim_patch.none_contract import VM,CALL,Unknown
import ast

class ProtocolTests(unittest.TestCase):
    def test_sampling_does_not_read_gold_or_known_success(self):
        values=[dict(ticket_id='repo-'+str(i),repo='org/repo',title='Parser error '+str(i),description='Handle empty string',base_commit='a'*40) for i in range(8)]
        altered=[dict(r,patch='ANSWER',test_patch='ASSERT ANSWER',resolved=True,hints_text='Use easy solution') for r in values]
        self.assertEqual(select_stratified(values,{'repo-1'}),select_stratified(altered,{'repo-1'}))
        self.assertNotIn('repo-1',[r['ticket_id'] for r in select_stratified(values,{'repo-1'})])
        self.assertFalse(any(k in public_ticket(altered[0]) for k in ('patch','test_patch','hints_text','resolved')))

    def test_sampling_preserves_every_repository(self):
        values=[dict(ticket_id=repo+'-'+str(i),repo=repo,description='Ambiguous behavior') for repo in ('repoA','repoB','repoC') for i in range(3)]
        selected=select_stratified(values,set())
        self.assertEqual(len(selected),6);self.assertEqual({r['repo'] for r in selected},{'repoA','repoB','repoC'})

    def test_missing_error_and_unsupported_remain_in_denominator(self):
        report=summarize_cases([dict(ticket_id='a',generated_before_validation=True,generation_attempts=1),dict(ticket_id='b',error='environment',attempted=True)],['a','b','c','d'])
        self.assertEqual(report['static_generation_rate_full_denominator'],0.25)
        self.assertEqual(report['missing_cases'],2)
        self.assertIsNone(report['official_repair_rate_full_denominator'])
        self.assertEqual(report['official_untested_cases'],4)

    def test_pending_does_not_claim_zero_model_success_rate(self):
        report=summarize_cases([dict(ticket_id='a',generation_attempts=0)],['a'])
        self.assertIsNone(report['static_generation_rate_full_denominator'])

    def test_diagnostic_false_rates_need_labels(self):
        report=summarize_cases([dict(ticket_id='a',diagnostic_detected=True,diagnostic_expected=False),dict(ticket_id='b',diagnostic_detected=False,diagnostic_expected=True)],['a','b'])
        self.assertEqual((report['diagnostic_false_positives'],report['diagnostic_false_negatives']),(1,1))
        self.assertIsNone(summarize_cases([],['a'])['diagnostic_false_positives'])

    def test_duplicate_and_unregistered_results_refused(self):
        for records in ([dict(ticket_id='a'),dict(ticket_id='a')],[dict(ticket_id='other')]):
            with self.assertRaises(ValueError):summarize_cases(records,['a'])

    def test_identical_backend_decoding_before_both_arms(self):
        c={'backend_response':{'response':'    return x\n<EOT>echo','done':True}}
        text,stop,raw=saved_middle(c)
        self.assertEqual((text,stop),('    return x\n','visible_eot'));self.assertIn('echo',raw)
        with self.assertRaises(ValueError):saved_middle({'backend_response':{'response':'x','done':False}})

    def test_unknown_callback_is_not_diagnostic_or_truth(self):
        with self.assertRaises(Unknown):VM({'cb':CALL}).value(ast.parse('cb(1,2)',mode='eval').body)

    def test_freeze_refuses_modified_prepared_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);dump(root/'data.json',{'seed':43})
            dump(root/'freeze.json',dict(implementation_sha256={},prepared_artifact_sha256={'data.json':sha((root/'data.json').read_bytes())}))
            check_freeze(root)
            dump(root/'data.json',{'seed':44})
            with self.assertRaisesRegex(ValueError,'prepared artifact changed'):check_freeze(root)

class PairedParserTests(unittest.TestCase):
    setUp=test_fim.PatchTests.setUp
    def measure(self,text,expected):
        span=function_span(self.raw,self.loc['stage3_ranked_symbols'][0])
        with patch('urllib.request.urlopen',side_effect=AssertionError('network')):
            results=[evaluate_parser(self.raw,span,text,'length','', 'app.py',self.repo,safe=safe) for safe in (False,True)]
        self.assertEqual(results[0]['raw_sha256'],results[1]['raw_sha256'])
        self.assertEqual(results[1]['accepted'],expected,results)
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
        return results

    def test_complete_target_truncated_sibling_recovered(self):
        results=self.measure('    return "" if x is None else x.strip()\n\ndef sibling():\n    return (\n',True)
        self.assertFalse(results[0]['accepted'])

    def test_truncated_target_is_not_completed(self):
        self.measure('    if True:\n\ndef sibling():\n    pass\n',False)

    def test_noop_and_doc_padding_not_counted_as_repair(self):
        self.measure('    """New docs"""\n    return x.strip()\n\ndef sibling():\n    pass\n',False)

    def test_fake_boundary_in_string_not_extracted(self):
        self.measure('    text = """\ndef fake():\n    pass\n"""\n    return text\n',False)

    def test_unknown_name_remains_rejected_after_extraction(self):
        self.measure('    return imaginary(x)\n\ndef sibling():\n    pass\n',False)

    def test_seed_pair_generation_is_exactly_two_mocked_requests(self):
        import run_seed_pair
        from fim_patch.backend import OllamaFIM
        from fim_patch.generator import git
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);dump(root/'first.json',{'fixture':True})
            expected=dict(model='mock',digest='pin',num_ctx=4096,max_new_tokens=512,temperature=0.2,transport='ollama_suffix',tokenizer_sha256=None)
            base=dict(model='mock',prompt='def f(x):\n',suffix='',stream=False,
                      options=dict(num_ctx=4096,num_predict=128,temperature=0.2,top_p=0.95,seed=43))
            span=function_span(self.raw,self.loc['stage3_ranked_symbols'][0])
            pair=dict(status='prepared_not_dispatched',max_new_generate_dispatches=2,
                arms=dict(no_feedback=base,with_feedback=dict(base,prompt='# check None\n'+base['prompt'])),
                saved_first_path=str(root/'first.json'),saved_first_sha256=sha((root/'first.json').read_bytes()),
                snapshot_root=str(self.repo),source_file='app.py',source_sha256=sha(self.raw),
                base_commit=git(self.repo,'rev-parse','HEAD').decode().strip(),model_expected=expected,
                input_tokens={'no_feedback':10,'with_feedback':13},tokenizer_json=None,span=span)
            dump(root/'pair.json',pair);dump(root/'freeze.json',dict(implementation_sha256={},prepared_artifact_sha256={'pair.json':sha((root/'pair.json').read_bytes())}))
            output=root/'out'
            with patch.object(sys,'argv',['pair','--pair',str(root/'pair.json'),'--output',str(output),'--generate']), \
                 patch.object(OllamaFIM,'describe',return_value=expected), \
                 patch.object(OllamaFIM,'infill',return_value={'middle':'    return "" if x is None else x.strip()\n','stop_reason':'visible_eot'}) as provider, \
                 patch('urllib.request.urlopen',side_effect=AssertionError('network')),contextlib.redirect_stdout(io.StringIO()):
                run_seed_pair.main()
            self.assertEqual(provider.call_count,2)
            self.assertEqual([c.kwargs['seed'] for c in provider.call_args_list],[43,43])
            self.assertEqual(json.loads((output/'summary.json').read_text(encoding='utf8'))['new_generate_dispatches'],2)

if __name__=='__main__':unittest.main()
