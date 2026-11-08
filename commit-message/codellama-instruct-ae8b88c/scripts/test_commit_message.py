"""Live Ollama demo: generate messages for fixture patches and validate structure."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from commit_message import generate_commit_message


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run fixture patches through Code Llama-Instruct")
    parser.add_argument("--model", default=None, help="Ollama model name")
    parser.add_argument("--ollama-url", default=None, help="Ollama base URL")
    parser.add_argument("--timeout", type=float, default=None, help="Request timeout in seconds")
    args = parser.parse_args(argv)
    cases = json.loads((ROOT / "tests/fixtures/commit_message/cases.json").read_text(encoding="utf-8"))
    failures = 0
    for case in cases:
        print(f"=== {case['name']} ===\nPatch:")
        print(case["patch"] or "<empty>")
        result = generate_commit_message(case["patch"], model=args.model,
                                         ollama_url=args.ollama_url, timeout=args.timeout)
        print("Commit message:")
        print(result["commit_message"] or "<none>")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        passed = result["success"] == case["success"]
        if case["success"]:
            passed &= result["changed_files"] == case["files"]
            passed &= result["generation_method"] == "code_llama_instruct_zero_shot"
            passed &= len(result["commit_message"].splitlines()) == 1
        else:
            passed &= result["error"] == case["error"]
        failures += not passed
        print("PASS (matches expected result)" if passed else "FAIL")
    print(f"{len(cases) - failures}/{len(cases)} scenarios passed")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
