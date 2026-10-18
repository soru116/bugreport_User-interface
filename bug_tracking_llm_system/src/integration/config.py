"""Configuration for the integrated ticket workflow.

All artifact locations (trained models, feature directories, history datasets, repositories)
live in one JSON file so the team can point the workflow at the artifacts that exist on the
machine running it.  Relative paths are resolved against the directory of the config file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT as MAIN_PROJECT_ROOT


SUBPROJECT_DIRS = {
    "tojson": "ToJson",
    "duplicate": "bug-duplicate-detection",
    "priority": "bug-priority-drone",
    "fault_localization": "fault_localization_feature_files",
}


def default_workspace_root() -> Path:
    """Return the multi-project root that contains ToJson/, bug-duplicate-detection/, ..."""

    return MAIN_PROJECT_ROOT.parent


def resolve_subproject(workspace_root: Path, key: str) -> Path:
    """Locate a sub-project folder, tolerating the nested ``name/name`` checkout layout."""

    name = SUBPROJECT_DIRS[key]
    direct = workspace_root / name
    nested = direct / name
    if (nested / "README.md").exists():
        return nested
    return direct


@dataclass(slots=True)
class ExtractionSettings:
    enabled: bool = True
    ollama_url: str = "http://localhost:11434/api/generate"
    model: str = "codellama:7b-instruct"
    timeout_seconds: int = 180
    prompt_mode: str = "zero_shot"
    fallback_to_rule_normalization: bool = True


@dataclass(slots=True)
class DuplicateSettings:
    sbert_model_dir: Path | None = None
    combine: str = "weighted:0.6,0.4"
    tfidf_combine: str = "max"
    metadata_rerank_fields: list[str] = field(default_factory=lambda: ["component:0.02"])
    stable_rerank: bool = True
    stable_tfidf_weight: float = 0.20
    stable_metadata_weight: float = 0.05
    stable_agreement_weight: float = 0.05
    candidate_pool: int = 50
    top_k: int = 10
    # Raw model-score thresholds.  ``None`` keeps the semi-automatic design of the DUP project:
    # candidates are always handed to an engineer for confirmation.
    auto_duplicate_threshold: float | None = None
    review_threshold: float | None = None


@dataclass(slots=True)
class PrioritySettings:
    model_path: Path | None = None
    reference_feature_dir: Path | None = None
    history_csv: Path | None = None
    rep_weights_json: Path | None = None
    history_max_rows: int = 0
    extra_build_args: list[str] = field(default_factory=list)
    timeout_seconds: int = 900
    fallback_to_baseline: bool = True


@dataclass(slots=True)
class ProjectSettings:
    name: str
    repo_path: Path
    code_index_path: Path | None = None
    description: str = ""
    # Versioned repositories (e.g. SWE-bench): one source snapshot per base_commit in
    # ``snapshots_dir/<commit>/``; a ticket with ``base_commit`` is localized in its own snapshot,
    # with its code index cached as ``snapshots_dir/<commit>.index.json``.
    snapshots_dir: Path | None = None


@dataclass(slots=True)
class LocalizationSettings:
    top_k: int = 5
    embedding_backend: str = "tfidf"
    include_code_preview: bool = True


@dataclass(slots=True)
class IntegrationConfig:
    workspace_root: Path = field(default_factory=default_workspace_root)
    store_dir: Path = field(default_factory=lambda: MAIN_PROJECT_ROOT / "data" / "workspace")
    historical_tickets_paths: list[Path] = field(default_factory=list)
    extraction: ExtractionSettings = field(default_factory=ExtractionSettings)
    duplicate: DuplicateSettings = field(default_factory=DuplicateSettings)
    priority: PrioritySettings = field(default_factory=PrioritySettings)
    assignee: dict[str, Any] = field(default_factory=dict)
    localization: LocalizationSettings = field(default_factory=LocalizationSettings)
    projects: dict[str, ProjectSettings] = field(default_factory=dict)
    default_project: str = ""
    config_path: Path | None = None

    def subproject(self, key: str) -> Path:
        return resolve_subproject(self.workspace_root, key)

    def priority_paths(self) -> dict[str, Path | None]:
        """Resolve PRI artifact paths, defaulting to the PRI README's documented locations."""

        root = self.subproject("priority")
        settings = self.priority
        return {
            "root": root,
            "model_path": settings.model_path or root / "models" / "recall_balanced_priority_model.joblib",
            "reference_feature_dir": settings.reference_feature_dir
            or root / "data" / "processed" / "features_bm25_p2_error_keywords_train",
            "history_csv": settings.history_csv,
            "rep_weights_json": settings.rep_weights_json
            or root / "models" / "rep_minus_weights_bm25_p2_error_keywords.json",
        }

    def project(self, name: str | None) -> ProjectSettings | None:
        key = (name or self.default_project or "").strip()
        if key and key in self.projects:
            return self.projects[key]
        if not key and len(self.projects) == 1:
            return next(iter(self.projects.values()))
        return None

    def to_public_dict(self) -> dict[str, Any]:
        """Serializable view used by the status page (paths as strings)."""

        def _p(value: Path | None) -> str:
            return str(value) if value else ""

        return {
            "config_path": _p(self.config_path),
            "workspace_root": _p(self.workspace_root),
            "store_dir": _p(self.store_dir),
            "historical_tickets_paths": [_p(path) for path in self.historical_tickets_paths],
            "extraction": {
                "enabled": self.extraction.enabled,
                "ollama_url": self.extraction.ollama_url,
                "model": self.extraction.model,
                "prompt_mode": self.extraction.prompt_mode,
            },
            "duplicate": {
                "sbert_model_dir": _p(self.duplicate.sbert_model_dir),
                "combine": self.duplicate.combine,
                "metadata_rerank_fields": list(self.duplicate.metadata_rerank_fields),
                "stable_rerank": self.duplicate.stable_rerank,
                "top_k": self.duplicate.top_k,
                "auto_duplicate_threshold": self.duplicate.auto_duplicate_threshold,
                "review_threshold": self.duplicate.review_threshold,
            },
            "priority": {key: _p(value) for key, value in self.priority_paths().items()},
            "assignee": {key: str(value) for key, value in self.assignee.items()},
            "localization": {
                "top_k": self.localization.top_k,
                "embedding_backend": self.localization.embedding_backend,
            },
            "projects": {
                name: {
                    "repo_path": _p(project.repo_path),
                    "code_index_path": _p(project.code_index_path),
                    "description": project.description,
                    "snapshots_dir": _p(project.snapshots_dir),
                }
                for name, project in self.projects.items()
            },
            "default_project": self.default_project,
        }


PATH_KEYS_DUPLICATE = {"sbert_model_dir"}
PATH_KEYS_PRIORITY = {"model_path", "reference_feature_dir", "history_csv", "rep_weights_json"}
ASSIGNEE_PATH_KEYS = {
    "assignee_dataset_path",
    "assignee_active_roster_path",
    "assignee_inactive_path",
    "assignee_component_ownership_path",
    "assignee_file_ownership_path",
    "assignee_feedback_path",
    "assignee_routing_policy_path",
    "assignee_calibration_artifact_path",
    "assignee_open_set_artifact_path",
}


def load_integration_config(path: str | Path | None = None) -> IntegrationConfig:
    """Load the JSON config.  Without a file every section keeps its documented default."""

    if path is None:
        return IntegrationConfig()
    config_path = Path(path).expanduser().resolve()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Integration config must be a JSON object.")
    return config_from_mapping(data, base_dir=config_path.parent, config_path=config_path)


def config_from_mapping(
    data: dict[str, Any],
    *,
    base_dir: Path | None = None,
    config_path: Path | None = None,
) -> IntegrationConfig:
    base = base_dir or Path.cwd()

    def as_path(value: Any) -> Path | None:
        if value in (None, ""):
            return None
        candidate = Path(str(value)).expanduser()
        return candidate if candidate.is_absolute() else (base / candidate).resolve()

    config = IntegrationConfig(config_path=config_path)
    if data.get("workspace_root"):
        config.workspace_root = as_path(data["workspace_root"]) or config.workspace_root
    if data.get("store_dir"):
        config.store_dir = as_path(data["store_dir"]) or config.store_dir

    history = data.get("historical_tickets_paths", data.get("historical_tickets_path"))
    if isinstance(history, str):
        history = [history]
    config.historical_tickets_paths = [path for path in (as_path(item) for item in history or []) if path]

    extraction = data.get("extraction") or {}
    for key, value in extraction.items():
        if hasattr(config.extraction, key):
            setattr(config.extraction, key, value)

    duplicate = data.get("duplicate") or {}
    for key, value in duplicate.items():
        if not hasattr(config.duplicate, key):
            continue
        setattr(config.duplicate, key, as_path(value) if key in PATH_KEYS_DUPLICATE else value)

    priority = data.get("priority") or {}
    for key, value in priority.items():
        if not hasattr(config.priority, key):
            continue
        setattr(config.priority, key, as_path(value) if key in PATH_KEYS_PRIORITY else value)

    assignee = {key: value for key, value in (data.get("assignee") or {}).items() if not str(key).startswith("_")}
    for key in list(assignee):
        if key in ASSIGNEE_PATH_KEYS:
            assignee[key] = as_path(assignee[key])
    config.assignee = {key: value for key, value in assignee.items() if value is not None}

    localization = data.get("localization") or {}
    for key, value in localization.items():
        if hasattr(config.localization, key):
            setattr(config.localization, key, value)

    projects = data.get("projects") or {}
    if isinstance(projects, list):
        projects = {str(item.get("name")): item for item in projects if isinstance(item, dict)}
    for name, item in projects.items():
        if not isinstance(item, dict) or not item.get("repo_path"):
            continue
        config.projects[str(name)] = ProjectSettings(
            name=str(name),
            repo_path=as_path(item["repo_path"]) or Path(str(item["repo_path"])),
            code_index_path=as_path(item.get("code_index_path")),
            description=str(item.get("description") or ""),
            snapshots_dir=as_path(item.get("snapshots_dir")),
        )
    config.default_project = str(data.get("default_project") or "")
    return config
