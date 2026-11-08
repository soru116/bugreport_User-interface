"""Optional local artifact integration; all provider operations are mocked."""
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

SAVED=ROOT/'runs/eval1-codellama13b-cross14995-body-v1/tickets/astropy__astropy-14995/result.json'
ASSETS=ROOT/'runs/codellama13b-offline-diagnostics'


@unittest.skipUnless(SAVED.exists() and (ASSETS/'tokenizer.json').exists(),'local saved output/tokenizer absent')
class SavedSafeRetryTests(unittest.TestCase):
    def module(self):
        spec=importlib.util.spec_from_file_location('saved_safe_retry',ROOT/'experiments/eval36_v7/verify_safe_body_feedback.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_explicit_live_switch_replays_first_and_sends_only_second(self):
        module=self.module();saved=json.loads(SAVED.read_text(encoding='utf8'))
        counter=VerifiedCodeLlamaCounter(ASSETS/'tokenizer.json',json.loads((ASSETS/'model_show.json').read_text(encoding='utf8')))
        def metadata(backend):
            backend._token_counter=counter
            return dict(saved['model'])
        reply=dict(response=saved['candidates'][0]['backend_response']['response'],done=True,done_reason='length')
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'result'
            with patch.object(sys,'argv',['verify','--generate-retry','--output',str(output)]), \
                 patch.object(OllamaFIM,'describe',metadata),patch.object(OllamaFIM,'post',return_value=reply) as post, \
                 patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden in test')),contextlib.redirect_stdout(io.StringIO()):
                module.main()
            self.assertEqual(post.call_count,1)
            self.assertEqual(post.call_args.args[0],'generate')
            self.assertEqual(post.call_args.args[1]['options']['seed'],43)
            self.assertEqual(post.call_args.args[1]['options']['num_predict'],256)
            report=json.loads((output/'report.json').read_text(encoding='utf8'))
            self.assertEqual(report['controlled_saved_replays'],1)
            self.assertEqual(report['unique_static_candidates'],1)
            self.assertEqual(report['accepted_candidates'],0)

    def test_mismatched_model_never_dispatches_retry(self):
        module=self.module();saved=json.loads(SAVED.read_text(encoding='utf8'))
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'result'
            with patch.object(sys,'argv',['verify','--generate-retry','--output',str(output)]), \
                 patch.object(OllamaFIM,'describe',return_value=dict(saved['model'],digest='different')), \
                 patch.object(OllamaFIM,'post') as post:
                with self.assertRaisesRegex(ValueError,'model/config mismatch'):module.main()
                post.assert_not_called()
            self.assertFalse(output.exists())


if __name__=='__main__':unittest.main()
