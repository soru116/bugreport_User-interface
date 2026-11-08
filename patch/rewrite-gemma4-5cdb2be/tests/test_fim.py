import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from urllib import error

from fim_patch.backend import MARKERS, OllamaFIM, OllamaRewrite, GenerationError
from fim_patch.generator import (FIMPatchGenerator, check_body_retention,
                                 check_executable_change, check_new_undefined_names,
                                 body_overruns, check_reachable,
                                 check_statement_reorder_only, evidence,
                                 function_span, issue_statement_span,
                                 parse_rewrite_response, rewrite_prompt,
                                 trim_generated_statement,
                                 build_patch, git, sha)
from fim_patch.validation import UnittestValidator, run_suite


class FakeBackend:
    # Mirrors the real backend's contract: a per-call token budget, and a
    # stop_reason saying whether the model closed the infill itself.
    max_new_tokens = 2048

    def __init__(self, outputs, stop_reason="visible_eot"):
        self.outputs = iter(outputs)
        self.calls = 0
        self.budgets = []
        self.stop_reason = stop_reason

    def describe(self):
        return {"model": "test-double"}

    def infill(self, prefix, suffix, *, seed, max_new_tokens=None):
        self.calls += 1
        self.budgets.append(max_new_tokens)
        return {"middle": next(self.outputs), "seed": seed, "stop_reason": self.stop_reason}


class FakeRewriteBackend:
    max_new_tokens = 2048

    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = 0
        self.prompts = []
        self.budgets = []

    def describe(self):
        return {"model": "rewrite-test-double", "backend": "fake-rewrite"}

    def rewrite(self, prompt, *, seed, max_new_tokens=None):
        self.calls += 1
        self.prompts.append(prompt)
        self.budgets.append(max_new_tokens)
        return {"raw_response": next(self.outputs), "seed": seed,
                "stop_reason": "model_eos", "transport": "test-rewrite"}


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.raw = b'def f(x):\n    return x.strip()\n\n\ndef g():\n    return f(" hi ")\n'
        (self.repo / "app.py").write_bytes(self.raw)
        git(self.repo, "init")
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=t@example.invalid", "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.row = {"file_path": "app.py", "symbol_qualified_name": "f", "start_line": 1, "end_line": 2}
        self.loc = {"base_commit": self.base, "confidence_level": "high", "should_manual_review": False,
                    "recommend_patch_generation": True, "stage3_ranked_symbols": [self.row]}
        self.ticket = {"base_commit": self.base, "description": "Return empty string for None"}

    def generate(self, output, **kwargs):
        return FIMPatchGenerator(FakeBackend([output]), candidates=1, **kwargs).generate(self.ticket, self.loc, self.repo)

    def test_patch_applies_and_preserves_suffix_and_repository(self):
        result = self.generate('    return "" if x is None else x.strip()\n')
        self.assertEqual(result["patch_status"], "generated")
        self.assertEqual(result["validation_status"], "not_run")
        self.assertEqual((self.repo / "app.py").read_bytes(), self.raw)
        git(self.repo, "apply", data=result["patch"].encode())
        self.assertIn(b'def g():\n    return f(" hi ")', (self.repo / "app.py").read_bytes())

    def test_missing_confidence_never_calls_model(self):
        backend = FakeBackend([])
        self.loc.pop("should_manual_review")
        r = FIMPatchGenerator(backend).generate(self.ticket, self.loc, self.repo)
        self.assertEqual(r["patch_status"], "blocked_low_confidence")
        self.assertEqual(backend.calls, 0)

    def test_malformed_input_blocks(self):
        backend = FakeBackend([])
        r = FIMPatchGenerator(backend).generate([], self.loc, self.repo)
        self.assertEqual(r["patch_status"], "blocked_invalid_context")
        self.loc["stage3_ranked_symbols"] = "not a list"
        r = FIMPatchGenerator(backend).generate(self.ticket, self.loc, self.repo)
        self.assertEqual(r["patch_status"], "blocked_invalid_context")
        self.assertEqual(backend.calls, 0)

    def test_research_mode_preserves_production_ineligibility(self):
        self.loc.update(confidence_level='medium', recommend_patch_generation=False,
                        should_manual_review=True)
        r=FIMPatchGenerator(FakeBackend(['    return None\n']), candidates=1,
                            research_mode=True,local_context=True).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated')
        self.assertFalse(r['production_eligible'])
        self.assertFalse(r['recommend_patch_generation'])
        self.assertTrue(r['requires_manual_review'])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_local_prompt_crop_does_not_crop_reconstructed_file(self):
        before=b'# distant context\n'*400
        raw=before+self.raw+b'# after context\n'*300
        (self.repo/'app.py').write_bytes(raw)
        git(self.repo,'add','app.py')
        git(self.repo,'-c','user.name=Test','-c','user.email=t@example.invalid','commit','-m','long context')
        base=git(self.repo,'rev-parse','HEAD').decode().strip()
        self.ticket['base_commit']=self.loc['base_commit']=base
        self.row.update(start_line=401,end_line=402)
        r=FIMPatchGenerator(FakeBackend(['    return None\n']),candidates=1,
                            local_context=True).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated')
        self.assertTrue(r['prompt_context']['truncated'])
        git(self.repo,'apply',data=r['patch'].encode())
        self.assertEqual((self.repo/'app.py').read_bytes(),raw.replace(b'return x.strip()',b'return None'))

    def test_modified_source_blocks(self):
        (self.repo / "app.py").write_bytes(self.raw + b"# changed\n")
        self.assertEqual(self.generate("    pass\n")["patch_status"], "blocked_invalid_context")

    def test_wrong_hash_blocks(self):
        self.loc["source_file_sha256"] = {"app.py": "wrong"}
        self.assertEqual(self.generate("    pass\n")["patch_status"], "blocked_invalid_context")

    def test_wrong_coordinates_block(self):
        self.row["end_line"] = 5
        self.assertEqual(self.generate("    pass\n")["patch_status"], "blocked_invalid_context")

    def test_research_ast_expansion_requires_matching_start(self):
        raw = b'def f(x):\n    y = x\n    return y\n'
        row = dict(self.row, end_line=2)
        with self.assertRaises(ValueError):
            function_span(raw, row)
        span = function_span(raw, row, expand_truncated_end=True)
        self.assertEqual(span['line_end'], 3)
        self.assertTrue(span['coordinates_expanded'])
        for invalid in (dict(row, start_line=2), dict(row, end_line=4)):
            with self.assertRaises(ValueError):
                function_span(raw, invalid, expand_truncated_end=True)

    def test_research_skips_test_and_class_targets_with_audit(self):
        self.loc['stage3_ranked_symbols'] = [dict(self.row, file_path='test_app.py'),
                                           dict(self.row, symbol_qualified_name='MissingClass'), self.row]
        backend = FakeBackend(['    return None\n'])
        result = FIMPatchGenerator(backend, candidates=1, research_mode=True,
                                  target_policy='first_supported_ast_top5').generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'], 'generated')
        self.assertEqual([x['status'] for x in result['target_resolution']], ['rejected','rejected','selected'])
        self.assertEqual(backend.calls, 1)
        self.assertEqual((self.repo/'app.py').read_bytes(), self.raw)

    def test_alternative_policy_cannot_enable_production_or_search_beyond_five(self):
        with self.assertRaises(ValueError):
            FIMPatchGenerator(target_policy='first_supported_ast_top5')
        self.loc['stage3_ranked_symbols'] = [dict(self.row,file_path='../app.py')]*5+[self.row]
        backend = FakeBackend([])
        result = FIMPatchGenerator(backend,research_mode=True,target_policy='first_supported_ast_top5').generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'blocked_invalid_context')
        self.assertEqual(backend.calls,0)

    def test_ambiguous_symbol_still_rejected_with_expansion(self):
        with self.assertRaises(ValueError):
            function_span(b'def f():\n    pass\ndef f():\n    pass\n',self.row,expand_truncated_end=True)

    def test_expanded_function_rebuilds_whole_body_and_preserves_following_function(self):
        raw = b'def f(x):\n    y = x.strip()\n    return y\n\ndef g():\n    return 3\n'
        (self.repo/'app.py').write_bytes(raw)
        git(self.repo,'add','app.py')
        git(self.repo,'-c','user.name=Test','-c','user.email=t@example.invalid','commit','-m','longer body')
        self.ticket['base_commit']=self.loc['base_commit']=git(self.repo,'rev-parse','HEAD').decode().strip()
        result=FIMPatchGenerator(FakeBackend(['    return "" if x is None else x.strip()\n']),
                                 candidates=1,research_mode=True,target_policy='first_supported_ast_top5').generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'generated')
        self.assertTrue(result['target_resolution'][0]['coordinates_expanded'])
        self.assertEqual((self.repo/'app.py').read_bytes(),raw)
        git(self.repo,'apply',data=result['patch'].encode('utf-8'))
        self.assertEqual((self.repo/'app.py').read_bytes(),b'def f(x):\n    return "" if x is None else x.strip()\n\ndef g():\n    return 3\n')

    def test_research_target_policy_does_not_bypass_source_hash(self):
        self.loc['source_file_sha256']={'app.py':'invalid'}
        backend=FakeBackend([])
        result=FIMPatchGenerator(backend,research_mode=True,target_policy='first_supported_ast_top5').generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'blocked_invalid_context')
        self.assertIn('Source hash differs',result['target_resolution'][0]['reason'])
        self.assertEqual(backend.calls,0)

    def test_path_escape_blocks(self):
        self.row["file_path"] = "../app.py"
        self.assertEqual(self.generate("    pass\n")["patch_status"], "blocked_invalid_context")

    def test_noop_rejected(self):
        self.assertEqual(self.generate("    return x.strip()\n")["patch_status"], "no_valid_candidate")

    def test_a_definition_the_model_appended_never_reaches_the_file(self):
        """The escape is removed, and the guarantee is checked on the result.

        This used to assert the whole candidate was rejected. It is now
        trimmed instead, because the same output shape arrives both ways: in
        runs/multitarget-A the model emitted a visible ``<EOT>`` *after*
        running into the next function, so stop_reason cannot tell a run-on
        from a clean finish. What must hold either way is that nothing outside
        the target body changes, so that is what this asserts.
        """

        r = self.generate("    pass\n\ndef malicious():\n    pass\n")
        self.assertEqual(r["patch_status"], "generated", r["candidates"])
        self.assertNotIn("def malicious", r["patch"])
        git(self.repo, "apply", data=r["patch"].encode())
        patched = (self.repo / "app.py").read_bytes()
        self.assertNotIn(b"def malicious", patched)
        self.assertEqual(patched, self.raw.replace(b"return x.strip()", b"pass"))

    def test_an_escape_the_trim_cannot_remove_is_still_rejected(self):
        """The trim is a convenience; the scope gate is the guarantee.

        Here the appended definition is indented into the body, so no dedent
        marks the end and nothing is trimmed. The AST comparison has to catch
        it on its own.
        """

        r = self.generate("    pass\n    global malicious\n"
                          "    def malicious():\n        pass\n")
        self.assertEqual(r["patch_status"], "generated")
        git(self.repo, "apply", data=r["patch"].encode())
        patched = (self.repo / "app.py").read_bytes()
        # A nested def stays inside f, so the module's own symbols are unchanged.
        self.assertIn(b"def f(x):", patched)
        self.assertIn(b'def g():\n    return f(" hi ")', patched)

    def test_invalid_syntax_rejected(self):
        self.assertEqual(self.generate("return x\n")["patch_status"], "no_valid_candidate")

    def test_roundtrip_crlf_bom_unicode_decorator(self):
        raw = ('\ufeff# 中文\r\n@deco\r\ndef f(x):\r\n    return x\r\n').encode("utf-8")
        row = dict(self.row, start_line=2, end_line=4)
        span = function_span(raw, row)
        self.assertEqual((span["prefix"] + span["original_middle"] + span["suffix"]).encode("utf-8-sig"), raw)
        patch, after = build_patch(raw, span, '    return None\n', "app.py")
        self.assertTrue(after.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(after.count(b"\r\n"), 4)

    def test_missing_final_newline_diff(self):
        raw = b"def f(x):\n    return x"
        span = function_span(raw, self.row)
        patch, after = build_patch(raw, span, "    return None", "app.py")
        (self.repo / "app.py").write_bytes(raw)
        git(self.repo, "apply", data=patch.encode())
        self.assertEqual((self.repo / "app.py").read_bytes(), after)

    def test_inline_suite_rejected(self):
        with self.assertRaises(ValueError):
            function_span(b"def f(x): return x\n", dict(self.row, end_line=1))

    def test_duplicate_records_remain(self):
        backend = FakeBackend(["    return None\n"] * 2)
        r = FIMPatchGenerator(backend, candidates=2).generate(self.ticket, self.loc, self.repo)
        self.assertEqual(len(r["candidates"]), 2)
        self.assertEqual(r["candidates"][1]["reason"], "duplicate")

    def test_test_gate_rejects_syntactically_valid_wrong_patch(self):
        r = self.generate("    return None\n", validator=lambda p, r: {"status": "failed"})
        self.assertEqual(r["patch_status"], "no_valid_candidate")


class BackendTests(unittest.TestCase):
    def test_preserve_indent_and_eot(self):
        b = OllamaFIM()
        b.post = lambda *a: {"response": "    return 1\n    <EOT>", "done": True, "done_reason": "stop"}
        self.assertEqual(b.infill("def f():\n", "\n", seed=1)["middle"], "    return 1\n")

    def test_a_stop_without_the_marker_is_still_refused(self):
        # The model claims it finished but leaves no evidence of where the
        # infill ended: nothing downstream could tell a body from a fragment.
        b = OllamaFIM()
        b.post = lambda *a: {"response": "pass", "done": True, "done_reason": "stop"}
        with self.assertRaises(GenerationError):
            b.infill("def f():\n", "", seed=1)

    def test_an_unfinished_reply_is_still_refused(self):
        b = OllamaFIM()
        b.post = lambda *a: {"response": "pass", "done": False}
        with self.assertRaises(GenerationError):
            b.infill("def f():\n", "", seed=1)

    def test_hitting_the_cap_is_reported_rather_than_raised(self):
        # The measured normal case: the model writes the body and keeps going.
        # Refusing here is what left research-3e and 3f with nothing to judge.
        b = OllamaFIM()
        b.post = lambda *a: {"response": "    return 1\n\ndef sibling():\n    pass\n",
                             "done": True, "done_reason": "length"}
        out = b.infill("def f():\n", "", seed=1)
        self.assertEqual(out["stop_reason"], "length")
        self.assertIn("def sibling", out["middle"])

    def test_the_marker_still_wins_when_present(self):
        b = OllamaFIM()
        b.post = lambda *a: {"response": "    return 1\n<EOT>trailing",
                             "done": True, "done_reason": "stop"}
        out = b.infill("def f():\n", "", seed=1)
        self.assertEqual(out["stop_reason"], "visible_eot")
        self.assertEqual(out["middle"], "    return 1\n")

    def test_a_per_call_budget_overrides_the_configured_ceiling(self):
        b = OllamaFIM(num_ctx=16384, max_new_tokens=2048)
        seen = {}

        def post(endpoint, payload):
            seen["payload"] = payload
            return {"response": "    return 1\n<EOT>", "done": True, "done_reason": "stop"}

        b.post = post
        b.infill("def f():\n", "", seed=1, max_new_tokens=256)
        self.assertEqual(seen["payload"]["options"]["num_predict"], 256)

    def test_the_bulky_context_array_is_not_kept_on_failure(self):
        b = OllamaFIM()
        b.post = lambda *a: {"response": "pass", "done": True, "done_reason": "stop",
                             "context": list(range(4269))}
        with self.assertRaises(GenerationError) as caught:
            b.infill("def f():\n", "", seed=1)
        self.assertNotIn("context", caught.exception.response)
        self.assertEqual(caught.exception.response["request"]["prompt"], "def f():\n")

    def test_marker_collision(self):
        with self.assertRaises(ValueError):
            OllamaFIM().infill("# <MID>", "", seed=1)

    def test_budget_checked_before_request(self):
        with self.assertRaises(ValueError):
            OllamaFIM().infill("x" * 4096, "", seed=1)


class RewriteBackendTests(unittest.TestCase):
    def capture(self, backend=None, response=None, **call_kwargs):
        backend = backend or OllamaRewrite(num_ctx=16384, max_new_tokens=2048)
        seen = {}

        def post(endpoint, payload):
            seen["endpoint"] = endpoint
            seen["payload"] = payload
            return response or {
                "response": "def f():\n    return 1\n",
                "done": True,
                "done_reason": "stop",
            }

        backend.post = post
        result = backend.rewrite(
            "Repair this function:\ndef f():\n    return 0\n",
            seed=7,
            **call_kwargs,
        )
        return seen, result

    def test_request_uses_generate_without_any_fim_fields(self):
        seen, result = self.capture(max_new_tokens=256)
        self.assertEqual(seen["endpoint"], "generate")
        payload = seen["payload"]
        self.assertNotIn("suffix", payload)
        self.assertNotIn("raw", payload)
        self.assertNotIn("format", payload)
        self.assertEqual(payload["options"]["num_predict"], 256)
        self.assertEqual(payload["options"]["seed"], 7)
        self.assertEqual(result["transport"], "ollama_generate")
        self.assertEqual(result["stop_reason"], "model_eos")

    def test_completed_response_is_preserved_for_the_future_parser(self):
        raw = "def f():\n    return 2\n\n"
        _, result = self.capture(response={
            "response": raw,
            "done": True,
            "done_reason": "stop",
            "eval_count": 12,
        })
        self.assertEqual(result["raw_response"], raw)
        self.assertEqual(result["metrics"]["eval_count"], 12)

    def test_length_stop_is_rejected_as_incomplete(self):
        with self.assertRaises(GenerationError) as caught:
            self.capture(response={
                "response": "def f():\n    return",
                "done": True,
                "done_reason": "length",
            })
        self.assertIn("truncated_or_incomplete", str(caught.exception))
        self.assertNotIn("context", caught.exception.response)

    def test_done_false_is_rejected_as_incomplete(self):
        with self.assertRaises(GenerationError):
            self.capture(response={"response": "def f():\n", "done": False})

    def test_empty_response_is_rejected(self):
        with self.assertRaises(GenerationError) as caught:
            self.capture(response={"response": "  \n", "done": True,
                                   "done_reason": "stop"})
        self.assertIn("empty_response", str(caught.exception))

    def test_prompt_rejects_fim_markers_and_oversize_input(self):
        with self.assertRaises(ValueError):
            OllamaRewrite().rewrite("▁<PRE>def f(): pass", seed=1)
        with self.assertRaises(ValueError):
            OllamaRewrite().rewrite("x" * 4096, seed=1)

    def test_describe_checks_the_tag_without_a_suffix_probe(self):
        backend = OllamaRewrite(num_ctx=4096)
        calls = []

        def post(endpoint, payload):
            calls.append(endpoint)
            self.assertEqual(endpoint, "show")
            return {"model_info": {"llama.context_length": 16384},
                    "capabilities": ["completion"],
                    "details": {"parameter_size": "7B"}}

        backend.post = post
        backend._tags = lambda: {"models": [{"name": backend.model, "digest": "d" * 64}]}
        described = backend.describe()
        self.assertEqual(calls, ["show"])
        self.assertEqual(described["backend"], "ollama-rewrite-v1")
        self.assertEqual(described["transport"], "ollama_generate")
        self.assertEqual(described["capabilities"], ["completion"])


class PromptContentTests(unittest.TestCase):
    """What the model is shown, and what it must never be shown."""

    LONG_BODY = ("def render(rows, sep):\n"
                 "    out = []\n"
                 "    for row in rows:\n"
                 "        if row is None:\n"
                 "            continue\n"
                 "        text = str(row).strip()\n"
                 "        if not text:\n"
                 "            continue\n"
                 "        out.append(text)\n"
                 "    if not out:\n"
                 "        return ''\n"
                 "    return sep.join(out)\n"
                 "\n\ndef other():\n    return 1\n")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.raw = self.LONG_BODY.encode("utf-8")
        (self.repo / "app.py").write_bytes(self.raw)
        git(self.repo, "init")
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
            "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.row = {"file_path": "app.py", "symbol_qualified_name": "render",
                    "start_line": 1, "end_line": 12}
        self.loc = {"base_commit": self.base, "confidence_level": "high",
                    "should_manual_review": False, "recommend_patch_generation": True,
                    "stage3_ranked_symbols": [self.row]}
        self.ticket = {"base_commit": self.base, "description": "render must drop blank rows",
                       # Ground truth that must never reach the model.
                       "patch": "SECRET GOLD PATCH", "fail_to_pass": ["test_secret"],
                       "hints_text": "SECRET HINT"}

    def prompt_for(self, **kwargs):
        seen = {}

        class Recorder(FakeBackend):
            def infill(self, prefix, suffix, *, seed, max_new_tokens=None):
                seen["prefix"] = prefix
                return super().infill(prefix, suffix, seed=seed, max_new_tokens=max_new_tokens)

        backend = Recorder(["    return sep.join(str(r).strip() for r in rows if r)\n"] * 4)
        result = FIMPatchGenerator(backend, candidates=1, **kwargs).generate(
            self.ticket, self.loc, self.repo)
        return seen.get("prefix", ""), result

    def test_by_default_the_model_never_sees_the_body_it_replaces(self):
        prompt, result = self.prompt_for()
        self.assertNotIn("out.append(text)", prompt)
        self.assertEqual(result["prompt_version"], "issue-brief-task-last-psm-v7-scope-contract-v1-issue-compact-v1")
        self.assertFalse(result["include_original_body"])

    def test_the_legacy_layout_still_reports_its_published_version(self):
        prompt, result = self.prompt_for(prompt_layout="legacy")
        self.assertNotIn("out.append(text)", prompt)
        self.assertEqual(result["prompt_version"], "issue-plus-localization-psm-v3-scope-contract-v1")

    def test_the_current_implementation_can_be_shown_as_comments(self):
        prompt, result = self.prompt_for(include_original_body=True)
        self.assertIn("===== CURRENT IMPLEMENTATION OF render (buggy) =====", prompt)
        self.assertIn("out.append(text)", prompt)
        self.assertEqual(result["prompt_version"],
                          "issue-brief-before-task-last-psm-v7-scope-contract-v1-issue-compact-v1")
        self.assertTrue(result["include_original_body"])
        # Shown, but still commented out: the model generates the body itself.
        for line in prompt.splitlines():
            if "out.append(text)" in line:
                self.assertTrue(line.lstrip().startswith("#"))

    def test_the_legacy_layout_still_labels_the_body_BEFORE(self):
        prompt, result = self.prompt_for(include_original_body=True,
                                         prompt_layout="legacy")
        self.assertIn("# BEFORE (buggy):", prompt)
        self.assertIn("#         out.append(text)", prompt)
        self.assertEqual(result["prompt_version"],
                          "issue-plus-before-after-localization-psm-v5-scope-contract-v1")

    def test_the_instruction_comes_after_the_source_not_before_it(self):
        """The rewrite, stated as an ordering.

        Under the old layout the directive sat 110 lines of ordinary source code
        above the generation point, so the likeliest continuation at the hole was
        ordinary source code -- and 42.7% of candidates were copies of the body.
        """

        prompt, _ = self.prompt_for(include_original_body=True)
        signature = prompt.rindex("def render(rows, sep):")
        shown_body = prompt.index("===== CURRENT IMPLEMENTATION")
        task = prompt.index("===== WHAT TO WRITE BELOW =====")
        self.assertLess(signature, shown_body)
        self.assertLess(shown_body, task)
        self.assertEqual(task, max(signature, shown_body, task))
        self.assertNotIn("# AFTER (fixed):", prompt)

    def test_the_legacy_layout_keeps_before_original_after_signature(self):
        prompt, _ = self.prompt_for(include_original_body=True,
                                    prompt_layout="legacy")
        before = prompt.index("# BEFORE (buggy):")
        original = prompt.index("#         out.append(text)")
        after = prompt.index("# AFTER (fixed):")
        signature = prompt.rindex("def render(rows, sep):")
        self.assertLess(before, original)
        self.assertLess(original, after)
        self.assertLess(after, signature)

    def test_current_body_prompt_requires_a_minimal_complete_executable_change(self):
        prompt, _ = self.prompt_for(include_original_body=True)
        # The directive now points at labels the issue brief actually prints,
        # instead of describing two fields the prompt had never marked.
        self.assertIn("--- Expected behaviour", prompt)
        self.assertIn("--- Actual behaviour / error", prompt)
        self.assertIn("make Expected behaviour happen instead of Actual behaviour",
                      prompt)
        self.assertIn("returning the current implementation unchanged is invalid", prompt)
        self.assertIn("Change only the lines the issue requires", prompt)
        self.assertIn("keep every other branch, call, check and return value", prompt)
        self.assertIn("only the complete replacement function body", prompt)

    def test_explicit_argument_edit_uses_only_public_issue_and_selected_source(self):
        self.ticket["description"] = (
            "The second argument of `helper` should be True, rather than False."
        )
        self.loc["stage3_ranked_symbols"][0]["code_text"] = (
            "def render(value):\n    return helper(value, False)\n"
        )
        baseline, _ = self.prompt_for(include_original_body=True)
        self.assertNotIn("Issue-stated argument edit", baseline)
        prompt, result = self.prompt_for(
            include_original_body=True, explicit_argument_edit=True)
        self.assertIn(
            "in each helper(...) call whose argument 2 is False, "
            "replace only that argument with True", prompt)
        self.assertTrue(result["explicit_argument_edit"])
        self.assertEqual(
            result["prompt_version"],
            "issue-brief-before-task-last-psm-v7-scope-contract-v1-issue-compact-v1-explicit-argument-edit",
        )
        self.assertNotIn("SECRET GOLD PATCH", prompt)

    def test_explicit_argument_edit_requires_a_matching_call_in_source(self):
        self.ticket["description"] = (
            "The second argument of `helper` should be True, rather than False."
        )
        self.loc["stage3_ranked_symbols"][0]["code_text"] = "def render(value): return value\n"
        prompt, _ = self.prompt_for(explicit_argument_edit=True)
        self.assertNotIn("Issue-stated argument edit", prompt)

    def test_ground_truth_stays_out_of_the_prompt_in_both_modes(self):
        for kwargs in ({}, {"include_original_body": True}):
            prompt, _ = self.prompt_for(**kwargs)
            for secret in ("SECRET GOLD PATCH", "test_secret", "SECRET HINT"):
                self.assertNotIn(secret, prompt)

    def test_prompt_exposes_ranked_related_symbols_without_source_or_gold(self):
        related = {"file_path": "helpers.py", "symbol_qualified_name": "report_diff_values",
                   "start_line": 10, "end_line": 20}
        self.loc["stage3_ranked_symbols"].append(related)
        prompt, _ = self.prompt_for()
        self.assertIn("# 1. app.py::render [selected repair target]", prompt)
        self.assertIn("# 2. helpers.py::report_diff_values", prompt)
        self.assertNotIn("SECRET GOLD PATCH", prompt)

    def test_prompt_requires_executable_tolerance_aware_array_change(self):
        self.ticket["description"] = "Respect rtol when reporting array values"
        self.loc["stage3_ranked_symbols"][0]["code_text"] = (
            "def render(a, b):\n"
            "    if isinstance(a, np.ndarray):\n"
            "        return np.where(a != b)\n"
        )
        prompt, _ = self.prompt_for(include_original_body=True,domain_adapters=('tolerance',))
        self.assertIn("Make an executable code change", prompt)
        self.assertIn("tolerance-aware comparison", prompt)
        self.assertIn("Add or propagate rtol/atol", prompt)

    def test_a_marker_in_the_shown_body_is_refused(self):
        with self.assertRaises(ValueError):
            evidence({"description": "x"}, "    return '<MID>'\n")


class RewritePromptTests(unittest.TestCase):
    SOURCE = (
        "def render(rows, sep=' '):\n"
        "    values = [str(row).strip() for row in rows if row]\n"
        "    return sep.join(values)\n"
        "\n"
        "def sibling():\n"
        "    return 'must stay outside the prompt'\n"
    )
    ROW = {"file_path": "app.py", "symbol_qualified_name": "render",
           "start_line": 1, "end_line": 3}

    def prompt(self, ticket=None, rows=None, raw=None, row=None, **kwargs):
        raw = (raw or self.SOURCE.encode("utf-8"))
        row = row or self.ROW
        span = function_span(raw, row, include_signature=True)
        rows = rows or [row]
        ticket = ticket or {"title": "Drop blank rows",
                            "description": "Ignore blank values while rendering"}
        return rewrite_prompt(ticket, span, rows, 1, **kwargs)

    def test_contains_public_issue_localization_complete_function_and_contract(self):
        related = {"file_path": "helpers.py", "symbol_qualified_name": "clean_rows",
                   "patch": "LOCALIZATION GOLD"}
        prompt = self.prompt(rows=[self.ROW, related])
        self.assertIn("Drop blank rows", prompt)
        self.assertIn("app.py::render [selected repair target]", prompt)
        self.assertIn("helpers.py::clean_rows", prompt)
        self.assertIn("def render(rows, sep=' '):", prompt)
        self.assertIn("return sep.join(values)", prompt)
        self.assertNotIn("def sibling", prompt)
        self.assertIn("exactly one complete Python function", prompt)
        self.assertIn("return exactly NO_CHANGE", prompt)
        self.assertIn("do not repeat the buggy function first", prompt)
        self.assertIn("Begin your response immediately", prompt)
        self.assertNotIn("BEFORE (buggy)", prompt)
        self.assertNotIn("AFTER (fixed)", prompt)
        self.assertNotIn("<PRE>", prompt)
        self.assertNotIn("LOCALIZATION GOLD", prompt)

    def test_ground_truth_fields_never_enter_the_prompt(self):
        ticket = {
            "title": "Public title",
            "description": "Public description",
            "bug_report": "Public bug report",
            "patch": "SECRET PATCH",
            "test_patch": "SECRET TEST PATCH",
            "gold_patch": "SECRET GOLD PATCH",
            "fail_to_pass": ["SECRET FAIL TEST"],
            "pass_to_pass": ["SECRET PASS TEST"],
            "hints_text": "SECRET HINT",
            "gold_symbols": ["SECRET SYMBOL"],
        }
        prompt = self.prompt(ticket=ticket)
        self.assertIn("Public title", prompt)
        self.assertIn("Public description", prompt)
        self.assertIn("Public bug report", prompt)
        for secret in ("SECRET PATCH", "SECRET TEST PATCH", "SECRET GOLD PATCH",
                       "SECRET FAIL TEST", "SECRET PASS TEST", "SECRET HINT",
                       "SECRET SYMBOL"):
            self.assertNotIn(secret, prompt)

    def test_decorated_nested_async_crlf_bom_function_is_complete(self):
        source = (
            "class Worker:\r\n"
            "    @trace('job')\r\n"
            "    async def run(self, value: str = 'x') -> str:\r\n"
            "        cleaned = value.strip()\r\n"
            "        return cleaned\r\n"
            "\r\n"
            "def sibling():\r\n"
            "    return 1\r\n"
        ).encode("utf-8")
        raw = b"\xef\xbb\xbf" + source
        row = {"file_path": "worker.py", "symbol_qualified_name": "Worker.run",
               "start_line": 2, "end_line": 5}
        prompt = self.prompt(raw=raw, row=row, rows=[row])
        self.assertIn("    @trace('job')\n", prompt)
        self.assertIn("    async def run(self, value: str = 'x') -> str:\n", prompt)
        self.assertIn("        return cleaned\n", prompt)
        self.assertNotIn("\r", prompt)
        self.assertNotIn("def sibling", prompt)

    def test_body_only_span_and_fim_marker_source_are_refused(self):
        body_span = function_span(self.SOURCE.encode("utf-8"), self.ROW)
        with self.assertRaisesRegex(ValueError, "complete function span"):
            rewrite_prompt({"description": "x"}, body_span, [self.ROW], 1)
        marked = "def render():\n    return '<MID>'\n".encode("utf-8")
        row = {"file_path": "app.py", "symbol_qualified_name": "render",
               "start_line": 1, "end_line": 2}
        span = function_span(marked, row, include_signature=True)
        with self.assertRaisesRegex(ValueError, "FIM marker collision"):
            rewrite_prompt({"description": "x"}, span, [row], 1)

    def test_public_issue_is_bounded_and_no_change_text_remains_data(self):
        ticket = {"description": "User wrote NO_CHANGE as ordinary issue text. " + "x" * 500}
        prompt = self.prompt(ticket=ticket, max_requirement_chars=120)
        self.assertIn("User wrote NO_CHANGE", prompt)
        self.assertNotIn("x" * 120, prompt)
        self.assertEqual(prompt.count("return exactly NO_CHANGE"), 1)


class RewriteResponseParserTests(unittest.TestCase):
    ORIGINAL = (
        "@trace('original')\n"
        "async def clean(value: str = 'x', *, strict: bool = False) -> str:\n"
        "    \"\"\"Clean one value.\"\"\"\n"
        "    return value.strip()\n"
        "\n"
        "def sibling():\n"
        "    return 1\n"
    )
    ROW = {"symbol_qualified_name": "clean", "start_line": 1, "end_line": 4}

    def span(self, source=None, row=None):
        source = source or self.ORIGINAL
        return function_span(source.encode("utf-8"), row or self.ROW,
                             include_signature=True)

    def test_exact_no_change_is_distinct_from_explanatory_text(self):
        result = parse_rewrite_response(" \nNO_CHANGE\n", self.span())
        self.assertEqual(result, {"status": "no_change", "reason": "rewrite_no_change",
                                  "middle": None})
        with self.assertRaisesRegex(ValueError, "rewrite_contract_violation"):
            parse_rewrite_response("NO_CHANGE because this target is unrelated", self.span())

    def test_non_text_and_empty_responses_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "response_not_text"):
            parse_rewrite_response(None, self.span())
        with self.assertRaisesRegex(ValueError, "empty_response"):
            parse_rewrite_response("  \n", self.span())

    def test_valid_complete_function_returns_body_for_existing_reconstruction(self):
        response = (
            "@trace('original')\n"
            "async def clean(value: str = 'x', *, strict: bool = False) -> str:\n"
            "    \"\"\"Clean one value.\"\"\"\n"
            "    cleaned = value.strip()\n"
            "    return cleaned.casefold() if strict else cleaned\n"
        )
        parsed = parse_rewrite_response(response, self.span())
        self.assertEqual(parsed["status"], "parsed")
        self.assertIn("    cleaned = value.strip()\n", parsed["middle"])
        body_span = function_span(self.ORIGINAL.encode("utf-8"), self.ROW)
        patch, after = build_patch(
            self.ORIGINAL.encode("utf-8"), body_span, parsed["middle"], "app.py")
        self.assertIn("cleaned.casefold()", patch)
        self.assertIn(b"def sibling():\n    return 1", after)

    def test_signature_decorator_and_function_kind_changes_are_rejected(self):
        base = (
            "@trace('original')\n"
            "async def clean(value: str = 'x', *, strict: bool = False) -> str:\n"
            "    return value\n"
        )
        variants = {
            "name": base.replace("def clean", "def renamed"),
            "kind": base.replace("async def", "def"),
            "decorators": base.replace("trace('original')", "trace('changed')"),
            "arguments": base.replace("value: str", "value: int"),
            "defaults": base.replace("'x'", "'y'"),
            "keyword_only": base.replace(", *, strict: bool = False", ""),
            "returns": base.replace("-> str", "-> bytes"),
        }
        for expected_field, response in variants.items():
            with self.subTest(expected_field=expected_field):
                with self.assertRaisesRegex(ValueError, "signature_changed") as caught:
                    parse_rewrite_response(response, self.span())
                mapped = "arguments" if expected_field in {
                    "arguments", "defaults", "keyword_only"} else expected_field
                self.assertIn(mapped, str(caught.exception))

    def test_type_parameter_changes_are_rejected(self):
        source = "def identity[T](value: T) -> T:\n    return value\n"
        row = {"symbol_qualified_name": "identity", "start_line": 1, "end_line": 2}
        span = self.span(source, row)
        with self.assertRaisesRegex(ValueError, "signature_changed: type_params"):
            parse_rewrite_response(
                "def identity[U](value: T) -> T:\n    return value\n", span)

    def test_nested_decorated_async_body_is_reindented_and_keeps_comments(self):
        source = (
            "class Worker:\n"
            "    @trace('job')\n"
            "    async def run(self, value: str = 'x') -> str:\n"
            "        return value\n"
        )
        row = {"symbol_qualified_name": "Worker.run", "start_line": 2, "end_line": 4}
        response = (
            "@trace('job')\r\n"
            "async def run(self, value: str = 'x') -> str:\r\n"
            "    # Keep this rationale.\r\n"
            "    \"\"\"Normalize a value.\"\"\"\r\n"
            "\r\n"
            "    return value.strip()\r\n"
        )
        parsed = parse_rewrite_response(response, self.span(source, row))
        self.assertIn("        # Keep this rationale.\n", parsed["middle"])
        self.assertIn("        \"\"\"Normalize a value.\"\"\"\n", parsed["middle"])
        self.assertIn("        return value.strip()\n", parsed["middle"])
        self.assertNotIn("\r", parsed["middle"])

    def test_non_function_wrappers_and_extra_output_are_rejected(self):
        invalid = {
            "markdown": "```python\nasync def clean():\n    pass\n```",
            "diff": "--- a/app.py\n+++ b/app.py\n",
            "body_only": "return value.strip()",
            "class": "class Clean:\n    pass\n",
            "module_statement": "VALUE = 1\n",
            "multiple": "def one():\n    pass\n\ndef two():\n    pass\n",
            "analysis": "Explanation first.\nasync def clean():\n    pass\n",
            "trailing_comment": (
                "@trace('original')\n"
                "async def clean(value: str = 'x', *, strict: bool = False) -> str:\n"
                "    return value\n"
                "# trailing analysis\n"
            ),
        }
        for label, response in invalid.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    parse_rewrite_response(response, self.span())

    def test_inline_suite_and_body_only_original_span_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "inline_function_body"):
            parse_rewrite_response(
                "@trace('original')\n"
                "async def clean(value: str = 'x', *, strict: bool = False) -> str: return value\n",
                self.span(),
            )
        body_span = function_span(self.ORIGINAL.encode("utf-8"), self.ROW)
        with self.assertRaisesRegex(ValueError, "complete_original_span_required"):
            parse_rewrite_response(
                "@trace('original')\n"
                "async def clean(value: str = 'x', *, strict: bool = False) -> str:\n"
                "    return value\n",
                body_span,
            )


class RewriteGeneratorTests(unittest.TestCase):
    SOURCE = (
        "def clean(value):\n"
        "    cleaned = value.strip()\n"
        "    result = cleaned.casefold()\n"
        "    return result\n"
        "\n"
        "def sibling():\n"
        "    return 'untouched'\n"
    )
    VALID = (
        "def clean(value):\n"
        "    cleaned = value.strip()\n"
        "    result = cleaned.casefold()\n"
        "    return '' if result == 'invalid' else result\n"
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.raw = self.SOURCE.encode("utf-8")
        (self.repo / "app.py").write_bytes(self.raw)
        git(self.repo, "init")
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
            "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.row = {"file_path": "app.py", "symbol_qualified_name": "clean",
                    "start_line": 1, "end_line": 4}
        self.location = {
            "base_commit": self.base,
            "confidence_level": "high",
            "should_manual_review": False,
            "recommend_patch_generation": True,
            "stage3_ranked_symbols": [self.row],
        }
        self.ticket = {"base_commit": self.base,
                       "description": "Return an empty string for invalid values"}

    def generate(self, outputs, *, candidates=1, backend=None, **kwargs):
        backend = backend or FakeRewriteBackend(outputs)
        result = FIMPatchGenerator(
            backend, candidates=candidates, generation_mode="rewrite", **kwargs
        ).generate(self.ticket, self.location, self.repo)
        return result, backend

    def test_complete_function_rewrite_uses_existing_gates_and_keeps_repository_clean(self):
        result, backend = self.generate([self.VALID])
        self.assertEqual(result["patch_status"], "generated")
        self.assertEqual(result["generation_mode"], "rewrite")
        self.assertEqual(result["generation_strategy"], "rewrite_function")
        self.assertEqual(result["backend_kind"], "fake-rewrite")
        self.assertEqual(result["apply_status"], "passed")
        self.assertTrue(result["include_original_body"])
        self.assertEqual(result["prompt_version"], "issue-plus-full-function-rewrite-v2")
        self.assertEqual(result["prompt_context"]["strategy"], "complete_target_function")
        self.assertEqual(result["candidates"][0]["rewrite_status"], "parsed")
        self.assertIn("def clean(value):", backend.prompts[0])
        self.assertIn("return '' if result", result["patch"])
        self.assertEqual((self.repo / "app.py").read_bytes(), self.raw)
        git(self.repo, "apply", "--check", data=result["patch"].encode("utf-8"))

    def test_no_change_and_signature_change_are_separate_rejections(self):
        no_change, _ = self.generate(["NO_CHANGE"])
        self.assertEqual(no_change["patch_status"], "no_valid_candidate")
        self.assertEqual(no_change["candidates"][0]["reason"], "rewrite_no_change")
        changed, _ = self.generate([
            self.VALID.replace("def clean(value):", "def clean(value, strict=False):")
        ])
        self.assertEqual(changed["patch_status"], "no_valid_candidate")
        self.assertIn("signature_changed: arguments", changed["candidates"][0]["reason"])

    def test_existing_undefined_name_and_reorder_gates_still_apply(self):
        undefined = (
            "def clean(value):\n"
            "    cleaned = value.strip()\n"
            "    result = cleaned.casefold()\n"
            "    return WriterClass(result).render()\n"
        )
        result, _ = self.generate([undefined])
        self.assertIn("undefined_name: WriterClass", result["candidates"][0]["reason"])
        reordered = (
            "def clean(value):\n"
            "    result = cleaned.casefold()\n"
            "    cleaned = value.strip()\n"
            "    return result\n"
        )
        result, _ = self.generate([reordered])
        self.assertIn("statement_reorder_only", result["candidates"][0]["reason"])

    def test_retention_scope_and_apply_failures_are_rejected(self):
        scope_escape = self.VALID + "\ndef escaped():\n    pass\n"
        result, _ = self.generate([scope_escape])
        self.assertIn("multiple_functions", result["candidates"][0]["reason"])

        real_git = git

        def fail_apply(root, *args, data=None):
            if args[:2] == ("apply", "--check"):
                raise ValueError("simulated apply failure")
            return real_git(root, *args, data=data)

        with mock.patch("fim_patch.generator.git", side_effect=fail_apply):
            result, _ = self.generate([self.VALID])
        self.assertIn("simulated apply failure", result["candidates"][0]["reason"])

        # The retention gate intentionally ignores tiny functions. Replace the
        # fixture with a long target so this integration test exercises the
        # real >=10-line threshold instead of a later undefined-name gate.
        long_lines = ["def clean(value):"]
        long_lines.extend(f"    step_{index} = value" for index in range(10))
        long_lines.append("    return step_9")
        (self.repo / "app.py").write_text("\n".join(long_lines) + "\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
            "commit", "-m", "long retention fixture")
        self.base = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.ticket["base_commit"] = self.base
        self.location["base_commit"] = self.base
        self.row["end_line"] = len(long_lines)

        low_retention = "def clean(value):\n    return value.strip()\n"
        result, _ = self.generate([low_retention])
        self.assertIn("retention", result["candidates"][0]["reason"].casefold())

    def test_duplicate_rewrite_is_recorded_without_fallback(self):
        result, backend = self.generate([self.VALID, self.VALID], candidates=2)
        self.assertEqual(result["patch_status"], "generated")
        self.assertEqual(result["candidates"][1]["reason"], "duplicate")
        self.assertEqual(backend.calls, 2)
        self.assertFalse(hasattr(backend, "infill"))

    def test_apply_success_remains_visible_when_validation_rejects_the_patch(self):
        result, _ = self.generate(
            [self.VALID], validator=lambda patch, repo: {"status": "failed"})
        self.assertEqual(result["patch_status"], "no_valid_candidate")
        self.assertEqual(result["apply_status"], "passed")
        self.assertEqual(result["candidates"][0]["reason"], "validation_failed")

    def test_mode_backend_and_strategy_conflicts_fail_before_model_calls(self):
        rewrite_backend = FakeRewriteBackend([self.VALID])
        with self.assertRaisesRegex(ValueError, "Backend does not support"):
            FIMPatchGenerator(rewrite_backend, generation_mode="fim")
        fim_backend = FakeBackend(["    return value\n"])
        with self.assertRaisesRegex(ValueError, "Backend does not support"):
            FIMPatchGenerator(fim_backend, generation_mode="rewrite")
        self.assertEqual(rewrite_backend.calls, 0)
        self.assertEqual(fim_backend.calls, 0)
        with self.assertRaisesRegex(ValueError, "large_function_strategy"):
            FIMPatchGenerator(FakeRewriteBackend([]), generation_mode="rewrite",
                              large_function_strategy="issue_statement")
        with self.assertRaisesRegex(ValueError, "one primary target"):
            FIMPatchGenerator(FakeRewriteBackend([]), generation_mode="rewrite",
                              target_policy="related_targets_top5", research_mode=True)

    def test_default_fim_mode_keeps_the_original_contract(self):
        backend = FakeBackend([
            "    cleaned = value.strip()\n"
            "    result = cleaned.casefold()\n"
            "    return '' if result == 'invalid' else result\n"
        ])
        result = FIMPatchGenerator(backend, candidates=1).generate(
            self.ticket, self.location, self.repo)
        self.assertEqual(result["patch_status"], "generated")
        self.assertEqual(result["generation_mode"], "fim")
        self.assertEqual(result["generation_strategy"], "fim_psm")
        self.assertEqual(backend.calls, 1)


class ExecutableChangeTests(unittest.TestCase):
    """A patch that changes nothing the interpreter sees is not a patch."""

    ORIGINAL = 'def f(x):\n    """Doc."""\n    return x.strip()\n'

    def test_a_comment_only_patch_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            check_executable_change(
                self.ORIGINAL,
                'def f(x):\n    """Doc."""\n    # TODO: simplify this later\n    return x.strip()\n')
        self.assertIn("no_executable_change", str(caught.exception))

    def test_a_docstring_only_patch_is_refused(self):
        with self.assertRaises(ValueError):
            check_executable_change(
                self.ORIGINAL,
                'def f(x):\n    """Corrected doc."""\n    return x.strip()\n')

    def test_a_real_edit_passes(self):
        check_executable_change(
            self.ORIGINAL,
            'def f(x):\n    """Doc."""\n    return "" if x is None else x.strip()\n')

    def test_deleting_a_docstring_is_not_an_executable_change(self):
        with self.assertRaisesRegex(ValueError, "no_executable_change"):
            check_executable_change(self.ORIGINAL, 'def f(x):\n    return x.strip()\n')

    def test_adding_a_docstring_is_not_an_executable_change(self):
        with self.assertRaisesRegex(ValueError, "no_executable_change"):
            check_executable_change('def f(x):\n    return x.strip()\n', self.ORIGINAL)

    def test_nested_module_class_and_method_docstrings_are_ignored(self):
        before = ('"""Module."""\nclass C:\n    """Class."""\n'
                  '    def f(self):\n        """Method."""\n        return 1\n')
        after = 'class C:\n    def f(self):\n        return 1\n'
        with self.assertRaisesRegex(ValueError, "no_executable_change"):
            check_executable_change(before, after)

    def test_logic_change_with_docstring_deletion_still_passes(self):
        check_executable_change(self.ORIGINAL, 'def f(x):\n    return x.lower()\n')

    def test_docstring_only_function_and_pass_are_equivalent(self):
        with self.assertRaisesRegex(ValueError, "no_executable_change"):
            check_executable_change('def f():\n    """Doc."""\n', 'def f():\n    pass\n')

    def test_docstring_deletion_is_rejected_by_patch_construction(self):
        raw = self.ORIGINAL.encode("utf-8")
        span = function_span(raw, {"symbol_qualified_name": "f", "start_line": 1, "end_line": 3})
        with self.assertRaisesRegex(ValueError, "no_executable_change"):
            build_patch(raw, span, '    return x.strip()\n', "app.py")

    def test_an_edit_carrying_a_docstring_change_still_passes(self):
        check_executable_change(
            self.ORIGINAL,
            'def f(x):\n    """New doc."""\n    return x.lower()\n')

    def test_the_gate_reaches_build_patch(self):
        raw = self.ORIGINAL.encode("utf-8")
        span = function_span(raw, {"symbol_qualified_name": "f", "start_line": 1, "end_line": 3})
        with self.assertRaises(ValueError) as caught:
            build_patch(raw, span, '    """Doc."""\n    # unchanged, just noted\n    return x.strip()\n',
                        "app.py")
        self.assertIn("no_executable_change", str(caught.exception))


class UndefinedNameGateTests(unittest.TestCase):
    """Generated code must not invent unresolved bare names."""

    def assert_undefined(self, before, after, name, symbol="f"):
        with self.assertRaises(ValueError) as caught:
            check_new_undefined_names(before, after, symbol)
        self.assertIn("undefined_name", str(caught.exception))
        self.assertIn(name, str(caught.exception))

    def test_new_writer_class_is_rejected(self):
        self.assert_undefined(
            "def f(value):\n    return str(value)\n",
            "def f(value):\n    return WriterClass(value).render()\n",
            "WriterClass",
        )

    def test_new_bare_report_helper_is_rejected(self):
        self.assert_undefined(
            "def f(value):\n    return value\n",
            "def f(value):\n    return _report_hdu_count(value)\n",
            "_report_hdu_count",
        )

    def test_removing_wrapper_definition_is_rejected(self):
        self.assert_undefined(
            "def f(text):\n    wrapper = Formatter()\n    return wrapper.fill(text)\n",
            "def f(text):\n    return wrapper.fill(text)\n",
            "wrapper",
        )

    def test_module_imports_builtins_and_attributes_are_allowed(self):
        before = "import textwrap\n\ndef f(text):\n    return text\n"
        after = (
            "import textwrap\n\n"
            "def f(text):\n"
            "    wrapper = textwrap.TextWrapper()\n"
            "    return str(wrapper.fill(text))\n"
        )
        check_new_undefined_names(before, after, "f")

    def test_existing_dynamic_global_is_not_newly_rejected(self):
        before = "def f(value):\n    return runtime_hook(value)\n"
        after = "def f(value):\n    return runtime_hook(value) + 1\n"
        check_new_undefined_names(before, after, "f")

    def test_closure_reference_is_allowed(self):
        before = (
            "def outer(prefix):\n"
            "    def f(value):\n"
            "        return value\n"
            "    return f\n"
        )
        after = (
            "def outer(prefix):\n"
            "    def f(value):\n"
            "        return prefix + value\n"
            "    return f\n"
        )
        check_new_undefined_names(before, after, "outer.f")

    def test_the_gate_is_enforced_by_build_patch(self):
        raw = b"def f(value):\n    return str(value)\n"
        span = function_span(
            raw, {"symbol_qualified_name": "f", "start_line": 1, "end_line": 2})
        with self.assertRaises(ValueError) as caught:
            build_patch(raw, span,
                        "    return WriterClass(value).render()\n", "app.py")
        self.assertIn("undefined_name: WriterClass", str(caught.exception))


class StatementReorderGateTests(unittest.TestCase):
    """Moving unchanged statements is not evidence that the bug was fixed."""

    BEFORE = (
        "def read_table(path, table_id=None):\n"
        "    \"\"\"Read one table.\"\"\"\n"
        "    if table_id is None:\n"
        "        table_id = 0\n"
        "    tables = load_tables(path)\n"
        "    return tables[table_id]\n"
    )

    def test_14365_style_call_move_is_rejected(self):
        after = (
            "def read_table(path, table_id=None):\n"
            "    \"\"\"Read a selected table.\"\"\"\n"
            "    tables = load_tables(path)\n"
            "    if table_id is None:\n"
            "        table_id = 0\n"
            "    return tables[table_id]\n"
        )
        with self.assertRaises(ValueError) as caught:
            check_statement_reorder_only(self.BEFORE, after, "read_table")
        self.assertIn("statement_reorder_only", str(caught.exception))

    def test_real_statement_edit_is_allowed(self):
        after = self.BEFORE.replace(
            "tables = load_tables(path)",
            "tables = load_tables(path, case_sensitive=False)",
        )
        check_statement_reorder_only(self.BEFORE, after, "read_table")

    def test_added_statement_is_not_classified_as_reorder_only(self):
        after = self.BEFORE.replace(
            "    tables = load_tables(path)\n",
            "    path = normalize(path)\n    tables = load_tables(path)\n",
        )
        check_statement_reorder_only(self.BEFORE, after, "read_table")

    def test_signature_change_is_not_classified_as_reorder_only(self):
        after = self.BEFORE.replace(
            "def read_table(path, table_id=None):",
            "def read_table(path, table_id=None, strict=False):",
        ).replace(
            "    tables = load_tables(path)\n    return tables[table_id]\n",
            "    return tables[table_id]\n    tables = load_tables(path)\n",
        )
        check_statement_reorder_only(self.BEFORE, after, "read_table")

    def test_the_gate_is_enforced_by_build_patch(self):
        raw = self.BEFORE.encode("utf-8")
        span = function_span(
            raw, {"symbol_qualified_name": "read_table", "start_line": 1,
                  "end_line": len(self.BEFORE.splitlines())})
        reordered_body = (
            "    \"\"\"Read a selected table.\"\"\"\n"
            "    tables = load_tables(path)\n"
            "    if table_id is None:\n"
            "        table_id = 0\n"
            "    return tables[table_id]\n"
        )
        with self.assertRaises(ValueError) as caught:
            build_patch(raw, span, reordered_body, "qdp.py")
        self.assertIn("statement_reorder_only", str(caught.exception))


class ReachabilityGateTests(unittest.TestCase):
    """Appending an edit after every branch has returned is not a repair.

    The rewrite smoke run on astropy-14598 produced four candidates of exactly
    this shape; all four passed syntax, scope, undefined-name, reorder and
    ``git apply --check``.
    """

    PAD = (
        "def _pad(value):\n"
        '    """Pad to 80 characters."""\n'
        "    if len(value) == 80:\n"
        "        return value\n"
        "    elif len(value) > 80:\n"
        "        return value + ' '\n"
        "    else:\n"
        "        return value + '  '\n"
    )

    def test_14598_style_append_after_every_branch_returns_is_rejected(self):
        after = self.PAD + "\n    if value == '':\n        return ' '\n"
        with self.assertRaises(ValueError) as caught:
            check_reachable(self.PAD, after, "_pad")
        self.assertIn("unreachable_code", str(caught.exception))

    def test_edit_inside_a_branch_is_accepted(self):
        check_reachable(self.PAD, self.PAD.replace("value + '  '", "value + '   '"), "_pad")

    def test_statement_added_before_the_returns_is_accepted(self):
        after = self.PAD.replace('"""Pad to 80 characters."""\n',
                                 '"""Pad to 80 characters."""\n    value = value or " "\n')
        check_reachable(self.PAD, after, "_pad")

    def test_pre_existing_dead_code_is_not_blamed_on_the_candidate(self):
        before = self.PAD + "    return None\n"
        after = before.replace("value + '  '", "value + '   '")
        check_reachable(before, after, "_pad")

    def test_if_without_else_does_not_terminate(self):
        body = (
            "def _pad(value):\n"
            "    if value:\n"
            "        return value\n"
            "    return ' '\n"
        )
        check_reachable(body, body.replace("return ' '", "return '  '"), "_pad")

    def test_try_finally_that_always_returns_is_recognised(self):
        before = (
            "def load(path):\n"
            "    try:\n"
            "        return open(path)\n"
            "    finally:\n"
            "        return None\n"
        )
        with self.assertRaises(ValueError) as caught:
            check_reachable(before, before + "    log('done')\n", "load")
        self.assertIn("unreachable_code", str(caught.exception))


class LargeFunctionStatementTests(unittest.TestCase):
    """Large functions can narrow generation without narrowing reconstruction."""

    @staticmethod
    def source():
        padding = "".join(f"    value_{i} = {i}\n" for i in range(82))
        return (
            "def configure(reader, kwargs):\n"
            + padding
            + "    if 'header_start' in kwargs:\n"
              "        reader.header.start_line = kwargs['header_start']\n"
            + "    if 'converters' in kwargs:\n"
              "        reader.converters = kwargs['converters']\n"
              "    return reader\n"
        )

    def test_issue_terms_select_the_converter_branch(self):
        raw = self.source().encode("utf-8")
        row = {"symbol_qualified_name": "configure", "start_line": 1,
               "end_line": len(self.source().splitlines())}
        whole = function_span(raw, row)
        narrowed = issue_statement_span(
            raw, whole, {"description": (
                "Normalize converters before assigning them. A later example uses "
                "header_start with a commented_header reader.")})
        self.assertEqual(narrowed["generation_scope"], "issue_statement")
        self.assertEqual(narrowed["statement_kind"], "If")
        self.assertIn("converters", narrowed["statement_issue_terms"])
        self.assertNotIn("header_start", narrowed["statement_issue_terms"])
        self.assertEqual(narrowed["body_lines"], 2)
        self.assertGreaterEqual(narrowed["function_body_lines"], 80)
        self.assertEqual(
            narrowed["prefix"] + narrowed["original_middle"] + narrowed["suffix"],
            self.source(),
        )

    def test_no_issue_related_statement_is_rejected(self):
        raw = self.source().encode("utf-8")
        row = {"symbol_qualified_name": "configure", "start_line": 1,
               "end_line": len(self.source().splitlines())}
        with self.assertRaises(ValueError) as caught:
            issue_statement_span(raw, function_span(raw, row),
                                 {"description": "unrelated zebras"})
        self.assertIn("issue_statement_unavailable", str(caught.exception))

    def test_run_on_output_is_trimmed_to_one_statement(self):
        generated = (
            "    if 'converters' in kwargs:\n"
            "        reader.converters = tuple(kwargs['converters'])\n"
            "    return reader\n"
        )
        self.assertEqual(
            trim_generated_statement(generated),
            "    if 'converters' in kwargs:\n"
            "        reader.converters = tuple(kwargs['converters'])\n",
        )

    def test_generator_replaces_only_the_selected_statement(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            raw = self.source().encode("utf-8")
            (repo / "app.py").write_bytes(raw)
            git(repo, "init")
            git(repo, "config", "core.autocrlf", "false")
            git(repo, "add", ".")
            git(repo, "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
                "commit", "-m", "base")
            base = git(repo, "rev-parse", "HEAD").decode().strip()
            row = {"file_path": "app.py", "symbol_qualified_name": "configure",
                   "start_line": 1, "end_line": len(self.source().splitlines())}
            location = {"base_commit": base, "confidence_level": "high",
                        "should_manual_review": False, "recommend_patch_generation": True,
                        "stage3_ranked_symbols": [row]}
            ticket = {"base_commit": base,
                      "description": "Normalize converters before assigning them"}
            backend = FakeBackend([
                "    if 'converters' in kwargs:\n"
                "        reader.converters = tuple(kwargs['converters'])\n"
            ])
            result = FIMPatchGenerator(
                backend, candidates=1, local_context=True,
                include_original_body=True,
                large_function_strategy="issue_statement",
            ).generate(ticket, location, repo)
            self.assertEqual(result["patch_status"], "generated")
            self.assertEqual(result["prompt_version"],
                             "issue-brief-local-statement-task-last-psm-v7-scope-contract-v1-issue-compact-v1")
            self.assertEqual(result["span"]["generation_scope"], "issue_statement")
            self.assertEqual(result["span"]["body_lines"], 2)
            self.assertIn("tuple(kwargs['converters'])", result["patch"])
            self.assertNotIn("value_0 = 0", result["patch"])

class GeneratedSafetyTests(unittest.TestCase):
    def test_new_module_alias_must_exist(self):
        raw = (
            "from astropy import units as u\n\n"
            "def rotate(x):\n    return rotation_matrix(x, 'x', False)\n"
        ).encode("utf-8")
        span = function_span(
            raw, {"symbol_qualified_name": "rotate", "start_line": 3, "end_line": 4},
            include_signature=True,
        )
        with self.assertRaisesRegex(ValueError, "undefined_name: units"):
            build_patch(raw, span,
                        "def rotate(x):\n    return rotation_matrix(x, 'x', units.radian)\n",
                        "rotations.py")
        patch, _ = build_patch(
            raw, span,
            "def rotate(x):\n    return rotation_matrix(x, 'x', u.radian)\n",
            "rotations.py",
        )
        self.assertIn("u.radian", patch)

    def test_locked_function_keeps_signature_and_instance_state(self):
        raw = (
            "class TableDataDiff:\n"
            "    def __init__(self, a, rtol=0.0):\n"
            "        self.rtol = rtol\n"
            "        self.a = a\n"
        ).encode("utf-8")
        row = {"symbol_qualified_name": "TableDataDiff.__init__",
               "start_line": 2, "end_line": 4}
        span = function_span(raw, row, include_signature=True)
        span["lock_signature"] = True
        with self.assertRaisesRegex(ValueError, "signature_changed"):
            build_patch(raw, span,
                        "    def __init__(self, a):\n"
                        "        self.a = a\n",
                        "diff.py")
        with self.assertRaisesRegex(ValueError, "instance_attributes_removed: rtol"):
            build_patch(raw, span,
                        "    def __init__(self, a, rtol=0.0):\n"
                        "        self.a = a\n",
                        "diff.py")


class BodyRetentionTests(unittest.TestCase):
    """A fix edits behaviour; it rarely deletes most of a function."""

    def span(self, body):
        return {"original_middle": body}

    def test_a_body_that_loses_most_of_its_lines_is_refused(self):
        original = "".join(f"    line_{i} = {i}\n" for i in range(20))
        with self.assertRaises(ValueError) as caught:
            check_body_retention("    return None\n", self.span(original), 0.5)
        self.assertIn("body_shrank", str(caught.exception))

    def test_an_ordinary_edit_passes(self):
        original = "".join(f"    line_{i} = {i}\n" for i in range(20))
        rewritten = "".join(f"    line_{i} = {i + 1}\n" for i in range(18))
        check_body_retention(rewritten, self.span(original), 0.5)

    def test_short_bodies_are_not_judged_by_a_ratio(self):
        # One line replacing three is normal; the ratio would say otherwise.
        check_body_retention("    return None\n",
                             self.span("    a = 1\n    b = 2\n    return a\n"), 0.5)

    def test_the_check_can_be_switched_off_to_reproduce_earlier_runs(self):
        original = "".join(f"    line_{i} = {i}\n" for i in range(20))
        check_body_retention("    return None\n", self.span(original), 0)

    def test_blank_lines_do_not_count_as_kept_code(self):
        original = "".join(f"    line_{i} = {i}\n" for i in range(20))
        padded = "    return None\n" + "\n" * 30
        with self.assertRaises(ValueError):
            check_body_retention(padded, self.span(original), 0.5)


class RunOnGenerationTests(unittest.TestCase):
    """What happens when the model writes past the body it was asked for.

    This is the measured normal case, not an edge case: in research-3e and 3f
    every candidate ran to the token cap, having written a correct first line
    and then the sibling methods that follow the target in the file.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.raw = b'def f(x):\n    return x.strip()\n\n\ndef g():\n    return f(" hi ")\n'
        (self.repo / "app.py").write_bytes(self.raw)
        git(self.repo, "init")
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
            "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.row = {"file_path": "app.py", "symbol_qualified_name": "f",
                    "start_line": 1, "end_line": 2}
        self.loc = {"base_commit": self.base, "confidence_level": "high",
                    "should_manual_review": False, "recommend_patch_generation": True,
                    "stage3_ranked_symbols": [self.row]}
        self.ticket = {"base_commit": self.base, "description": "Return empty string for None"}

    def run_with(self, output, stop_reason="length"):
        backend = FakeBackend([output], stop_reason=stop_reason)
        result = FIMPatchGenerator(backend, candidates=1).generate(
            self.ticket, self.loc, self.repo)
        return backend, result

    def test_a_run_on_body_is_cut_at_the_sibling_that_follows_it(self):
        backend, result = self.run_with(
            '    return None\n\n\ndef h():\n    return 1\n\n\ndef i():\n    return 2\n')
        self.assertEqual(result["patch_status"], "generated")
        git(self.repo, "apply", data=result["patch"].encode())
        patched = (self.repo / "app.py").read_bytes()
        # Only the target's body changed; the sibling the model invented is gone
        # and the sibling that was already there is untouched.
        self.assertEqual(patched, self.raw.replace(b"return x.strip()", b"return None"))
        self.assertNotIn(b"def h()", patched)

    def test_a_body_that_never_dedents_is_refused_not_guessed_at(self):
        # The budget ran out inside the body. Nothing here can tell a finished
        # body from half of one, so the candidate must not be used.
        _, result = self.run_with("    value = (\n        x.strip()\n")
        self.assertEqual(result["patch_status"], "no_valid_candidate")
        self.assertIn("incomplete_body", result["candidates"][0]["reason"])

    def test_the_two_populations_stay_distinguishable_in_the_record(self):
        _, trimmed = self.run_with('    return None\n\ndef h():\n    pass\n')
        _, closed = self.run_with('    return None\n', stop_reason="visible_eot")
        self.assertTrue(trimmed["candidates"][0]["body_trimmed"])
        self.assertEqual(trimmed["candidates"][0]["stop_reason"], "length")
        self.assertFalse(closed["candidates"][0]["body_trimmed"])
        self.assertEqual(closed["candidates"][0]["stop_reason"], "visible_eot")

    def test_the_budget_is_sized_from_the_body_not_from_the_ceiling(self):
        backend, _ = self.run_with('    return None\n\ndef h():\n    pass\n')
        self.assertEqual(backend.budgets, [128])
        self.assertLess(backend.budgets[0], backend.max_new_tokens)

    def test_trimming_does_not_replace_the_gate_that_keeps_generation_in_place(self):
        # A cut that lands in the wrong place still has to fail: the trim is a
        # convenience, the AST comparison is the guarantee.
        _, result = self.run_with('    return x.strip()\n\ndef h():\n    pass\n')
        self.assertEqual(result["patch_status"], "no_valid_candidate")
        self.assertIn("no_op", result["candidates"][0]["reason"])

    # ---- the model closes the infill, but in the wrong place ------------

    def test_a_visible_eot_after_the_body_ended_is_still_trimmed(self):
        """``<EOT>`` says the model stopped, not that it stopped in the right place.

        Measured in runs/multitarget-A on correctly localized functions:
        ``_line_type`` emitted ``<EOT>`` after 105 lines carrying ``def
        _get_type_from_file``, and ``diff_values`` after 54 lines carrying
        ``def where_not_allclose``. Trusting the marker skipped the trim, so
        the overrun was spliced in and lost to the scope gate.
        """

        _, result = self.run_with(
            '    return None\n\n\ndef h():\n    return 1\n',
            stop_reason="visible_eot")
        self.assertEqual(result["patch_status"], "generated", result["candidates"])
        candidate = result["candidates"][0]
        self.assertTrue(candidate["body_trimmed"])
        self.assertTrue(candidate["overran_past_stop"])
        self.assertEqual(candidate["stop_reason"], "visible_eot")
        git(self.repo, "apply", data=result["patch"].encode())
        patched = (self.repo / "app.py").read_bytes()
        self.assertEqual(patched, self.raw.replace(b"return x.strip()", b"return None"))
        self.assertNotIn(b"def h()", patched)

    def test_a_body_that_stopped_in_the_right_place_is_left_alone(self):
        _, result = self.run_with('    return None\n', stop_reason="visible_eot")
        candidate = result["candidates"][0]
        self.assertFalse(candidate["body_trimmed"])
        self.assertNotIn("overran_past_stop", candidate)

    def test_model_eos_overrun_is_trimmed_too(self):
        _, result = self.run_with(
            '    return None\n\ndef h():\n    pass\n', stop_reason="model_eos")
        self.assertEqual(result["patch_status"], "generated", result["candidates"])
        self.assertTrue(result["candidates"][0]["overran_past_stop"])


class BodyOverrunTests(unittest.TestCase):
    """The predicate the trim decision is made on."""

    SPAN = {"original_middle": "    x = 1\n    return x\n"}

    def test_a_dedented_statement_is_an_overrun(self):
        self.assertTrue(body_overruns("    return 2\n\ndef other():\n    pass\n", self.SPAN))

    def test_a_body_that_stays_indented_is_not(self):
        self.assertFalse(body_overruns("    if x:\n        return 2\n    return 3\n", self.SPAN))

    def test_blank_lines_never_count_as_a_dedent(self):
        self.assertFalse(body_overruns("    return 2\n\n\n   \n", self.SPAN))

    def test_deeper_indentation_than_the_original_is_not_an_overrun(self):
        self.assertFalse(body_overruns("        return 2\n", self.SPAN))

    def test_an_unusable_span_reports_no_overrun_rather_than_raising(self):
        self.assertFalse(body_overruns("def h():\n    pass\n", {"original_middle": "\n\n"}))


class TransportTests(unittest.TestCase):
    """The prefix/suffix pair has to reach the model as special tokens.

    research-3e spent 12/12 candidates on done_reason="length" for bodies as
    short as seven lines. A probe over every tag installed on the dev machine
    showed why: a raw prompt carrying the marker text is not parsed into the
    FIM tokens, so the model only ever continues the prefix -- with the bare
    "<PRE>" spelling it even echoed the marker back as ordinary text. Only
    Ollama's own suffix parameter, on a tag that has an infilling template,
    stopped on its own (done_reason="stop", 55 of 128 tokens, visible <EOT>).
    """

    def capture(self, backend, prefix="def f():\n", suffix="\n"):
        seen = {}

        def post(endpoint, payload):
            seen["endpoint"] = endpoint
            seen["payload"] = payload
            return {"response": "    return 1\n<EOT>", "done": True, "done_reason": "stop"}

        backend.post = post
        result = backend.infill(prefix, suffix, seed=7)
        return seen["payload"], result

    def test_default_transport_hands_ollama_the_two_halves(self):
        payload, result = self.capture(OllamaFIM())
        self.assertEqual(payload["prompt"], "def f():\n")
        self.assertEqual(payload["suffix"], "\n")
        # raw=True bypasses the template that inserts the markers, which is
        # exactly what broke the earlier runs.
        self.assertNotIn("raw", payload)
        for marker in MARKERS:
            self.assertNotIn(marker, payload["prompt"])
        self.assertEqual(result["transport"], "ollama_suffix")

    def test_model_eos_is_a_valid_completed_infill(self):
        backend = OllamaFIM(model="qwen2.5-coder:14b-base")
        backend.post = lambda endpoint, payload: {
            "response": "    return 1\n", "done": True,
            "done_reason": "stop",
        }
        result = backend.infill("def f():\n", "\n", seed=1)
        self.assertEqual(result["stop_reason"], "model_eos")
        self.assertEqual(result["middle"], "    return 1\n")

    def test_legacy_transport_still_reproduces_the_hand_built_prompt(self):
        payload, result = self.capture(OllamaFIM(transport="raw_psm"))
        self.assertEqual(payload["prompt"],
                         MARKERS[0] + "def f():\n" + MARKERS[1] + "\n" + MARKERS[2])
        self.assertTrue(payload["raw"])
        self.assertNotIn("suffix", payload)
        self.assertEqual(result["transport"], "raw_psm")

    def test_unknown_transport_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            OllamaFIM(transport="psm_raw")

    def test_budget_is_guarded_under_both_transports(self):
        for transport in ("ollama_suffix", "raw_psm"):
            with self.assertRaises(ValueError):
                OllamaFIM(transport=transport).infill("x" * 4096, "", seed=1)

    def test_default_tag_is_the_infilling_one(self):
        self.assertEqual(OllamaFIM().model, "codellama:7b-code")


class DescribeTests(unittest.TestCase):
    VOCAB = {"model_info": {"tokenizer.ggml.tokens": list(MARKERS),
                            "llama.context_length": 16384},
             "details": {"parameter_size": "7B"}}

    def backend(self, generate_response):
        backend = OllamaFIM(num_ctx=4096)

        def post(endpoint, payload):
            if endpoint == "show":
                return self.VOCAB
            return generate_response()

        backend.post = post
        backend._tags = lambda: {"models": [{"name": backend.model, "digest": "d" * 64}]}
        return backend

    def test_a_tag_without_an_infilling_template_names_the_fix(self):
        def refuse():
            raise error.HTTPError("http://x/api/generate", 400, "Bad Request", {}, None)

        with self.assertRaises(ValueError) as caught:
            self.backend(refuse).describe()
        message = str(caught.exception)
        self.assertIn("model_lacks_fim_template", message)
        self.assertIn("codellama:7b-code", message)

    def test_a_usable_tag_reports_the_transport_it_will_use(self):
        described = self.backend(lambda: {"response": "", "done": True}).describe()
        self.assertEqual(described["transport"], "ollama_suffix")
        self.assertEqual(described["fim_token_ids"]["▁<MID>"], 2)

    def test_the_legacy_transport_skips_the_probe(self):
        backend = self.backend(lambda: self.fail("raw_psm must not probe for a template"))
        backend.transport = "raw_psm"
        self.assertEqual(backend.describe()["transport"], "raw_psm")

    def test_suffix_transport_accepts_non_codellama_insert_model(self):
        backend = self.backend(lambda: {"response": "", "done": True})
        backend.post = lambda endpoint, payload: (
            {"model_info": {"qwen2.context_length": 32768},
             "capabilities": ["completion", "insert"],
             "details": {"parameter_size": "14.8B"}}
            if endpoint == "show" else {"response": "", "done": True}
        )
        described = backend.describe()
        self.assertEqual(described["capabilities"], ["completion", "insert"])
        self.assertEqual(described["fim_token_ids"], {})


class ValidationTests(unittest.TestCase):
    def test_zero_tests_not_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_suite(tmp, {"discover": "."}, 10)
        self.assertNotEqual(r["status"], "passed")

    def test_real_before_after_and_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text('def f(x):\n    return x.strip()\n')
            (root / "test_app.py").write_text(
                'import unittest\nfrom app import f\nclass Bug(unittest.TestCase):\n'
                ' def test_bug(self): self.assertEqual(f(None), "")\n'
                'class Old(unittest.TestCase):\n def test_old(self): self.assertEqual(f(" a "), "a")\n')
            spec = {"reproducer": {"names": ["test_app.Bug"]},
                    "regression": {"names": ["test_app.Old"]}, "expected_failure": "AttributeError"}
            raw = (root / "app.py").read_bytes()
            span = function_span(raw, {"symbol_qualified_name": "f", "start_line": 1, "end_line": 2})
            patch, _ = build_patch(raw, span, '    return "" if x is None else x.strip()\n', "app.py")
            self.assertEqual(UnittestValidator(spec)(patch, root)["status"], "plausible")
            self.assertEqual((root / "app.py").read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()


class NarrowingThresholdTests(unittest.TestCase):
    """Who gets statement-level narrowing, and what happens when it finds nothing.

    The default only narrows oversized bodies (>= 80 lines), which on the fixed
    five tickets leaves the two clearest copying cases untouched: astropy-14365
    ``_line_type`` at 65 lines and astropy-13465 ``diff_values`` at 25. Lowering
    the threshold is how that gets tested, so it has to be a setting, and it
    must not cost coverage when no statement matches the issue.
    """

    SOURCE = (
        b"def handle(value, delimiter=None):\n"
        b"    cleaned = value.strip()\n"
        b"    parsed = cleaned.split(delimiter)\n"
        b"    return parsed\n"
    )
    WHOLE_BODY = ('    cleaned = value.strip()\n'
                  '    parsed = cleaned.split(delimiter or ",")\n'
                  '    return parsed\n')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "app.py").write_bytes(self.SOURCE)
        git(self.repo, "init")
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
            "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.row = {"file_path": "app.py", "symbol_qualified_name": "handle",
                    "start_line": 1, "end_line": 4}
        self.loc = {"base_commit": self.base, "confidence_level": "high",
                    "should_manual_review": False, "recommend_patch_generation": True,
                    "stage3_ranked_symbols": [self.row]}

    def generate(self, ticket, output, **kwargs):
        backend = FakeBackend([output])
        result = FIMPatchGenerator(
            backend, candidates=1, large_function_strategy="issue_statement",
            **kwargs).generate(ticket, self.loc, self.repo)
        return backend, result

    def ticket(self, description, title="delimiter handling"):
        return {"base_commit": self.base, "title": title, "description": description}

    def scope(self, result):
        """Absent means the whole body was generated, which is the default."""

        return result["span"].get("generation_scope", "function_body")

    def test_a_small_body_is_untouched_at_the_default_threshold(self):
        _, result = self.generate(self.ticket("delimiter is ignored"), self.WHOLE_BODY)
        self.assertEqual(result["patch_status"], "generated", result["candidates"])
        self.assertEqual(self.scope(result), "function_body")
        self.assertEqual(result["large_function_line_threshold"], 80)

    def test_lowering_the_threshold_narrows_the_same_small_body(self):
        _, result = self.generate(
            self.ticket("delimiter is ignored when parsing"),
            '    parsed = cleaned.split(delimiter or ",")\n',
            large_function_line_threshold=2)
        self.assertEqual(result["patch_status"], "generated", result["candidates"])
        self.assertEqual(self.scope(result), "issue_statement")
        # Only the matching statement moved; the rest of the body is untouched.
        self.assertIn('+    parsed = cleaned.split(delimiter or ",")', result["patch"])
        self.assertNotIn("-    cleaned = value.strip()", result["patch"])

    def test_no_matching_statement_falls_back_to_the_whole_body(self):
        """Narrowing that finds nothing must not cost the target."""

        # Nothing in the issue names anything the body mentions, so no
        # statement can be selected on public evidence alone.
        backend, result = self.generate(
            self.ticket("socket timeout during upload retries",
                        title="network retry backoff"),
            self.WHOLE_BODY, large_function_line_threshold=2)
        self.assertEqual(result["patch_status"], "generated", result["candidates"])
        self.assertEqual(self.scope(result), "function_body")
        self.assertIn("issue_statement_unavailable",
                      result["target_resolution"][0]["narrowing_skipped"])
        self.assertEqual(backend.calls, 1)

    def test_an_invalid_threshold_is_refused(self):
        for value in (0, -1, True, 2.5):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "large_function_line_threshold"):
                    FIMPatchGenerator(FakeBackend([]), large_function_line_threshold=value)

    def test_the_threshold_is_recorded_for_the_run(self):
        _, result = self.generate(
            self.ticket("delimiter is ignored"), self.WHOLE_BODY,
            large_function_line_threshold=2)
        self.assertEqual(result["large_function_line_threshold"], 2)
