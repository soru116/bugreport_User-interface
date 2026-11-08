"""Generate a commit-message draft from a patch with Code Llama-Instruct."""
import argparse
import json
from pathlib import Path
import sys

from .generator import failure, generate_commit_message, normalize_patch, PatchError


def main(argv=None):
    parser = argparse.ArgumentParser(description="Draft a commit message with Code Llama-Instruct via Ollama.")
    parser.add_argument("input", help="UTF-8 unified diff file, JSON file, or - for stdin")
    parser.add_argument("--json-input", action="store_true", help="Read a patch object instead of raw diff")
    parser.add_argument("--show-patch", action="store_true", help="Print normalized input patch before result")
    parser.add_argument("--model", help="Ollama model name (default: codellama:7b-instruct)")
    parser.add_argument("--ollama-url", help="Ollama base URL (default: http://127.0.0.1:11434)")
    parser.add_argument("--timeout", type=float, help="Model request timeout in seconds (default: 120)")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8-sig")
        data = json.loads(raw) if args.json_input else raw
        if args.show_patch:
            print("=== Patch ===")
            print(normalize_patch(data))
            print("=== Commit message result ===")
        result = generate_commit_message(data, model=args.model, ollama_url=args.ollama_url,
                                         timeout=args.timeout)
    except (OSError, UnicodeError, ValueError, PatchError) as exc:
        result = failure(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
