"""Stage 2: duplicate candidates using the bug-duplicate-detection (DUP) package.

The best documented DUP configuration is reproduced by calling the package's own functions:
SBERT title/content models (top1-mined) -> ``combine_similarity_scores(weighted:0.6,0.4)``
-> ``apply_metadata_rerank_scores(component:0.02)`` -> ``apply_stable_rerank_scores``
(TF-IDF 0.2, metadata 0.05, agreement 0.05).  Without a trained SBERT model directory the
DUP TF-IDF detector is used and the stage is reported as a fallback.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any

from integration.common import StageUnavailable, import_package_from_root, relative_to_workspace, stage_result
from integration.config import IntegrationConfig


HISTORY_DISPLAY_KEYS = (
    "product",
    "component",
    "severity",
    "priority",
    "assignee",
    "status",
    "resolution",
    "created_at",
    "creation_time",
)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(item) for item in value if str(item).strip())
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def read_history_rows(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.is_file():
        return []
    if path.suffix.lower() == ".jsonl":
        rows = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
        return rows
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            value = value.get("tickets") or value.get("records") or []
        return [row for row in value if isinstance(row, dict)]
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin")


def sbert_dir_problem(model_dir: Path | None) -> str:
    """Explain why an SBERT model directory cannot be used; empty string when it looks complete."""

    if not model_dir:
        return "設定中沒有 duplicate.sbert_model_dir（訓練好的 title_model/、content_model/）"
    for part in ("title_model", "content_model"):
        folder = Path(model_dir) / part
        if not folder.is_dir():
            return f"SBERT 模型資料夾不完整：找不到 {folder}"
        if not any((folder / name).is_file() for name in WEIGHT_FILES):
            return (
                f"{folder} 只有設定檔，沒有權重檔（model.safetensors 或 pytorch_model.bin）；"
                "權重檔被 .gitignore 排除，需要從訓練的電腦複製或重新執行 train-sbert"
            )
    return ""


class TeamDuplicateDetector:
    MODULE_LABEL = "bug-duplicate-detection：SBERT + stable rerank"

    def __init__(self, config: IntegrationConfig, *, sbert_detector: Any | None = None) -> None:
        self.config = config
        self.root = config.subproject("duplicate")
        self.source_root = self.root / "src"
        self._sbert_detector = sbert_detector
        self._sbert_error = ""

    @property
    def implementation(self) -> str:
        return relative_to_workspace(self.source_root / "duplicate_ticket_detection", self.config.workspace_root)

    def package(self):
        package = import_package_from_root("duplicate_ticket_detection", self.source_root)
        # Sub-modules used below; importing them also validates numpy/scikit-learn are installed.
        for name in ("dataset", "tfidf_detector", "sbert_detector", "rerank", "stable_rerank", "cli"):
            import_package_from_root(f"duplicate_ticket_detection.{name}", self.source_root)
        return package

    def history_rows(self, extra_paths: list[Path] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        rows: list[dict[str, Any]] = []
        sources: list[str] = []
        for path in [*self.config.historical_tickets_paths, *(extra_paths or [])]:
            loaded = read_history_rows(path)
            if loaded:
                rows.extend(loaded)
                sources.append(f"{path}（{len(loaded)} 筆）")
        return rows, sources

    def sbert_detector(self):
        if self._sbert_detector is not None:
            return self._sbert_detector
        model_dir = self.config.duplicate.sbert_model_dir
        problem = sbert_dir_problem(model_dir)
        if problem:
            self._sbert_error = problem
            return None
        from duplicate_ticket_detection.sbert_detector import SbertDuplicateDetector

        try:
            self._sbert_detector = SbertDuplicateDetector.load(model_dir, combine=self.config.duplicate.combine)
        except Exception as exc:  # sentence-transformers missing or model unreadable
            self._sbert_error = f"無法載入 SBERT 模型：{exc}"
            return None
        return self._sbert_detector

    def detect(
        self,
        structured_ticket: dict[str, Any],
        *,
        extra_history_paths: list[Path] | None = None,
        exclude_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            self.package()
        except StageUnavailable as exc:
            return stage_result(
                stage="duplicate",
                status="failed",
                module=self.MODULE_LABEL,
                implementation=self.implementation,
                error=str(exc),
                started=started,
            )

        import numpy as np
        from duplicate_ticket_detection import cli as dup_cli
        from duplicate_ticket_detection.dataset import DEFAULT_CONTENT_COLUMNS, _row_to_ticket
        from duplicate_ticket_detection.rerank import RerankConfig, apply_metadata_rerank_scores, parse_field_weights
        from duplicate_ticket_detection.stable_rerank import StableRerankConfig, apply_stable_rerank_scores
        from duplicate_ticket_detection.tfidf_detector import TfidfDuplicateDetector, combine_similarity_scores

        settings = self.config.duplicate
        history, sources = self.history_rows(extra_history_paths)
        query_id = str(structured_ticket.get("ticket_id") or "__query__")
        excluded = {query_id, *(exclude_ids or set())}
        # Only compare within the ticket's project; history rows without a project are shared.
        query_project = str(structured_ticket.get("project") or "").strip()
        other_project_rows = 0

        candidates = []
        raw_by_id: dict[str, dict[str, Any]] = {}
        for row in history:
            row_project = str(row.get("project") or "").strip()
            if query_project and row_project and row_project != query_project:
                other_project_rows += 1
                continue
            try:
                record = _row_to_ticket({key: _stringify(value) for key, value in row.items()}, content_columns=DEFAULT_CONTENT_COLUMNS)
            except ValueError:
                continue
            if record.ticket_id in excluded or record.ticket_id in raw_by_id:
                continue
            candidates.append(record)
            raw_by_id[record.ticket_id] = row

        if not candidates:
            return stage_result(
                stage="duplicate",
                status="skipped",
                module=self.MODULE_LABEL,
                implementation=self.implementation,
                output={"top_k_candidates": [], "needs_review": False, "is_duplicate": False},
                warnings=["沒有可比對的歷史 Ticket 資料庫，請在設定檔 historical_tickets_paths 指定，或於工作台匯入歷史資料。"],
                started=started,
            )

        query_row = {key: _stringify(value) for key, value in structured_ticket.items()}
        query_row["ticket_id"] = query_id
        query = _row_to_ticket(query_row, content_columns=DEFAULT_CONTENT_COLUMNS)

        tfidf_detector = TfidfDuplicateDetector(combine=settings.tfidf_combine).fit(candidates)
        tfidf_title, tfidf_content = dup_cli._tfidf_score_matrices(tfidf_detector, [query], candidates)

        warnings: list[str] = []
        detector = self.sbert_detector()
        if detector is not None:
            title_scores, content_scores = dup_cli._sbert_score_matrices(detector, [query], candidates)
            combine = settings.combine
            method = "sbert"
        else:
            title_scores, content_scores = tfidf_title, tfidf_content
            combine = settings.tfidf_combine
            method = "tfidf"
            warnings.append(f"未載入 SBERT 模型，改用 DUP 的 TF-IDF baseline。原因：{self._sbert_error}")

        model_scores = combine_similarity_scores(title_scores, content_scores, combine)
        steps = [f"{method} {combine}"]
        ordering = np.array(model_scores, dtype=float, copy=True)
        field_weights = parse_field_weights(settings.metadata_rerank_fields) if settings.metadata_rerank_fields else ()
        if field_weights:
            rerank_config = RerankConfig(field_weights=field_weights, candidate_pool=settings.candidate_pool)
            ordering = apply_metadata_rerank_scores([query], candidates, ordering, title_scores, content_scores, rerank_config)
            steps.append("metadata rerank " + ",".join(f"{name}:{weight}" for name, weight in field_weights))
        tfidf_scores = combine_similarity_scores(tfidf_title, tfidf_content, combine)
        if settings.stable_rerank and method == "sbert":
            stable_config = StableRerankConfig(
                tfidf_weight=settings.stable_tfidf_weight,
                metadata_weight=settings.stable_metadata_weight,
                agreement_weight=settings.stable_agreement_weight,
                candidate_pool=settings.candidate_pool,
            )
            ordering = apply_stable_rerank_scores([query], candidates, ordering, tfidf_scores, stable_config)
            steps.append(
                f"stable rerank tfidf={stable_config.tfidf_weight} metadata={stable_config.metadata_weight} "
                f"agreement={stable_config.agreement_weight}"
            )

        row_scores = ordering[0]
        ranked_indices = [int(index) for index in np.argsort(-row_scores) if np.isfinite(row_scores[index])]
        top_indices = ranked_indices[: max(1, int(settings.top_k))]
        top_candidates = []
        for rank, index in enumerate(top_indices, start=1):
            record = candidates[index]
            raw = raw_by_id.get(record.ticket_id, {})
            top_candidates.append(
                {
                    "rank": rank,
                    "ticket_id": record.ticket_id,
                    "title": record.title,
                    "ranking_score": round(float(row_scores[index]), 4),
                    "similarity": round(float(model_scores[0, index]), 4),
                    "title_similarity": round(float(title_scores[0, index]), 4),
                    "content_similarity": round(float(content_scores[0, index]), 4),
                    "tfidf_similarity": round(float(tfidf_scores[0, index]), 4),
                    "description": record.content[:600],
                    **{key: _stringify(raw.get(key)) for key in HISTORY_DISPLAY_KEYS if raw.get(key) not in (None, "")},
                    "metadata_match": {
                        field: bool(
                            str(structured_ticket.get(field) or "").strip().lower()
                            and str(structured_ticket.get(field) or "").strip().lower()
                            == str(raw.get(field) or "").strip().lower()
                        )
                        for field in ("product", "component", "severity")
                    },
                }
            )

        best = top_candidates[0] if top_candidates else None
        best_score = float(best["similarity"]) if best else 0.0
        auto_threshold = settings.auto_duplicate_threshold
        review_threshold = settings.review_threshold
        is_duplicate = bool(best and auto_threshold is not None and best_score >= float(auto_threshold))
        if is_duplicate:
            needs_review = False
            decision_policy = f"最高相似度 {best_score:.3f} ≥ 自動判定門檻 {auto_threshold}"
        elif review_threshold is not None:
            needs_review = bool(best and best_score >= float(review_threshold))
            decision_policy = (
                f"最高相似度 {best_score:.3f} ≥ 複核門檻 {review_threshold}，需工程師確認"
                if needs_review
                else f"最高相似度 {best_score:.3f} < 複核門檻 {review_threshold}，視為非重複"
            )
        else:
            needs_review = bool(best)
            decision_policy = "半自動模式（DUP README 建議）：Top-k 候選一律交由工程師確認"

        output = {
            "is_duplicate": is_duplicate,
            "duplicate_of": best["ticket_id"] if is_duplicate and best else None,
            "similarity_score": round(best_score, 4),
            "needs_review": needs_review,
            "decision_policy": decision_policy,
            "top_k_candidates": top_candidates,
            "history_size": len(candidates),
            "project_scope": query_project,
            "other_project_rows_skipped": other_project_rows,
        }
        details = {
            "method": method,
            "pipeline": steps,
            "history_sources": sources,
            "sbert_model_dir": str(settings.sbert_model_dir or ""),
            "reference_metrics": "DUP README 最佳設定：MAP 0.6884、Top-1 0.6639、Top-10 hit 0.9180、MRR 0.7535",
        }
        return stage_result(
            stage="duplicate",
            status="done" if method == "sbert" else "fallback",
            module=self.MODULE_LABEL if method == "sbert" else "bug-duplicate-detection：TF-IDF baseline（SBERT 未載入）",
            implementation=self.implementation,
            output=output,
            warnings=warnings,
            fallback_reason="" if method == "sbert" else self._sbert_error,
            details=details,
            started=started,
        )
