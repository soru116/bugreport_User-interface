"""Stage 3: P1-P5 priority with the bug-priority-drone (PRI) recall-balanced model.

PRI's feature pipeline is reused as-is: the new tickets are written as rows of the cleaned
CSV format, ``scripts/build_features.py --mode transform`` builds DRONE/REP- features against
the cleaned history (``--history-input``) with the fitted encoders of the training feature
directory, and the saved model bundle predicts through ``analyze_priority_errors.
predict_model_or_bundle``.  Only strictly earlier history rows feed a ticket's temporal,
author and related-report factors, so the placeholder label on the new rows never leaks.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from integration.common import StageUnavailable, import_module_from_path, relative_to_workspace, stage_result
from integration.config import IntegrationConfig
from modules.priority_classifier import PriorityClassifier


SEVERITIES = {"blocker", "critical", "major", "normal", "minor", "trivial", "enhancement"}
SEVERITY_ALIASES = {"重大": "critical", "高": "major", "中": "normal", "低": "minor", "high": "major", "medium": "normal", "low": "minor"}
PRIORITY_NAMES = {1: "P1 Blocker", 2: "P2 Critical", 3: "P3 Major", 4: "P4 Minor", 5: "P5 Trivial"}
FACTOR_COLUMNS = {
    "textual": [],
    "temporal": ["past_7d_count", "past_30d_count", "past_30d_same_or_higher_severity_count"],
    "author": ["author_past_count", "author_past_mean_priority"],
    "related_report": ["related_top1_priority", "related_top5_avg_priority", "related_similarity_top1"],
    "severity": ["severity"],
    "product_component": ["product_past_30d_count", "component_past_30d_mean_priority", "component_past_30d_count"],
}


def _severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = SEVERITY_ALIASES.get(text, text)
    return text if text in SEVERITIES else "unknown"


def _timestamp(ticket: dict[str, Any], *, history_is_tz_aware: bool) -> str:
    """Format the ticket time like the history CSV; pandas drops mixed naive/aware rows."""

    parsed: datetime | None = None
    for key in ("created_at", "creation_time"):
        value = str(ticket.get(key) or "").strip()
        if value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                break
            except ValueError:
                continue
    if parsed is None:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if history_is_tz_aware:
        return parsed.strftime("%Y-%m-%d %H:%M:%S+00:00")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _history_is_tz_aware(history_csv: Path) -> bool:
    import pandas as pd

    sample = pd.read_csv(history_csv, usecols=["creation_time"], nrows=20)["creation_time"].dropna().astype(str)
    return any(value.endswith("Z") or "+" in value[10:] or value[10:].count("-") > 0 for value in sample)


class TeamPriorityPredictor:
    MODULE_LABEL = "bug-priority-drone：DRONE/REP- 特徵 + recall-balanced 模型"

    def __init__(self, config: IntegrationConfig) -> None:
        self.config = config
        self.paths = config.priority_paths()
        self.scripts_dir = Path(self.paths["root"]) / "scripts"

    @property
    def implementation(self) -> str:
        return relative_to_workspace(self.scripts_dir, self.config.workspace_root)

    def check_artifacts(self) -> list[str]:
        missing = []
        for key in ("model_path", "reference_feature_dir", "history_csv"):
            path = self.paths.get(key)
            if not path:
                missing.append(f"priority.{key} 未設定")
            elif not Path(path).exists():
                missing.append(f"priority.{key} 不存在：{path}")
        for name in ("build_features.py", "analyze_priority_errors.py", "clean_bug_reports.py"):
            if not (self.scripts_dir / name).is_file():
                missing.append(f"找不到 PRI 程式：{self.scripts_dir / name}")
        return missing

    def predict_many(self, tickets: list[dict[str, Any]], duplicate_candidates: list[list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
        started = time.perf_counter()
        duplicate_candidates = duplicate_candidates or [[] for _ in tickets]
        missing = self.check_artifacts()
        if missing:
            reason = "；".join(missing)
            return [self._fallback(ticket, cands, started, reason) for ticket, cands in zip(tickets, duplicate_candidates)]
        try:
            predictions = self._run_model(tickets)
        except StageUnavailable as exc:
            return [self._fallback(ticket, cands, started, str(exc)) for ticket, cands in zip(tickets, duplicate_candidates)]
        results = []
        for ticket, prediction in zip(tickets, predictions):
            results.append(
                stage_result(
                    stage="priority",
                    status="done",
                    module=self.MODULE_LABEL,
                    implementation=self.implementation,
                    output=prediction,
                    details={
                        "model_path": str(self.paths["model_path"]),
                        "reference_feature_dir": str(self.paths["reference_feature_dir"]),
                        "history_csv": str(self.paths["history_csv"]),
                        "reference_metrics": "PRI README natural_holdout：Accuracy 0.7266、Macro-F1 0.7265、Off-by-one 0.9045",
                        "batch_size": len(tickets),
                    },
                    warnings=prediction.pop("_warnings", []),
                    started=started,
                )
            )
        return results

    def predict(self, ticket: dict[str, Any], duplicate_candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return self.predict_many([ticket], [duplicate_candidates or []])[0]

    # ------------------------------------------------------------------
    def _target_rows(self, tickets: list[dict[str, Any]], clean_module, *, tz_aware: bool) -> list[dict[str, Any]]:
        rows = []
        for index, ticket in enumerate(tickets):
            summary = clean_module.clean_text(ticket.get("title") or "")
            description_parts = [
                ticket.get("description"),
                " ".join(ticket.get("steps_to_reproduce") or []),
                ticket.get("actual_behavior"),
                ticket.get("error_message"),
            ]
            description = clean_module.clean_text(" ".join(str(part) for part in description_parts if part))
            text = clean_module.truncate_text(f"{summary} {description}".strip())
            rows.append(
                {
                    "id": f"__integration_target_{index}__{ticket.get('ticket_id') or ''}",
                    "summary": summary,
                    "description": description,
                    # Placeholder label: build_features only reads strictly earlier rows' labels.
                    "priority": "P3",
                    "severity": _severity(ticket.get("severity")),
                    "product": str(ticket.get("product") or "unknown").strip().lower() or "unknown",
                    "component": str(ticket.get("component") or "unknown").strip().lower() or "unknown",
                    "creator": str(ticket.get("reporter") or ticket.get("creator") or "unknown").strip().lower(),
                    "creation_time": _timestamp(ticket, history_is_tz_aware=tz_aware),
                    "dupe_of": "",
                    "priority_label": "Medium Priority",
                    "priority_num": 3,
                    "text": text,
                    "text_len": len(text),
                }
            )
        return rows

    def _run_model(self, tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            import joblib
            import pandas as pd
            from scipy.sparse import load_npz
        except ImportError as exc:
            raise StageUnavailable(f"缺少 PRI 相依套件：{exc}") from exc

        clean_module = import_module_from_path("pri_clean_bug_reports", self.scripts_dir / "clean_bug_reports.py")
        analyze_module = import_module_from_path("pri_analyze_priority_errors", self.scripts_dir / "analyze_priority_errors.py")

        settings = self.config.priority
        with tempfile.TemporaryDirectory(prefix="pri_integration_") as tmp:
            tmp_path = Path(tmp)
            target_csv = tmp_path / "targets.csv"
            history_csv = Path(self.paths["history_csv"])
            target_rows = self._target_rows(tickets, clean_module, tz_aware=_history_is_tz_aware(history_csv))
            pd.DataFrame(target_rows).to_csv(target_csv, index=False, encoding="utf-8-sig")

            # A ticket must never be its own history: when a real report from the history CSV is
            # imported as a new ticket, its labelled copy would otherwise feed the related-report and
            # author factors (build_features only de-duplicates by the internal target id).
            target_ids = {str(ticket.get("ticket_id") or "").strip() for ticket in tickets} - {""}
            try:
                history_ids = pd.read_csv(history_csv, usecols=["id"], dtype=str)["id"].str.strip()
                overlap = bool(target_ids) and bool(history_ids.isin(target_ids).any())
            except ValueError:  # history without an id column: nothing to match against
                overlap = False
            if overlap or (settings.history_max_rows and settings.history_max_rows > 0):
                history = pd.read_csv(history_csv)
                if overlap:
                    history = history[~history["id"].astype(str).str.strip().isin(target_ids)]
                if settings.history_max_rows and settings.history_max_rows > 0:
                    history["creation_time"] = pd.to_datetime(history["creation_time"], errors="coerce")
                    history = history.dropna(subset=["creation_time"]).sort_values("creation_time").tail(settings.history_max_rows)
                history_csv = tmp_path / "history_window.csv"
                history.to_csv(history_csv, index=False, encoding="utf-8-sig")

            feature_dir = tmp_path / "features"
            command = [
                sys.executable,
                str(self.scripts_dir / "build_features.py"),
                "--mode",
                "transform",
                "--input",
                str(target_csv),
                "--history-input",
                str(history_csv),
                "--reference-feature-dir",
                str(self.paths["reference_feature_dir"]),
                "--feature-dir",
                str(feature_dir),
            ]
            if self.paths.get("rep_weights_json") and Path(self.paths["rep_weights_json"]).exists():
                command += ["--rep-weights-json", str(self.paths["rep_weights_json"])]
            command += [str(item) for item in settings.extra_build_args]
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(self.scripts_dir),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=int(settings.timeout_seconds),
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                raise StageUnavailable(f"build_features.py 超過 {settings.timeout_seconds} 秒未完成") from exc
            if completed.returncode != 0:
                tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-6:]
                raise StageUnavailable("build_features.py 執行失敗：" + " | ".join(tail))

            X = load_npz(feature_dir / "X_features.npz")
            meta = pd.read_csv(feature_dir / "feature_meta.csv")
            try:
                bundle = joblib.load(self.paths["model_path"])
            except Exception as exc:  # pickles from another scikit-learn version may not load
                import sklearn

                raise StageUnavailable(
                    f"無法載入 PRI 模型（{type(exc).__name__}: {exc}）。這台電腦的 scikit-learn 是 {sklearn.__version__}，"
                    "可能和訓練時的版本不同；請安裝訓練時的版本，例如 pip install scikit-learn==1.8.0"
                ) from exc
            labels = analyze_module.predict_model_or_bundle(bundle, X)
            probabilities = self._probabilities(bundle, X)

        id_order = {row_id: position for position, row_id in enumerate(meta["id"].astype(str))}
        results = []
        for index, ticket in enumerate(tickets):
            row_id = f"__integration_target_{index}__{ticket.get('ticket_id') or ''}"
            position = id_order.get(row_id)
            if position is None:
                raise StageUnavailable(f"build_features.py 沒有輸出 ticket {ticket.get('ticket_id')} 的特徵列")
            label = int(labels[position])
            row = meta.iloc[position]
            factors = {
                name: {column: _jsonable(row[column]) for column in columns if column in row.index}
                for name, columns in FACTOR_COLUMNS.items()
            }
            factors["textual"] = {"summary_terms": str(row.get("summary", ""))[:160]}
            confidence = None
            distribution = {}
            if probabilities is not None:
                distribution = {f"P{int(cls)}": round(float(prob), 4) for cls, prob in probabilities[position].items()}
                confidence = distribution.get(f"P{label}")
            results.append(
                {
                    "predicted_priority": f"P{label}",
                    "priority_name": PRIORITY_NAMES.get(label, f"P{label}"),
                    "confidence": confidence,
                    "class_probabilities": distribution,
                    "model": str(bundle.get("model_type") if isinstance(bundle, dict) else type(bundle).__name__),
                    "feature_profile": str(bundle.get("feature_profile", "")) if isinstance(bundle, dict) else "",
                    "factor_values": factors,
                    "reported_priority": ticket.get("priority"),
                    "_warnings": [] if confidence is not None else ["此模型不提供機率，信心值無法計算。"],
                }
            )
        return results

    @staticmethod
    def _probabilities(bundle: Any, X) -> list[dict[int, float]] | None:
        if not isinstance(bundle, dict):
            return None
        model = bundle.get("base_model") or bundle.get("model")
        if model is None or not hasattr(model, "predict_proba"):
            return None
        try:
            matrix = model.predict_proba(X)
        except Exception:
            return None
        classes = list(getattr(model, "classes_", range(1, matrix.shape[1] + 1)))
        return [{int(cls): float(prob) for cls, prob in zip(classes, row)} for row in matrix]

    def _fallback(self, ticket: dict[str, Any], candidates: list[dict[str, Any]], started: float, reason: str) -> dict[str, Any]:
        if not self.config.priority.fallback_to_baseline:
            return stage_result(
                stage="priority",
                status="failed",
                module=self.MODULE_LABEL,
                implementation=self.implementation,
                error=reason,
                started=started,
            )
        baseline = PriorityClassifier().predict(ticket, candidates)
        baseline["reported_priority"] = ticket.get("priority")
        return stage_result(
            stage="priority",
            status="fallback",
            module="主系統 DRONE/GRAY 固定權重 baseline（備援）",
            implementation="bug_tracking_llm_system/src/modules/priority_classifier.py",
            output=baseline,
            warnings=["PRI 訓練模型未載入，改用主系統固定權重公式；此結果不是訓練模型的預測。"],
            fallback_reason=reason,
            started=started,
        )


def _jsonable(value: Any) -> Any:
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
    except ImportError:
        pass
    if isinstance(value, float) and value != value:
        return None
    return value
