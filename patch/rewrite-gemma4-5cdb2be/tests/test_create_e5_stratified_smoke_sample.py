from __future__ import annotations

import unittest

from scripts.create_e5_stratified_smoke_sample import select_ticket_ids


class CreateE5StratifiedSmokeSampleTest(unittest.TestCase):
    def test_selection_is_deterministic_and_balanced(self) -> None:
        mapping = {
            "repo/small": "small",
            "repo/medium": "medium",
            "repo/large": "large",
        }
        tickets = [
            {"ticket_id": f"{repository}-{index}", "repo": repository}
            for repository in mapping
            for index in range(5)
        ]
        first_ids, first_counts = select_ticket_ids(
            tickets,
            mapping,
            per_group=3,
            seed=42,
        )
        second_ids, second_counts = select_ticket_ids(
            list(reversed(tickets)),
            mapping,
            per_group=3,
            seed=42,
        )
        self.assertEqual(first_ids, second_ids)
        self.assertEqual(first_counts, {"small": 3, "medium": 3, "large": 3})
        self.assertEqual(second_counts, first_counts)
        self.assertEqual(len(first_ids), 9)

    def test_insufficient_group_is_rejected(self) -> None:
        mapping = {
            "repo/small": "small",
            "repo/medium": "medium",
            "repo/large": "large",
        }
        tickets = [
            {"ticket_id": f"{repository}-1", "repo": repository}
            for repository in mapping
        ]
        with self.assertRaisesRegex(ValueError, "requires 2"):
            select_ticket_ids(tickets, mapping, per_group=2, seed=42)


if __name__ == "__main__":
    unittest.main()
