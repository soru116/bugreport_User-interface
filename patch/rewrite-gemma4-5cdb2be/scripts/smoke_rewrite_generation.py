"""Run a real Ollama full-function rewrite against a synthetic Git fixture."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fim_patch.backend import OllamaRewrite  # noqa: E402
from fim_patch.generator import FIMPatchGenerator  # noqa: E402


SOURCE = (
    "def normalize_name(value):\n"
    "    \"\"\"Normalize a user name for lookup.\"\"\"\n"
    "    cleaned = value.strip()\n"
    "    lowered = cleaned.lower()\n"
    "    if not lowered:\n"
    "        return \"\"\n"
    "    return lowered\n"
)


class RecordingOllamaRewrite(OllamaRewrite):
    """Keep only request payloads needed to prove the transport contract."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.generate_payloads = []

    def post(self, endpoint, payload):
        if endpoint == "generate":
            self.generate_payloads.append(payload)
        return super().post(endpoint, payload)


def git(root: Path, *args: str, data: bytes | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args], input=data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return completed.stdout.decode("utf-8", errors="replace").strip()


def run_smoke(args) -> dict:
    with tempfile.TemporaryDirectory(prefix="rewrite-smoke-") as temp:
        repo = Path(temp)
        (repo / "names.py").write_text(SOURCE, encoding="utf-8")
        git(repo, "init")
        git(repo, "config", "core.autocrlf", "false")
        git(repo, "add", ".")
        git(repo, "-c", "user.name=Smoke", "-c",
            "user.email=smoke@example.invalid", "commit", "-m", "base")
        base = git(repo, "rev-parse", "HEAD")
        row = {
            "file_path": "names.py",
            "symbol_qualified_name": "normalize_name",
            "start_line": 1,
            "end_line": len(SOURCE.splitlines()),
        }
        ticket = {
            "base_commit": base,
            "title": "Handle a missing user name",
            "description": (
                "Add an executable guard before value.strip(): when value is None, "
                "return an empty string. Preserve trimming and lowercase normalization "
                "for every string value."
            ),
        }
        location = {
            "base_commit": base,
            "confidence_level": "high",
            "should_manual_review": False,
            "recommend_patch_generation": True,
            "stage3_ranked_symbols": [row],
        }
        backend = RecordingOllamaRewrite(
            model=args.model, url=args.url, num_ctx=args.num_ctx,
            max_new_tokens=args.max_new_tokens, timeout=args.timeout,
        )
        result = FIMPatchGenerator(
            backend, candidates=args.candidates, seed=args.seed,
            generation_mode="rewrite",
        ).generate(ticket, location, repo)
        patch_applies = False
        if result.get("patch"):
            git(repo, "apply", "--check", "--whitespace=nowarn",
                data=result["patch"].encode("utf-8"))
            patch_applies = True
        candidate_rows = []
        for candidate in result.get("candidates", []):
            candidate_rows.append({
                "candidate_id": candidate.get("candidate_id"),
                "status": candidate.get("status"),
                "reason": candidate.get("reason"),
                "rewrite_status": candidate.get("rewrite_status"),
                "stop_reason": candidate.get("stop_reason"),
                "raw_response": candidate.get("raw_response"),
            })
        payloads = backend.generate_payloads
        return {
            "model": result.get("model"),
            "generation_mode": result.get("generation_mode"),
            "prompt_version": result.get("prompt_version"),
            "patch_status": result.get("patch_status"),
            "apply_status": result.get("apply_status"),
            "patch_applies": patch_applies,
            "request_count": len(payloads),
            "requests_have_suffix": any("suffix" in payload for payload in payloads),
            "candidate_count": len(candidate_rows),
            "candidates": candidate_rows,
            "patch": result.get("patch", ""),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="codellama:7b-instruct")
    parser.add_argument("--url", default="http://127.0.0.1:11434")
    parser.add_argument("--candidates", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-ctx", type=int, default=4096)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    report = run_smoke(args)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if (report["patch_status"] == "generated"
                 and report["patch_applies"]
                 and not report["requests_have_suffix"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
