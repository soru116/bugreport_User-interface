"""Trusted unittest validation in separate source copies (not a security sandbox)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .generator import git


RUNNER = r'''
import io,json,sys,unittest
spec=json.loads(sys.argv[1])
loader=unittest.TestLoader()
suite=(loader.discover(spec['discover']) if 'discover' in spec
       else loader.loadTestsFromNames(spec['names']))
stream=io.StringIO()
r=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
report={'tests_run':r.testsRun,'failures':r.failures,'errors':r.errors,
        'skipped':r.skipped,'expected_failures':r.expectedFailures,
        'unexpected_successes':r.unexpectedSuccesses,'log':stream.getvalue()}
for key in ('failures','errors','skipped','expected_failures','unexpected_successes'):
    report[key]=[str(item) for item in report[key]]
with open(sys.argv[2],'w',encoding='utf-8') as f: json.dump(report,f)
'''


def run_suite(root, spec, timeout):
    with tempfile.TemporaryDirectory(prefix="fim_test_report_") as tmp:
        report_path = Path(tmp) / "report.json"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        try:
            run = subprocess.run([sys.executable, "-c", RUNNER, json.dumps(spec), str(report_path)],
                                 cwd=root, capture_output=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "tests_run": 0}
        if run.returncode or not report_path.exists():
            return {"status": "runner_error", "tests_run": 0,
                    "stderr": run.stderr.decode("utf-8", errors="replace")[-4000:]}
        report = json.loads(report_path.read_text(encoding="utf-8"))
        bad = any(report[k] for k in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"))
        report["status"] = "passed" if report["tests_run"] > 0 and not bad else "failed"
        return report


def validate_spec(spec):
    if not isinstance(spec, dict) or not spec.get("expected_failure"):
        raise ValueError("Test spec requires expected_failure text for the baseline bug")
    for key in ("reproducer", "regression"):
        suite = spec.get(key, {})
        names, discover = suite.get("names"), suite.get("discover")
        if bool(names) == bool(discover):
            raise ValueError(f"{key}: specify exactly one of names or discover")
        if names and (not isinstance(names, list) or not all(isinstance(n, str) and n for n in names)):
            raise ValueError("names must be nonempty unittest names")
        if discover:
            p = Path(discover)
            if not isinstance(discover, str) or p.is_absolute() or ".." in p.parts or ":" in discover:
                raise ValueError("discover must be a relative directory")
    return spec


class UnittestValidator:
    def __init__(self, spec, timeout=60):
        self.spec = validate_spec(spec)
        self.timeout = timeout

    def __call__(self, patch, repo_path):
        with tempfile.TemporaryDirectory(prefix="fim_validate_") as tmp:
            root = Path(tmp) / "repo"
            # Refuse symlinks/junctions before copying so tests cannot follow an
            # unexpected repository link into the source workspace.
            for folder, dirs, files in os.walk(repo_path, followlinks=False):
                dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "venv")]
                for name in dirs + files:
                    p = Path(folder) / name
                    if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
                        raise ValueError("Linked files/directories unsupported by local test runner")
            shutil.copytree(repo_path, root, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", ".pytest_cache", ".venv", "venv"))
            before = run_suite(root, self.spec["reproducer"], self.timeout)
            baseline_regression = run_suite(root, self.spec["regression"], self.timeout)
            report = {"status": "failed", "before": before, "baseline_regression": baseline_regression}
            expected = self.spec["expected_failure"]
            if (before.get("status") != "failed" or before.get("tests_run", 0) == 0 or
                    expected not in before.get("log", "") or
                    any(before.get(k) for k in ("skipped", "expected_failures")) or
                    baseline_regression.get("status") != "passed"):
                report["reason"] = "baseline_not_verified"
                return report
            # Fresh copy prevents baseline tests' file writes influencing patch tests.
            patched = Path(tmp) / "patched"
            shutil.copytree(repo_path, patched, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", ".pytest_cache", ".venv", "venv"))
            git(patched, "apply", "--check", "--whitespace=nowarn", data=patch.encode("utf-8"))
            git(patched, "apply", "--whitespace=nowarn", data=patch.encode("utf-8"))
            after = run_suite(patched, self.spec["reproducer"], self.timeout)
            regression = run_suite(patched, self.spec["regression"], self.timeout)
            report.update(after=after, regression=regression)
            if (after.get("status") == "passed" and regression.get("status") == "passed"
                    and after["tests_run"] == before["tests_run"]
                    and regression["tests_run"] == baseline_regression["tests_run"]):
                report["status"] = "plausible"
            return report
