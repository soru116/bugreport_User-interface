from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from scripts.freeze_swebench_full_stage1_v2 import validate_prediction_parity
from scripts.run_swebench_full_stage1_experiment import (
    build_shard_command,
    sha256_file,
    validate_freeze_manifest,
)
from scripts.run_swebench_lite_fault_localization import build_parser


class Stage1V2FreezeGuardTest(unittest.TestCase):
    def test_prediction_only_flag_is_available(self) -> None:
        args = build_parser().parse_args(["--prediction-only"])
        self.assertTrue(args.prediction_only)

    def test_final_holdout_shard_command_cannot_receive_gold(self) -> None:
        args = Namespace(
            repo_cache_dir="repos",
            output_dir="output",
            index_cache_dir="indexes",
            import_graph_cache_dir=None,
            call_graph_cache_dir=None,
            candidate_file_k=20,
            checkpoint_every=1,
            method="e4-c-call-outgoing-top3",
            fallback_index_cache_dir=[],
            no_resume=True,
            allow_sbert_download=False,
            repository_size_groups="groups.json",
        )
        command = build_shard_command(
            {"tickets": Path("tickets.jsonl"), "output": Path("run")},
            args,
        )
        self.assertIn("--prediction-only", command)
        self.assertNotIn("--gold", command)

    def test_prediction_parity_requires_identical_top20_order_and_scores(self) -> None:
        candidates = [
            {"file_path": f"src/file_{index}.py", "retrieval_score": 1.0 / (index + 1)}
            for index in range(20)
        ]
        current = [{"ticket_id": f"t-{index}", "stage1_candidate_files": candidates} for index in range(30)]
        reference = list(current)
        result = validate_prediction_parity(current, reference)
        self.assertEqual(result["exact_top20_file_order_matches"], 30)
        changed = json.loads(json.dumps(reference))
        changed[0]["stage1_candidate_files"][0]["file_path"] = "changed.py"
        with self.assertRaisesRegex(ValueError, "changed E4 Top-20"):
            validate_prediction_parity(current, changed)

    def test_runner_rejects_output_directory_outside_frozen_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tickets = root / "tickets.jsonl"
            gold = root / "gold.jsonl"
            tickets.write_text("{}\n", encoding="utf-8")
            gold.write_text("{}\n", encoding="utf-8")
            authorized = root / "authorized"
            freeze = root / "freeze.json"
            freeze.write_text(
                json.dumps(
                    {
                        "protocol": "swebench-full-stage1-protocol-v1",
                        "status": "frozen_before_holdout",
                        "selected_method_label": "e4-c-call-outgoing-top3",
                        "frozen_holdout_artifacts": {
                            "tickets": {"sha256": sha256_file(tickets)},
                            "gold": {"sha256": sha256_file(gold)},
                        },
                        "implementation": {},
                        "authorized_output_dir": str(authorized),
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "output directory"):
                validate_freeze_manifest(
                    freeze,
                    method_label="e4-c-call-outgoing-top3",
                    tickets_path=tickets,
                    gold_path=gold,
                    output_dir=root / "different",
                )

    def test_runner_accepts_frozen_paths_before_first_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tickets = root / "tickets.jsonl"
            gold = root / "gold.jsonl"
            tickets.write_text("{}\n", encoding="utf-8")
            gold.write_text("{}\n", encoding="utf-8")
            authorized = root / "authorized"
            freeze = root / "freeze.json"
            freeze.write_text(
                json.dumps(
                    {
                        "protocol": "swebench-full-stage1-protocol-v1",
                        "status": "frozen_before_holdout",
                        "selected_method_label": "e4-c-call-outgoing-top3",
                        "frozen_holdout_artifacts": {
                            "tickets": {"sha256": sha256_file(tickets)},
                            "gold": {"sha256": sha256_file(gold)},
                        },
                        "implementation": {},
                        "authorized_output_dir": str(authorized),
                    }
                ),
                encoding="utf-8",
            )
            result = validate_freeze_manifest(
                freeze,
                method_label="e4-c-call-outgoing-top3",
                tickets_path=tickets,
                gold_path=gold,
                output_dir=authorized,
            )
            self.assertEqual(result["status"], "frozen_before_holdout")

    def test_runner_rejects_holdout_after_final_record_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tickets = root / "tickets.jsonl"
            gold = root / "gold.jsonl"
            record = root / "final_record.json"
            tickets.write_text("{}\n", encoding="utf-8")
            gold.write_text("{}\n", encoding="utf-8")
            record.write_text("{}\n", encoding="utf-8")
            authorized = root / "authorized"
            freeze = root / "freeze.json"
            freeze.write_text(
                json.dumps(
                    {
                        "protocol": "swebench-full-stage1-protocol-v1",
                        "status": "frozen_before_holdout",
                        "selected_method_label": "e7-a-e6-development-baseline",
                        "frozen_holdout_artifacts": {
                            "tickets": {"sha256": sha256_file(tickets)},
                            "gold": {"sha256": sha256_file(gold)},
                        },
                        "implementation": {},
                        "authorized_output_dir": str(authorized),
                        "final_record_path": str(record),
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FileExistsError, "already evaluated"):
                validate_freeze_manifest(
                    freeze,
                    method_label="e7-a-e6-development-baseline",
                    tickets_path=tickets,
                    gold_path=gold,
                    output_dir=authorized,
                )


if __name__ == "__main__":
    unittest.main()
