from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from utils.symbol_localization import normalize_repository_path


SYMBOL_EVALUATION_ITEM_SCHEMA_VERSION = "symbol-evaluation-item-v1"
SYMBOL_MATCH_SCHEMA_VERSION = "symbol-match-v1"
SYMBOL_EVALUATION_SCHEMA_VERSION = "symbol-evaluation-v1"
SYMBOL_EVALUATION_K_VALUES = (1, 3, 5)

_TRUSTED_SYMBOL_ID = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class SymbolEvaluationItemV1:
    """Minimal file-qualified symbol identity accepted by the evaluator."""

    file_path: str
    qualified_name: str
    symbol_kind: str
    symbol_id: str = ""
    schema_version: str = SYMBOL_EVALUATION_ITEM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "file_path", normalize_repository_path(self.file_path))
        object.__setattr__(self, "qualified_name", normalize_qualified_name(self.qualified_name))
        object.__setattr__(self, "symbol_kind", str(self.symbol_kind or "").strip().casefold())
        object.__setattr__(self, "symbol_id", str(self.symbol_id or "").strip().casefold())
        if self.schema_version != SYMBOL_EVALUATION_ITEM_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SYMBOL_EVALUATION_ITEM_SCHEMA_VERSION!r}."
            )
        if not self.qualified_name:
            raise ValueError("qualified_name must be non-empty.")
        if not self.symbol_kind:
            raise ValueError("symbol_kind must be non-empty.")
        if self.symbol_id and not is_trusted_symbol_id(self.symbol_id):
            raise ValueError("symbol_id must be empty or a sha256-prefixed 64-character digest.")
        if (self.qualified_name == "<module>") != (self.symbol_kind == "module"):
            raise ValueError("<module> and symbol_kind='module' must be used together.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SymbolEvaluationItemV1":
        return cls(
            file_path=str(payload.get("file_path") or payload.get("file") or ""),
            qualified_name=str(
                payload.get("qualified_name")
                or payload.get("symbol_qualified_name")
                or payload.get("symbol_name")
                or ""
            ),
            symbol_kind=str(payload.get("symbol_kind") or payload.get("kind") or ""),
            symbol_id=str(payload.get("symbol_id") or ""),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "file_path": self.file_path,
            "qualified_name": self.qualified_name,
            "symbol_kind": self.symbol_kind,
            "symbol_id": self.symbol_id,
        }


@dataclass(frozen=True, slots=True)
class SymbolMatchV1:
    """Result of comparing one prediction with one gold symbol."""

    exact: bool
    relaxed: bool
    reason: str
    schema_version: str = SYMBOL_MATCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SYMBOL_MATCH_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SYMBOL_MATCH_SCHEMA_VERSION!r}.")
        if self.exact and not self.relaxed:
            raise ValueError("Every exact match must also be a relaxed match.")
        if self.reason not in {
            "exact_symbol_id",
            "exact_file_kind_qualified_name",
            "relaxed_qualified_name",
            "relaxed_suffix",
            "relaxed_leaf",
            "module_kind_mismatch",
            "no_match",
        }:
            raise ValueError(f"Unsupported symbol match reason: {self.reason!r}.")


def is_trusted_symbol_id(value: str) -> bool:
    return bool(_TRUSTED_SYMBOL_ID.fullmatch(str(value or "").strip().casefold()))


def normalize_qualified_name(value: str) -> str:
    text = str(value or "").strip()
    if "::" in text:
        text = text.split("::", maxsplit=1)[1]
    text = text.split(":", maxsplit=1)[0]
    return text.replace("#", ".").strip().lstrip(".")


def match_symbol(
    prediction: SymbolEvaluationItemV1,
    gold: SymbolEvaluationItemV1,
) -> SymbolMatchV1:
    """Apply the v1 exact contract, followed by the legacy relaxed diagnostic.

    When both records carry trusted IDs, the IDs are authoritative. Otherwise
    exact matching uses the complete normalized (file, kind, qualified-name)
    tuple. Relaxed matching intentionally ignores file and kind so historical
    suffix/leaf behaviour remains visible as a diagnostic, never as a primary
    result. Module symbols cannot relax-match non-module symbols.
    """

    if prediction.symbol_id and gold.symbol_id:
        if prediction.symbol_id == gold.symbol_id:
            return SymbolMatchV1(True, True, "exact_symbol_id")
    elif (
        prediction.file_path,
        prediction.symbol_kind,
        prediction.qualified_name,
    ) == (
        gold.file_path,
        gold.symbol_kind,
        gold.qualified_name,
    ):
        return SymbolMatchV1(True, True, "exact_file_kind_qualified_name")

    if (prediction.symbol_kind == "module") != (gold.symbol_kind == "module"):
        return SymbolMatchV1(False, False, "module_kind_mismatch")

    left = prediction.qualified_name
    right = gold.qualified_name
    if left == right:
        return SymbolMatchV1(False, True, "relaxed_qualified_name")
    if left.endswith("." + right) or right.endswith("." + left):
        return SymbolMatchV1(False, True, "relaxed_suffix")
    if symbol_leaf_name(left) == symbol_leaf_name(right):
        return SymbolMatchV1(False, True, "relaxed_leaf")
    return SymbolMatchV1(False, False, "no_match")


def symbol_leaf_name(value: str) -> str:
    return normalize_qualified_name(value).rsplit(".", maxsplit=1)[-1]


def evaluate_ranked_symbols(
    ranked_predictions: Sequence[SymbolEvaluationItemV1],
    gold_symbols: Sequence[SymbolEvaluationItemV1],
    *,
    k_values: Sequence[int] = SYMBOL_EVALUATION_K_VALUES,
) -> dict[str, Any]:
    """Return per-ticket exact and relaxed ranks, hits, and multi-gold recall."""

    ordered_k = tuple(sorted({int(k) for k in k_values if int(k) > 0}))
    exact_rank = _first_rank(ranked_predictions, gold_symbols, exact=True)
    relaxed_rank = _first_rank(ranked_predictions, gold_symbols, exact=False)
    return {
        "schema_version": SYMBOL_EVALUATION_SCHEMA_VERSION,
        "gold_symbol_count": len(gold_symbols),
        "prediction_symbol_count": len(ranked_predictions),
        "exact": _rank_metrics(
            ranked_predictions,
            gold_symbols,
            first_rank=exact_rank,
            exact=True,
            k_values=ordered_k,
        ),
        "relaxed_diagnostic": _rank_metrics(
            ranked_predictions,
            gold_symbols,
            first_rank=relaxed_rank,
            exact=False,
            k_values=ordered_k,
        ),
    }


def evaluation_contract() -> dict[str, Any]:
    """Machine-readable description stored beside every formal metric report."""

    return {
        "schema_version": SYMBOL_EVALUATION_SCHEMA_VERSION,
        "item_schema_version": SYMBOL_EVALUATION_ITEM_SCHEMA_VERSION,
        "primary_match": "exact",
        "exact_identity_precedence": [
            "symbol_id_when_both_trusted",
            "normalized_file_path+symbol_kind+qualified_name",
        ],
        "relaxed_diagnostic": [
            "qualified_name",
            "dot_boundary_suffix",
            "leaf_name",
        ],
        "relaxed_ignores_file_and_kind": True,
        "module_rule": "<module> only matches symbol_kind=module; module never matches non-module",
        "missing_prediction_rule": "retain in end-to-end denominator as a miss",
        "multi_gold_rule": "hit uses any gold; recall averages matched unique gold / all gold",
        "candidate_rule": "exact Hit/Recall@10/30 only when a gold file is in Stage-2 Top-5",
        "coverage_rule": "LLM valid/fallback/timeout rates use LLM-eligible rows",
        "paired_rule": "compare stage3_retrieval_symbols with stage3_ranked_symbols per ticket",
        "bootstrap_rule": "paired mean delta, 2000 resamples, deterministic seeds",
    }


def _first_rank(
    predictions: Sequence[SymbolEvaluationItemV1],
    gold_symbols: Sequence[SymbolEvaluationItemV1],
    *,
    exact: bool,
) -> int | None:
    for rank, prediction in enumerate(predictions, start=1):
        for gold in gold_symbols:
            result = match_symbol(prediction, gold)
            if result.exact if exact else result.relaxed:
                return rank
    return None


def _rank_metrics(
    predictions: Sequence[SymbolEvaluationItemV1],
    gold_symbols: Sequence[SymbolEvaluationItemV1],
    *,
    first_rank: int | None,
    exact: bool,
    k_values: Sequence[int],
) -> dict[str, Any]:
    hits: dict[str, int] = {}
    recalls: dict[str, float] = {}
    for k in k_values:
        hits[str(k)] = int(first_rank is not None and first_rank <= k)
        matched_gold = {
            index
            for index, gold in enumerate(gold_symbols)
            if any(
                (
                    match_symbol(prediction, gold).exact
                    if exact
                    else match_symbol(prediction, gold).relaxed
                )
                for prediction in predictions[:k]
            )
        }
        recalls[str(k)] = (
            len(matched_gold) / len(gold_symbols) if gold_symbols else 0.0
        )
    return {
        "first_relevant_rank": first_rank,
        "reciprocal_rank": 1.0 / first_rank if first_rank else 0.0,
        "hit_at": hits,
        "recall_at": recalls,
    }
