from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import error, request


MARKERS = ("▁<PRE>", "▁<SUF>", "▁<MID>", "▁<EOT>")

# How the prefix/suffix pair reaches the model.
#
# "ollama_suffix" hands Ollama the two halves and lets it build the infilling
# prompt from the model's own template, so ▁<PRE>/▁<SUF>/▁<MID> arrive as the
# special tokens (32007-32009) the model was trained on.
#
# "raw_psm" is the original hand-assembled prompt sent with raw=True. It is
# kept only to reproduce the research-3a..3e runs: Ollama does not parse
# marker text out of a raw prompt, so the model receives them as ordinary
# characters, never enters infilling mode, and never emits an end marker. The
# observable symptom is every candidate stopping at done_reason="length" with
# the full num_predict budget spent -- even for a seven-line target body --
# and the bare "<PRE>" spelling being echoed back inside the completion.
TRANSPORTS = ("ollama_suffix", "raw_psm")


def _auditable(reply, payload=None):
    """What is worth keeping about a reply, and what is only bulk.

    Ollama returns ``context`` -- several thousand token ids used to continue a
    conversation. It carries nothing a reader of a failed candidate can use,
    and it grew one run's candidates.json from 32 KB to 142 KB. The request is
    the opposite case: research-3e could not be diagnosed from its own records
    because the prompt that produced them was never written down, so the
    prompt had to be reconstructed by hand before anything could be checked.
    """

    kept = {k: v for k, v in reply.items() if k != "context"}
    if payload is not None:
        kept["request"] = {"prompt": payload.get("prompt"), "suffix": payload.get("suffix"),
                           "options": payload.get("options")}
    return kept


@dataclass
class OllamaFIM:
    """Code Llama infilling via Ollama; no chat/JSON template.

    The default tag is the ``code`` variant because it is the one carrying an
    infilling template in Ollama. ``codellama:7b-instruct`` and plain
    ``codellama:7b`` reject a request with a ``suffix`` (HTTP 400): the
    instruction fine-tune is trained to answer prompts, not to fill a hole
    between two halves of a file.
    """

    model: str = "codellama:7b-code"
    url: str = "http://localhost:11434"
    timeout: int = 180
    num_ctx: int = 4096
    max_new_tokens: int = 512
    temperature: float = 0.2
    transport: str = "ollama_suffix"
    tokenizer_json: str | None = None

    def __post_init__(self):
        if self.timeout <= 0 or self.max_new_tokens <= 0 or self.num_ctx <= self.max_new_tokens + 8:
            raise ValueError("Invalid timeout or token budget")
        if self.temperature < 0:
            raise ValueError("temperature cannot be negative")
        if self.transport not in TRANSPORTS:
            raise ValueError(f"Unknown FIM transport: {self.transport!r}")
        if self.tokenizer_json and self.transport != "ollama_suffix":
            raise ValueError('verified tokenizer budget requires ollama_suffix')

    def post(self, endpoint, payload):
        req = request.Request(self.url.rstrip("/") + "/api/" + endpoint,
                              data=json.dumps(payload).encode("utf-8"),
                              headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=self.timeout) as response:
            return json.load(response)

    def describe(self):
        info = self.post("show", {"model": self.model, "verbose": True})
        metadata = info.get("model_info", {})
        tokens = metadata.get("tokenizer.ggml.tokens", [])
        if self.transport == "raw_psm" and not all(t in tokens for t in MARKERS):
            raise ValueError("unsupported_fim_backend: Code Llama FIM tokens missing")
        tags = self._tags()
        matches = [m for m in tags.get("models", []) if m.get("name") == self.model]
        if not matches:
            raise ValueError("Model must be installed with an explicit matching tag")
        context_lengths = [int(value) for key, value in metadata.items()
                           if key.endswith(".context_length") and isinstance(value, (int, float))]
        context_length = max(context_lengths, default=0)
        if context_length and self.num_ctx > context_length:
            raise ValueError("Configured context exceeds model metadata")
        if self.tokenizer_json:
            from .token_budget import verified_counter
            self._token_counter = verified_counter(self.tokenizer_json, info)
        if self.transport == "ollama_suffix":
            self._require_infilling_template()
        return {"model": self.model, "digest": matches[0]["digest"],
                "details": info.get("details"),
                "capabilities": info.get("capabilities") or [],
                "fim_token_ids": {t: tokens.index(t) for t in MARKERS if t in tokens},
                "num_ctx": self.num_ctx, "max_new_tokens": self.max_new_tokens,
                "temperature": self.temperature, "transport": self.transport,
                "backend": "ollama-fim-v4-token-budget" if self.tokenizer_json else "ollama-fim-v3",
                "budget_measurement": "verified-tokenizer-v1" if self.tokenizer_json else "utf8-byte-upper-bound",
                "tokenizer_sha256": getattr(getattr(self, '_token_counter', None), 'sha256', None)}

    def _tags(self):
        with request.urlopen(self.url.rstrip("/") + "/api/tags", timeout=self.timeout) as r:
            return json.load(r)

    def _require_infilling_template(self):
        """Refuse a tag that cannot infill, before any real generation runs.

        The FIM tokens being present in the vocabulary is not sufficient: every
        Code Llama tag shares one tokenizer, so that check passes for tags whose
        template cannot infill at all. One probe costing a single predicted
        token turns an opaque HTTP 400 mid-batch into a message naming the fix.
        """

        try:
            self.post("generate", {"model": self.model, "prompt": "def f():\n",
                                   "suffix": "\n", "stream": False,
                                   "options": {"num_predict": 1, "temperature": 0}})
        except error.HTTPError as exc:
            raise ValueError(
                f"model_lacks_fim_template: {self.model} does not accept Ollama's "
                f"suffix parameter (HTTP {exc.code}). Install and select an "
                "infilling tag such as codellama:7b-code."
            ) from None

    def _guard_budget(self, text, budget, suffix=""):
        # Byte-fallback tokenizer: byte length is a conservative upper bound.
        # Refuse oversize input rather than let Ollama silently truncate it.
        # The template's own markers are four tokens, inside the margin below.
        if self.tokenizer_json:
            counter = getattr(self, '_token_counter', None)
            if counter is None:
                raise ValueError('tokenizer_verification_required: call describe before generation')
            size = counter.count(text, suffix)
            method = 'verified-tokenizer-v1'
        else:
            size = len((text + suffix).encode('utf-8'))
            method = 'utf8-byte-upper-bound'
        self._last_budget = {'method': method, 'input_count': size, 'output_tokens': budget,
                             'input_unit': 'tokens' if self.tokenizer_json else 'utf8_bytes',
                             'margin_tokens': 8, 'context_limit': self.num_ctx,
                             'fits': size + budget + 8 <= self.num_ctx}
        if not self._last_budget['fits']:
            raise ValueError(f'context_budget_exceeded: {method}; input={size}, output={budget}, margin=8, context={self.num_ctx}')

    def _payload(self, prefix, suffix, seed, budget):
        options = {"num_ctx": self.num_ctx, "num_predict": budget,
                   "temperature": self.temperature, "top_p": 0.95, "seed": seed}
        if self.transport == "ollama_suffix":
            self._guard_budget(prefix, budget, suffix)
            return {"model": self.model, "prompt": prefix, "suffix": suffix,
                    "stream": False, "options": options}
        prompt = MARKERS[0] + prefix + MARKERS[1] + suffix + MARKERS[2]
        self._guard_budget(prompt, budget)
        return {"model": self.model, "prompt": prompt, "raw": True,
                "stream": False, "options": options}

    def infill(self, prefix, suffix, *, seed, max_new_tokens=None):
        """Generate the middle, reporting HOW the model stopped.

        ``stop_reason`` is "visible_eot" when the model closed the infill
        itself, and "length" when it ran to the token cap instead. The second
        case used to be fatal. It is not, because it turned out to be the
        normal outcome rather than an anomaly: across research-3e and 3f every
        candidate hit the cap -- on bodies as short as seven lines, with the
        first generated line already correct and the rest an unbroken run of
        sibling methods. A 7B Code Llama does not emit the end-of-infill marker
        reliably on repository-scale prompts, whatever the transport.

        The caller therefore owns the decision of where the body ends, and has
        to record which of the two happened: a body the model closed and a body
        we cut are not the same evidence, and must not be counted together.
        """

        for marker in ("<PRE>", "<SUF>", "<MID>", "<EOT>", "<FILL_ME>"):
            if marker in prefix or marker in suffix:
                raise ValueError("FIM marker collision in source/evidence")
        budget = self.max_new_tokens if max_new_tokens is None else max_new_tokens
        if budget <= 0:
            raise ValueError("max_new_tokens must be positive")
        payload = self._payload(prefix, suffix, seed, budget)
        reply = self.post("generate", payload)
        raw = reply.get("response", "")
        if not reply.get("done"):
            raise GenerationError("truncated_or_incomplete", _auditable(reply, payload))
        # Do not add a stop string: retain visible EOT evidence, distinct from EOS.
        pos = raw.find("<EOT>")
        if pos >= 0:
            middle, stop_reason = raw[:pos], "visible_eot"
            if middle.endswith("▁"):
                middle = middle[:-1]
            # Remove only EOT-adjacent indentation, not code indentation/newlines.
            middle = middle.rstrip(" \t")
        elif reply.get("done_reason") == "length":
            middle, stop_reason = raw, "length"
        elif (reply.get("done_reason") in {"stop", "load"}
              and self.transport == "ollama_suffix"
              and not self.model.casefold().startswith("codellama")):
            middle, stop_reason = raw, "model_eos"
        else:
            raise GenerationError("missing_visible_eot", _auditable(reply, payload))
        if any(t in middle for t in ("<PRE>", "<SUF>", "<MID>", "```")):
            raise GenerationError("non_code_output", _auditable(reply, payload))
        return {"middle": middle, "raw_response": raw, "seed": seed,
                "stop_reason": stop_reason, "token_budget": budget,
                "prompt": payload.get("prompt"), "suffix": payload.get("suffix"),
                "transport": self.transport,
                "budget_check": self._last_budget.copy(),
                "metrics": {k: reply.get(k) for k in (
                    "done_reason", "prompt_eval_count", "eval_count", "total_duration")}}


@dataclass
class OllamaRewrite:
    """Instruction-model function rewriting through Ollama's generate API.

    This backend deliberately has no suffix or FIM transport. Its caller owns
    the full-function prompt and the later parsing decision.
    """

    model: str = "codellama:7b-instruct"
    url: str = "http://localhost:11434"
    timeout: int = 180
    num_ctx: int = 4096
    max_new_tokens: int = 512
    temperature: float = 0.2

    def __post_init__(self):
        if self.timeout <= 0 or self.max_new_tokens <= 0 or self.num_ctx <= self.max_new_tokens + 8:
            raise ValueError("Invalid timeout or token budget")
        if self.temperature < 0:
            raise ValueError("temperature cannot be negative")

    def post(self, endpoint, payload):
        req = request.Request(self.url.rstrip("/") + "/api/" + endpoint,
                              data=json.dumps(payload).encode("utf-8"),
                              headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=self.timeout) as response:
            return json.load(response)

    def _tags(self):
        with request.urlopen(self.url.rstrip("/") + "/api/tags", timeout=self.timeout) as r:
            return json.load(r)

    def describe(self):
        info = self.post("show", {"model": self.model, "verbose": True})
        metadata = info.get("model_info", {})
        tags = self._tags()
        matches = [m for m in tags.get("models", []) if m.get("name") == self.model]
        if not matches:
            raise ValueError("Model must be installed with an explicit matching tag")
        context_lengths = [int(value) for key, value in metadata.items()
                           if key.endswith(".context_length") and isinstance(value, (int, float))]
        context_length = max(context_lengths, default=0)
        if context_length and self.num_ctx > context_length:
            raise ValueError("Configured context exceeds model metadata")
        return {"model": self.model, "digest": matches[0]["digest"],
                "details": info.get("details"),
                "capabilities": info.get("capabilities") or [],
                "num_ctx": self.num_ctx, "max_new_tokens": self.max_new_tokens,
                "temperature": self.temperature, "transport": "ollama_generate",
                "backend": "ollama-rewrite-v1"}

    def _guard_budget(self, prompt, budget):
        # Keep the same conservative byte bound as the FIM backend so Ollama
        # cannot silently truncate repository context.
        if len(prompt.encode("utf-8")) + budget + 8 > self.num_ctx:
            raise ValueError("context_budget_exceeded: conservative UTF-8 byte bound")

    def rewrite(self, prompt, *, seed, max_new_tokens=None):
        """Request one complete rewritten function without entering FIM mode."""

        if not isinstance(prompt, str) or not prompt:
            raise ValueError("rewrite prompt must be a nonempty string")
        if any(marker in prompt for marker in MARKERS):
            raise ValueError("FIM marker collision in rewrite prompt")
        budget = self.max_new_tokens if max_new_tokens is None else max_new_tokens
        if budget <= 0:
            raise ValueError("max_new_tokens must be positive")
        self._guard_budget(prompt, budget)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            # A reasoning model spends the whole num_predict budget in the reply's
            # "thinking" field and returns response="" with done_reason="length",
            # which the truncation check below reports as truncated_or_incomplete.
            # Ollama accepts think=false for models without the capability, so this
            # is unconditional rather than a per-model flag.
            "think": False,
            "options": {"num_ctx": self.num_ctx, "num_predict": budget,
                        "temperature": self.temperature, "top_p": 0.95, "seed": seed},
        }
        reply = self.post("generate", payload)
        raw = reply.get("response", "")
        if not reply.get("done") or reply.get("done_reason") == "length":
            raise GenerationError("truncated_or_incomplete", _auditable(reply, payload))
        if reply.get("done_reason") != "stop":
            raise GenerationError("unexpected_stop_reason", _auditable(reply, payload))
        if not isinstance(raw, str) or not raw.strip():
            raise GenerationError("empty_response", _auditable(reply, payload))
        return {"raw_response": raw, "seed": seed, "stop_reason": "model_eos",
                "token_budget": budget, "prompt": prompt,
                "transport": "ollama_generate",
                "metrics": {k: reply.get(k) for k in (
                    "done_reason", "prompt_eval_count", "eval_count", "total_duration")}}


class GenerationError(ValueError):
    def __init__(self, message, response):
        super().__init__(message)
        self.response = response
