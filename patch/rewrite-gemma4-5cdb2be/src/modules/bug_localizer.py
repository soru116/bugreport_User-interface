from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.fault_localization import CODE_SUFFIXES, CodeIndex, build_code_index, load_code_index, localize_ticket


class BugLocalizer:
    """Locate likely faulty files/functions from ticket text and repository code.

    The default implementation is a runnable retrieval baseline: it builds code
    chunks from the repository, ranks them against the bug report with vector
    similarity, and optionally leaves room for an LLM reranker.
    """

    def __init__(
        self,
        *,
        code_index_path: str | Path | None = None,
        top_k: int = 5,
        embedding_backend: str = "tfidf",
        sbert_model: str = "sentence-transformers/all-MiniLM-L6-v2",
        sbert_local_files_only: bool = True,
        semantic_candidate_k: int = 50,
        candidate_file_k: int = 20,
        generic_routing: bool = False,
        domain_path_routing: bool = True,
        repository_proximity: bool = False,
        import_graph_mode: str = "off",
        call_graph_mode: str = "off",
        symbol_expansion_mode: str = "off",
        llm_client: Any | None = None,
        llm_rerank: bool = False,
        llm_candidate_k: int | None = None,
        file_aggregation: bool = True,
        advanced_file_aggregation: bool = False,
        min_ticket_chars: int = 20,
    ) -> None:
        self.code_index_path = Path(code_index_path) if code_index_path else None
        self.top_k = top_k
        self.embedding_backend = embedding_backend
        self.sbert_model = sbert_model
        self.sbert_local_files_only = sbert_local_files_only
        self.semantic_candidate_k = semantic_candidate_k
        self.candidate_file_k = candidate_file_k
        self.generic_routing = generic_routing
        self.domain_path_routing = domain_path_routing
        self.repository_proximity = repository_proximity
        self.import_graph_mode = import_graph_mode
        self.call_graph_mode = call_graph_mode
        self.symbol_expansion_mode = symbol_expansion_mode
        self.llm_client = llm_client
        self.llm_rerank = llm_rerank
        self.llm_candidate_k = llm_candidate_k
        self.file_aggregation = file_aggregation
        self.advanced_file_aggregation = advanced_file_aggregation
        self.min_ticket_chars = min_ticket_chars
        self._cached_index_key: tuple[str, ...] | None = None
        self._cached_index: CodeIndex | None = None

    def localize(self, ticket_json: dict[str, Any], repo_path: str) -> dict[str, Any]:
        code_index: CodeIndex
        if self.code_index_path is not None and self.code_index_path.exists():
            key = (str(self.code_index_path.resolve()), str(self.code_index_path.stat().st_mtime_ns))
            if self._cached_index_key != key:
                self._cached_index = load_code_index(self.code_index_path)
                self._cached_index_key = key
        else:
            root = Path(repo_path).resolve()
            repository_name = str(
                ticket_json.get("repo")
                or ticket_json.get("repository")
                or root.name
            ).strip()
            base_commit = str(ticket_json.get("base_commit") or "").strip()
            key = (
                str(root),
                _source_tree_token(root),
                repository_name,
                base_commit,
            )
            if self._cached_index_key != key:
                previous_index = (
                    self._cached_index
                    if self._cached_index is not None
                    and self._cached_index.runtime_repository_matches(root) is True
                    else None
                )
                self._cached_index = build_code_index(
                    root,
                    repository_name=repository_name,
                    base_commit=base_commit or None,
                    previous_index=previous_index,
                )
                self._cached_index_key = key

        if self._cached_index is None:  # Defensive guard for type/runtime safety.
            raise ValueError("Unable to build or load the repository code index.")
        code_index = self._cached_index

        result = localize_ticket(
            ticket_json,
            code_index=code_index,
            top_k=self.top_k,
            embedding_backend=self.embedding_backend,
            sbert_model=self.sbert_model,
            sbert_local_files_only=self.sbert_local_files_only,
            semantic_candidate_k=self.semantic_candidate_k,
            candidate_file_k=self.candidate_file_k,
            generic_routing=self.generic_routing,
            domain_path_routing=self.domain_path_routing,
            repository_proximity=self.repository_proximity,
            import_graph_mode=self.import_graph_mode,
            call_graph_mode=self.call_graph_mode,
            symbol_expansion_mode=self.symbol_expansion_mode,
            llm_client=self.llm_client,
            llm_rerank=self.llm_rerank,
            llm_candidate_k=self.llm_candidate_k,
            file_aggregation=self.file_aggregation,
            advanced_file_aggregation=self.advanced_file_aggregation,
            min_ticket_chars=self.min_ticket_chars,
        )
        input_validation = result.get("input_validation")
        invalid_input = isinstance(input_validation, dict) and not bool(input_validation.get("is_valid", True))
        if not result.get("localized_candidates") and not invalid_input:
            raise ValueError("No bug localization candidates found in the repository code index.")
        if not result.get("repository_path"):
            result["repository_path"] = "."
        return result


def _source_tree_token(root: Path) -> str:
    """Return a cheap change token so repeated tickets can reuse one index."""

    count = 0
    total_size = 0
    latest_mtime_ns = 0
    ignored = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".venv", "venv"}
    for path in root.rglob("*"):
        if any(part in ignored for part in path.parts) or path.is_symlink():
            continue
        if not path.is_file() or path.suffix.lower() not in CODE_SUFFIXES:
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
            stat = resolved.stat()
        except (OSError, ValueError):
            continue
        count += 1
        total_size += stat.st_size
        latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
    return f"count={count}:size={total_size}:mtime={latest_mtime_ns}"
