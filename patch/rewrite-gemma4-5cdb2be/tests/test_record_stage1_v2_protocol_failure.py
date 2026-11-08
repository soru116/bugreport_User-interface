from __future__ import annotations

import unittest

from scripts.record_swebench_full_stage1_v2_protocol_failure import Counter


class RecordStage1V2ProtocolFailureTest(unittest.TestCase):
    def test_candidate_distribution_identifies_short_rows(self) -> None:
        counts = Counter([20, 20, 18, 7])
        exact_rows = counts.get(20, 0)
        short_rows = sum(count for size, count in counts.items() if size < 20)
        self.assertEqual(exact_rows, 2)
        self.assertEqual(short_rows, 2)


if __name__ == "__main__":
    unittest.main()
