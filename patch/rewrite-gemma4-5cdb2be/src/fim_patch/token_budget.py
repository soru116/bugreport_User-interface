"""Optional, locally verified Qwen FIM token accounting (no inference)."""
import hashlib
import json
from pathlib import Path

QWEN_FIM_TEMPLATE = '{{- if .Suffix }}<|fim_prefix|>{{ .Prompt }}<|fim_suffix|>{{ .Suffix }}<|fim_middle|>{{ else }}{{ .Prompt }}{{ end }}'
QWEN_TOKENIZER_SHA256 = 'c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539'
CODELLAMA_TOKENIZER_SHA256 = 'dcb4337ba92f948b322e080f8b59a2a21cfe65dc2aa7ecf7df080ae8aa63c157'


class VerifiedCodeLlamaCounter:
    def __init__(self, path, info):
        from tokenizers import Tokenizer
        raw = Path(path).read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        expected_template = '{{- if .Suffix }}<PRE> {{ .Prompt }} <SUF>{{ .Suffix }} <MID>\n{{- else }}{{ .Prompt }}\n{{- end }}'
        metadata = info.get('model_info', {})
        if (self.sha256 != CODELLAMA_TOKENIZER_SHA256
                or metadata.get('tokenizer.ggml.model') != 'llama'
                or metadata.get('tokenizer.ggml.bos_token_id') != 1
                or info.get('template', '').strip() != expected_template):
            raise ValueError('tokenizer_verification_failed: Code Llama file/template/BOS mismatch')
        self.tokenizer = Tokenizer.from_str(raw.decode('utf-8'))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        tokens = metadata.get('tokenizer.ggml.tokens', [])
        vocab = self.tokenizer.get_vocab()
        if len(vocab) != len(tokens) or any(tokens[i] != text for text,i in vocab.items()):
            raise ValueError('tokenizer_verification_failed: Code Llama vocabulary differs')
        if not all(marker in vocab for marker in MARKER_TOKENS):
            raise ValueError('tokenizer_verification_failed: Code Llama FIM tokens missing')

    def count(self, prefix, suffix):
        # Exact installed Ollama template; include the tokenizer's BOS.
        text = '<PRE> '+prefix+' <SUF>'+suffix+' <MID>' if suffix else prefix
        return len(self.tokenizer.encode(text, add_special_tokens=True).ids)


MARKER_TOKENS = ('▁<PRE>', '▁<SUF>', '▁<MID>', '▁<EOT>')


def verified_counter(path, info):
    if info.get('model_info', {}).get('tokenizer.ggml.model') == 'llama':
        return VerifiedCodeLlamaCounter(path, info)
    return VerifiedQwenCounter(path, info)


class VerifiedQwenCounter:
    def __init__(self, path, info):
        from tokenizers import Tokenizer
        raw = Path(path).read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        # Matching vocab/merges alone does not verify normalization or regex
        # pre-tokenization. Pin the complete official tokenizer configuration.
        if self.sha256 != QWEN_TOKENIZER_SHA256:
            raise ValueError('tokenizer_verification_failed: unsupported tokenizer file hash')
        config = json.loads(raw)
        metadata = info.get('model_info', {})
        if (metadata.get('tokenizer.ggml.pre') != 'qwen2'
                or metadata.get('tokenizer.ggml.add_bos_token') is not False
                or info.get('template') != QWEN_FIM_TEMPLATE):
            raise ValueError('tokenizer_verification_failed: unsupported tokenizer/template/BOS')
        self.tokenizer = Tokenizer.from_str(raw.decode('utf-8'))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        tokens = metadata.get('tokenizer.ggml.tokens', [])
        vocab = self.tokenizer.get_vocab()
        if not tokens or any(i >= len(tokens) or tokens[i] != text for text, i in vocab.items()):
            raise ValueError('tokenizer_verification_failed: vocabulary/token IDs differ')
        merges = [' '.join(x) if isinstance(x, list) else x for x in config['model']['merges']]
        if merges != metadata.get('tokenizer.ggml.merges'):
            raise ValueError('tokenizer_verification_failed: BPE merges differ')
        self.extra_tokens = tokens[len(vocab):]

    def render(self, prefix, suffix):
        return ('<|fim_prefix|>' + prefix + '<|fim_suffix|>' + suffix + '<|fim_middle|>'
                if suffix else prefix)

    def count(self, prefix, suffix):
        text = self.render(prefix, suffix)
        if any(token and token in text for token in self.extra_tokens):
            raise ValueError('tokenizer_verification_failed: model-only added token in prompt')
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)
