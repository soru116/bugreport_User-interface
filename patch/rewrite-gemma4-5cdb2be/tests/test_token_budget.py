import json
import hashlib
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))

from fim_patch.backend import OllamaFIM
from fim_patch.token_budget import QWEN_FIM_TEMPLATE, VerifiedQwenCounter

try:
    from tokenizers import Tokenizer, models
except ImportError:
    Tokenizer = None


class BudgetGuardTests(unittest.TestCase):
    def backend(self, count):
        backend = OllamaFIM(num_ctx=100, max_new_tokens=20, tokenizer_json='local.json')
        backend._token_counter = SimpleNamespace(count=lambda prefix, suffix: count)
        return backend

    def test_byte_oversize_can_fit_verified_tokens_without_changing_fim(self):
        backend = self.backend(72)
        payload = backend._payload('x'*1000, 'y', seed=42, budget=20)
        self.assertEqual(payload['prompt'], 'x'*1000)
        self.assertEqual(payload['suffix'], 'y')
        self.assertTrue(backend._last_budget['fits'])

    def test_tokens_output_and_margin_exceeding_limit_are_rejected(self):
        backend = self.backend(73)
        with self.assertRaisesRegex(ValueError, 'context_budget_exceeded'):
            backend._payload('short', 'suffix', seed=42, budget=20)
        self.assertFalse(backend._last_budget['fits'])

    def test_cannot_enable_unverified_counter(self):
        backend = OllamaFIM(tokenizer_json='local.json')
        with self.assertRaisesRegex(ValueError, 'verification_required'):
            backend._payload('short', '', seed=42, budget=20)

    def test_raw_transport_is_not_silently_measured_as_qwen_fim(self):
        with self.assertRaisesRegex(ValueError, 'requires ollama_suffix'):
            OllamaFIM(transport='raw_psm', tokenizer_json='local.json')


@unittest.skipIf(Tokenizer is None, 'optional tokenizers package unavailable')
class CounterVerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'tokenizer.json'
        tokenizer = Tokenizer(models.BPE(vocab={'a':0,'b':1,'ab':2}, merges=[('a','b')]))
        tokenizer.save(str(self.path))
        pinned = patch('fim_patch.token_budget.QWEN_TOKENIZER_SHA256',
                       hashlib.sha256(self.path.read_bytes()).hexdigest())
        pinned.start()
        self.addCleanup(pinned.stop)
        self.info = {'template':QWEN_FIM_TEMPLATE, 'model_info':{
            'tokenizer.ggml.pre':'qwen2', 'tokenizer.ggml.add_bos_token':False,
            'tokenizer.ggml.tokens':['a','b','ab'], 'tokenizer.ggml.merges':['a b']}}

    def test_empty_suffix_uses_plain_prompt_and_bpe_merge(self):
        counter = VerifiedQwenCounter(self.path, self.info)
        self.assertEqual(counter.render('ab',''), 'ab')
        self.assertEqual(counter.count('ab',''), 1)

    def test_nonempty_suffix_renders_three_fim_markers(self):
        counter = VerifiedQwenCounter(self.path, self.info)
        self.assertEqual(counter.render('a','b'), '<|fim_prefix|>a<|fim_suffix|>b<|fim_middle|>')

    def test_wrong_ids_merges_template_or_bos_fail_closed(self):
        changes = [('tokenizer.ggml.tokens',['b','a','ab']),
                   ('tokenizer.ggml.merges',[]), ('tokenizer.ggml.add_bos_token',True)]
        for key, value in changes:
            info = json.loads(json.dumps(self.info))
            info['model_info'][key] = value
            with self.assertRaisesRegex(ValueError, 'verification_failed'):
                VerifiedQwenCounter(self.path, info)
        info = dict(self.info, template='{{ .Prompt }}')
        with self.assertRaisesRegex(ValueError, 'verification_failed'):
            VerifiedQwenCounter(self.path, info)

    def test_modified_tokenizer_configuration_fails_hash_check(self):
        data = json.loads(self.path.read_text(encoding='utf-8'))
        data['normalizer'] = {'type':'Lowercase'}
        self.path.write_text(json.dumps(data),encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'file hash'):
            VerifiedQwenCounter(self.path, self.info)


if __name__ == '__main__':
    unittest.main()
