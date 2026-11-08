"""Saved fixture integration; all live provider calls are mocked."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from fim_patch.backend import OllamaFIM
from fim_patch.token_budget import VerifiedCodeLlamaCounter
SAVED=ROOT/'runs/eval1-codellama13b-cross13638-body-v1/tickets/astropy__astropy-13638/result.json'
ASSETS=ROOT/'runs/codellama13b-offline-diagnostics'


@unittest.skipUnless(SAVED.exists() and (ASSETS/'tokenizer.json').exists(),'local saved fixture absent')
class SavedContractRetryTests(unittest.TestCase):
    def module(self):
        spec=importlib.util.spec_from_file_location('contract_saved',ROOT/'experiments/eval36_v7/verify_operation_contract_integration.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_explicit_live_switch_calls_only_second_once(self):
        module=self.module();saved=json.loads(SAVED.read_text(encoding='utf8'))
        counter=VerifiedCodeLlamaCounter(ASSETS/'tokenizer.json',json.loads((ASSETS/'model_show.json').read_text(encoding='utf8')))
        def describe(backend):backend._token_counter=counter;return saved['model']
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'out'
            with patch.object(sys,'argv',['verify','--contract-retry','--generate-retry','--output',str(out)]), \
                 patch.object(OllamaFIM,'describe',describe),patch.object(OllamaFIM,'post',return_value=saved['candidates'][0]['backend_response']) as provider, \
                 patch('urllib.request.urlopen',side_effect=AssertionError('network')),contextlib.redirect_stdout(io.StringIO()):
                module.main()
            self.assertEqual(provider.call_count,1)
            self.assertEqual(provider.call_args.args[1]['options']['seed'],43)
            self.assertEqual(provider.call_args.args[1]['options']['num_predict'],256)
            report=json.loads((out/'report.json').read_text())
            self.assertEqual(report['controlled_saved_replays'],1)
            self.assertEqual(report['model_calls'],1) # mock dispatch, not real inference
            self.assertTrue(report['selected_patch_and_verdict_unchanged'])

    def test_metadata_mismatch_never_dispatches_case_generation(self):
        module=self.module();saved=json.loads(SAVED.read_text(encoding='utf8'))
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(sys,'argv',['verify','--contract-retry','--generate-retry','--output',str(Path(temp)/'out')]), \
                 patch.object(OllamaFIM,'describe',return_value=dict(saved['model'],digest='wrong')), \
                 patch.object(OllamaFIM,'post') as provider,contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError,'model/config mismatch'):module.main()
                provider.assert_not_called()


if __name__=='__main__':unittest.main()
