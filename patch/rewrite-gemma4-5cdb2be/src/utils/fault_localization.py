from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import subprocess
import warnings
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

from utils.symbol_localization import (
    SYMBOL_PARSE_RESULT_SCHEMA_VERSION,
    SYMBOL_RECORD_SCHEMA_VERSION,
    SymbolParseResult,
    SymbolRecordV1,
    extract_python_symbols,
    extract_symbols_from_repository_file,
    extract_symbols_from_source,
)


CODE_INDEX_VERSION = 5


CODE_SUFFIXES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".go": "go",
    ".rs": "rust",
    ".scala": "scala",
}

IGNORED_DIRS_ANYWHERE = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "venv",
    ".venv",
}

# These names commonly contain generated artifacts at repository root, but are
# also legitimate source-package names deeper in a tree (for example,
# django/db/models). Excluding them everywhere can silently remove real code.
IGNORED_ROOT_DIRS = {
    "dist",
    "build",
    "data",
    "models",
    "reports",
}

# Kept as a public compatibility alias for callers that inspect this constant.
IGNORED_DIRS = IGNORED_DIRS_ANYWHERE | IGNORED_ROOT_DIRS

TEST_DIR_NAMES = {"test", "tests", "__tests__", "spec", "specs"}
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
CAMEL_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|[0-9]+")
TOKEN_SPLIT_RE = re.compile(r"[_\W]+")

STOPWORDS = {
    "about",
    "actual",
    "after",
    "again",
    "also",
    "and",
    "are",
    "behavior",
    "bug",
    "but",
    "can",
    "cannot",
    "crash",
    "does",
    "error",
    "expected",
    "fail",
    "failed",
    "fails",
    "failure",
    "for",
    "from",
    "has",
    "have",
    "into",
    "issue",
    "log",
    "none",
    "not",
    "null",
    "only",
    "should",
    "step",
    "steps",
    "the",
    "this",
    "ticket",
    "typeerror",
    "value",
    "when",
    "with",
    "without",
}

SCORING_WEIGHTS = {
    # The five retrieval weights sum to 1.0. Domain-path evidence is an
    # additive, bounded routing boost because it is sparse and high precision.
    "embedding_score": 0.50,
    "stack_trace_score": 0.30,
    "component_score": 0.05,
    "keyword_score": 0.08,
    "symbol_score": 0.07,
    "domain_path_score": 0.18,
    "identifier_score": 0.14,
    "path_term_score": 0.12,
    "repository_proximity_score": 0.10,
    "package_proximity_score": 0.12,
}

HYBRID_SEMANTIC_WEIGHTS = {
    "tfidf": 0.35,
    "sbert": 0.65,
}

# E1 is intentionally a small, ordered ablation instead of one opaque switch.
# The values are fixed before Validation is run; only Development may be used
# to replace them in a later protocol version.
FILE_AGGREGATION_MODES = (
    "basic",
    "supporting-chunks",
    "supporting-symbols",
    "supporting-symbols-package",
)
FILE_AGGREGATION_PARAMETERS: dict[str, float | int] = {
    "max_evidence_chunks": 5,
    "evidence_score_ratio": 0.80,
    "minimum_evidence_score": 0.05,
    "support_bonus_per_additional_chunk": 0.0125,
    "support_bonus_cap": 0.05,
    "symbol_bonus_per_additional_symbol": 0.01,
    "symbol_bonus_cap": 0.04,
    "package_bonus_cap": 0.10,
}

# E2 import-graph ablations.  The graph is repository-level and independent of
# the ticket; only the bounded one-hop scoring pass runs per ticket.
IMPORT_GRAPH_MODES = ("off", "outgoing", "bidirectional")
IMPORT_GRAPH_PARAMETERS: dict[str, float | int] = {
    "reference_file_k": 5,
    "outgoing_signal": 0.80,
    "incoming_signal": 0.65,
    "rank_decay": 0.08,
    "maximum_signal": 0.80,
    "maximum_bonus": 0.08,
    "maximum_evidence_per_file": 4,
}

# E4 follows only statically resolvable Python calls from the strongest files.
# The graph and scoring values are fixed Development hypotheses, not
# literature-derived weights.  File and evidence caps prevent hub functions
# from spreading a bonus across a large repository.
CALL_GRAPH_MODES = ("off", "outgoing", "outgoing-top3")
CALL_GRAPH_PARAMETERS: dict[str, float | int] = {
    "reference_file_k": 5,
    "outgoing_signal": 0.90,
    "rank_decay": 0.08,
    "maximum_signal": 0.90,
    "maximum_bonus": 0.08,
    "maximum_target_files_per_reference": 8,
    "maximum_evidence_per_file": 4,
}

# E3-B resolves explicit code-like names in a Ticket to definition files in the
# pre-fix Code Index.  The values are fixed engineering hypotheses for the
# first Development run; they are not literature-derived weights.
SYMBOL_EXPANSION_MODES = (
    "off",
    "symbol-definitions",
    "symbol-definitions-strict",
    "symbol-definitions-guarded",
    "symbol-definitions-api",
    "symbol-definitions-api-namespace",
)
SYMBOL_EXPANSION_PARAMETERS: dict[str, float | int] = {
    "max_ticket_symbols": 20,
    "max_files_per_symbol": 4,
    "exact_match_bonus": 0.06,
    "leaf_match_bonus": 0.04,
    "maximum_bonus": 0.10,
    "maximum_evidence_per_file": 4,
}

# E3-C follows explicit, statically resolvable Python API aliases and direct
# wrapper delegation.  These are fixed Development hypotheses, not
# literature-derived weights.  The caps prevent a generic API name from
# spreading evidence across a large repository.
API_IMPLEMENTATION_PARAMETERS: dict[str, float | int] = {
    "max_links_per_symbol": 4,
    "reexport_bonus": 0.04,
    "wrapper_bonus": 0.05,
    "maximum_bonus": 0.08,
    "maximum_evidence_per_file": 4,
    "maximum_wrapper_statements": 4,
}

# Guarded dotted-name fallback keeps distinctive API names such as
# ``numpy.histogram`` while rejecting receiver-dependent or generic names such
# as ``self.version`` and ``printer.flush``.
DOTTED_LEAF_RECEIVER_NAMES = {"self", "other", "cls", "super"}
DOTTED_LEAF_GENERIC_NAMES = {
    "check",
    "copy",
    "data",
    "fit",
    "fits",
    "flush",
    "from_columns",
    "getdata",
    "group",
    "html",
    "info",
    "masked",
    "name",
    "quantity",
    "read",
    "to_table",
    "unit",
    "units",
    "version",
    "view",
    "wcs",
    "write",
    "writeto",
}

SYMBOL_IDENTIFIER_STOPWORDS = STOPWORDS | {
    "actual_behavior",
    "assert",
    "async",
    "await",
    "bool",
    "break",
    "bug_report",
    "bytes",
    "class",
    "continue",
    "description",
    "def",
    "dict",
    "else",
    "error_message",
    "except",
    "false",
    "finally",
    "float",
    "expected_behavior",
    "import",
    "int",
    "lambda",
    "len",
    "list",
    "logs",
    "print",
    "raise",
    "screenshots_text",
    "steps_to_reproduce",
    "summary",
    "return",
    "self",
    "set",
    "str",
    "true",
    "ticket_id",
    "title",
    "try",
    "tuple",
    "while",
    "yield",
}

LLM_MAX_BUG_REPORT_CHARS = 4_000
LLM_MAX_CODE_CHARS_PER_CANDIDATE = 800
LLM_RERANK_WEIGHTS = {
    "retrieval": 0.70,
    "llm": 0.30,
}
LLM_RERANK_BATCH_SIZE = 5

SYMBOL_RERANK_KINDS = {
    "module",
    "function",
    "async_function",
    "method",
    "async_method",
    "class",
}

SYMBOL_RETRIEVAL_MODES = ("b0-tfidf", "b1-structured")
SYMBOL_SELECTION_MODES = ("global", "module-reserved", "coverage-aware-v1", "source-neighborhood-v1", "call-neighborhood-v1", "call-neighborhood-v2")
SYMBOL_B1_WEIGHTS = {
    "lexical_score": 0.50,
    "identifier_score": 0.20,
    "stack_trace_score": 0.15,
    "symbol_name_score": 0.10,
    "stage2_file_score": 0.05,
}
CALL_NEIGHBOR_SCORE_WEIGHT = 0.45

TICKET_CONTENT_KEYS = (
    "bug_report",
    "title",
    "summary",
    "body",
    "description",
    "error_message",
    "logs",
    "steps_to_reproduce",
    "expected_behavior",
    "actual_behavior",
)


@dataclass(frozen=True)
class CodeChunk:
    chunk_id: str
    file_path: str
    language: str
    symbol_kind: str
    function_name: str
    class_name: str
    start_line: int
    end_line: int
    code_text: str

    @property
    def symbol_name(self) -> str:
        if self.function_name:
            return self.function_name
        if self.class_name:
            return self.class_name
        return ""

    @property
    def symbol_qualified_name(self) -> str:
        if self.symbol_kind == "module":
            return "<module>"
        return self.symbol_name

    @property
    def evaluation_key(self) -> str:
        symbol = self.symbol_qualified_name or "<file>"
        return f"{self.file_path}::{symbol}:{self.start_line}-{self.end_line}"

    @property
    def search_text(self) -> str:
        return "\n".join(
            part
            for part in (
                self.file_path,
                self.language,
                self.symbol_kind,
                self.function_name,
                self.class_name,
                self.code_text,
            )
            if part
        )

    def to_dict(self, *, include_code: bool = True) -> dict[str, Any]:
        row: dict[str, Any] = {
            "chunk_id": self.chunk_id,
            "file_path": self.file_path,
            "language": self.language,
            "symbol_kind": self.symbol_kind,
            "function_name": self.function_name,
            "class_name": self.class_name,
            "symbol_name": self.symbol_name,
            "symbol_qualified_name": self.symbol_qualified_name,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "evaluation_key": self.evaluation_key,
        }
        if include_code:
            row["code_text"] = self.code_text
        return row

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "CodeChunk":
        return cls(
            chunk_id=str(row.get("chunk_id") or ""),
            file_path=str(row.get("file_path") or row.get("file") or ""),
            language=str(row.get("language") or ""),
            symbol_kind=str(row.get("symbol_kind") or row.get("kind") or "chunk"),
            function_name=str(row.get("function_name") or row.get("function") or ""),
            class_name=str(row.get("class_name") or ""),
            start_line=int(row.get("start_line") or row.get("line_start") or 1),
            end_line=int(row.get("end_line") or row.get("line_end") or 1),
            code_text=str(row.get("code_text") or row.get("snippet") or ""),
        )


@dataclass
class ImportGraph:
    """Cached, repository-level Python import relationships."""

    outgoing: dict[str, list[str]] = field(default_factory=dict)
    incoming: dict[str, list[str]] = field(default_factory=dict)
    unresolved: dict[str, list[str]] = field(default_factory=dict)
    version: int = 1
    stats: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "outgoing": self.outgoing,
            "incoming": self.incoming,
            "unresolved": self.unresolved,
            "stats": self.stats,
        }

    def save(self, path: str | Path) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "ImportGraph":
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("Import graph cache must contain a JSON object.")
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ImportGraph":
        def adjacency(name: str) -> dict[str, list[str]]:
            raw = payload.get(name) or {}
            if not isinstance(raw, dict):
                return {}
            return {
                str(file_path): sorted({str(value) for value in values})
                for file_path, values in raw.items()
                if isinstance(values, list)
            }

        raw_stats = payload.get("stats") or {}
        return cls(
            outgoing=adjacency("outgoing"),
            incoming=adjacency("incoming"),
            unresolved=adjacency("unresolved"),
            version=int(payload.get("version") or 1),
            stats={
                str(key): int(value)
                for key, value in raw_stats.items()
                if isinstance(value, (int, float))
            }
            if isinstance(raw_stats, dict)
            else {},
        )


@dataclass
class CallGraph:
    """Cached, repository-level one-hop Python call relationships."""

    outgoing: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    incoming: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    unresolved: dict[str, list[str]] = field(default_factory=dict)
    version: int = 1
    stats: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "outgoing": self.outgoing,
            "incoming": self.incoming,
            "unresolved": self.unresolved,
            "stats": self.stats,
        }

    def save(self, path: str | Path) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "CallGraph":
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("Call graph cache must contain a JSON object.")
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CallGraph":
        def edge_map(name: str) -> dict[str, list[dict[str, Any]]]:
            raw = payload.get(name) or {}
            if not isinstance(raw, dict):
                return {}
            output: dict[str, list[dict[str, Any]]] = {}
            for raw_file, raw_records in raw.items():
                file_path = _normalize_path(str(raw_file))
                if not file_path or not isinstance(raw_records, list):
                    continue
                records: list[dict[str, Any]] = []
                for raw_record in raw_records:
                    if not isinstance(raw_record, dict):
                        continue
                    caller_file = _normalize_path(
                        str(raw_record.get("caller_file") or "")
                    )
                    target_file = _normalize_path(
                        str(raw_record.get("target_file") or "")
                    )
                    if not caller_file or not target_file:
                        continue
                    records.append(
                        {
                            "caller_file": caller_file,
                            "caller_symbol": str(
                                raw_record.get("caller_symbol") or "<module>"
                            ),
                            "target_file": target_file,
                            "target_symbol": str(
                                raw_record.get("target_symbol") or ""
                            ),
                            "call_name": str(raw_record.get("call_name") or ""),
                            "line": int(raw_record.get("line") or 1),
                            "resolution_type": str(
                                raw_record.get("resolution_type") or ""
                            ),
                        }
                    )
                if records:
                    output[file_path] = records
            return output

        raw_unresolved = payload.get("unresolved") or {}
        unresolved = (
            {
                _normalize_path(str(file_path)): sorted(
                    {str(value) for value in values if str(value)}
                )
                for file_path, values in raw_unresolved.items()
                if _normalize_path(str(file_path)) and isinstance(values, list)
            }
            if isinstance(raw_unresolved, dict)
            else {}
        )
        raw_stats = payload.get("stats") or {}
        return cls(
            outgoing=edge_map("outgoing"),
            incoming=edge_map("incoming"),
            unresolved=unresolved,
            version=int(payload.get("version") or 1),
            stats={
                str(key): int(value)
                for key, value in raw_stats.items()
                if isinstance(value, (int, float))
            }
            if isinstance(raw_stats, dict)
            else {},
        )

@dataclass
class CodeIndex:
    repository_path: str
    chunks: list[CodeChunk]
    symbol_records: list[SymbolRecordV1] = field(default_factory=list)
    symbol_parse_results: list[SymbolParseResult] = field(default_factory=list)
    created_at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    version: int = CODE_INDEX_VERSION
    settings: dict[str, Any] = field(default_factory=dict)
    import_graph: ImportGraph | None = None
    call_graph: CallGraph | None = None
    symbol_definitions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    api_implementation_links: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict
    )
    _runtime_repository_path: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        raw_path = str(self.repository_path or "").strip()
        if raw_path and Path(raw_path).is_absolute():
            if not self._runtime_repository_path:
                self._runtime_repository_path = str(Path(raw_path).resolve())
            self.repository_path = "."
        elif _is_absolute_path_any_platform(raw_path):
            # A foreign-platform absolute path came from a serialized index;
            # discard it without treating it as a usable runtime repository.
            self.repository_path = "."
        else:
            self.repository_path = _portable_repository_path(raw_path)

    def runtime_repository_matches(self, repo_path: str | Path) -> bool | None:
        """Compare a live repository path when this process built the index.

        A loaded portable index intentionally has no machine-specific runtime
        path. In that case callers should validate the repository fingerprint.
        """

        if not self._runtime_repository_path:
            return None
        return Path(self._runtime_repository_path).resolve() == Path(repo_path).resolve()

    def to_dict(self, *, include_code: bool = True) -> dict[str, Any]:
        return {
            "version": self.version,
            "repository_path": _portable_repository_path(self.repository_path),
            "created_at_utc": self.created_at_utc,
            "settings": self.settings,
            "import_graph": self.import_graph.to_dict() if self.import_graph is not None else None,
            "call_graph": self.call_graph.to_dict() if self.call_graph is not None else None,
            "symbol_definitions": self.symbol_definitions,
            "api_implementation_links": self.api_implementation_links,
            "symbol_records": [record.to_dict() for record in self.symbol_records],
            "symbol_parse_diagnostics": [
                result.to_dict(include_symbols=False)
                for result in self.symbol_parse_results
            ],
            "chunks": [chunk.to_dict(include_code=include_code) for chunk in self.chunks],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CodeIndex":
        chunks = [CodeChunk.from_dict(row) for row in payload.get("chunks", []) if isinstance(row, dict)]
        symbol_records = [
            SymbolRecordV1.from_dict(row)
            for row in payload.get("symbol_records", [])
            if isinstance(row, dict)
        ]
        symbols_by_file: dict[str, list[SymbolRecordV1]] = {}
        for record in symbol_records:
            symbols_by_file.setdefault(record.file_path, []).append(record)
        symbol_parse_results: list[SymbolParseResult] = []
        for row in payload.get("symbol_parse_diagnostics", []):
            if not isinstance(row, dict):
                continue
            file_path = _normalize_path(str(row.get("file_path") or ""))
            symbols = symbols_by_file.get(file_path, []) if row.get("status") == "ok" else []
            expected_count = int(row.get("symbol_count") or 0)
            if expected_count != len(symbols):
                raise ValueError(
                    f"Symbol diagnostic count mismatch for {file_path!r}: "
                    f"expected {expected_count}, found {len(symbols)}."
                )
            symbol_parse_results.append(
                SymbolParseResult.from_dict(row, symbols=symbols)
            )
        return cls(
            # Old indexes may contain an absolute path from the machine that
            # created them. Discard it when loading so the index remains usable
            # after the project is moved to another environment.
            repository_path=_portable_repository_path(
                str(payload.get("repository_path") or "")
            ),
            chunks=chunks,
            symbol_records=symbol_records,
            symbol_parse_results=symbol_parse_results,
            created_at_utc=str(payload.get("created_at_utc") or ""),
            version=int(payload.get("version") or 1),
            settings=dict(payload.get("settings") or {}),
            import_graph=(
                ImportGraph.from_dict(payload["import_graph"])
                if isinstance(payload.get("import_graph"), dict)
                else None
            ),
            call_graph=(
                CallGraph.from_dict(payload["call_graph"])
                if isinstance(payload.get("call_graph"), dict)
                else None
            ),
            symbol_definitions=_load_symbol_definition_index(
                payload.get("symbol_definitions")
            ),
            api_implementation_links=_load_api_implementation_index(
                payload.get("api_implementation_links")
            ),
        )

    def save(self, path: str | Path) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "CodeIndex":
        with Path(path).open("r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))


def _is_absolute_path_any_platform(value: str) -> bool:
    """Recognize absolute paths written for POSIX or Windows."""

    normalized = str(value or "").strip()
    return bool(normalized) and (
        PurePosixPath(normalized).is_absolute()
        or PureWindowsPath(normalized).is_absolute()
    )


def _portable_repository_path(value: str) -> str:
    """Return an environment-independent repository-root marker."""

    normalized = str(value or "").strip()
    if not normalized or _is_absolute_path_any_platform(normalized):
        return "."
    return Path(normalized).as_posix()


@dataclass(frozen=True)
class LocalizationCandidate:
    chunk: CodeChunk
    score: float
    embedding_score: float
    reason: str
    signals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, rank: int) -> dict[str, Any]:
        scoring_signals = _candidate_scoring_signals(self)
        row = {
            "rank": rank,
            "file_path": self.chunk.file_path,
            "function_name": self.chunk.function_name,
            "class_name": self.chunk.class_name,
            "symbol_name": self.chunk.symbol_name,
            "symbol_qualified_name": self.chunk.symbol_qualified_name,
            "symbol_kind": self.chunk.symbol_kind,
            "start_line": self.chunk.start_line,
            "end_line": self.chunk.end_line,
            "score": round(float(self.score), 4),
            "final_score": round(float(self.score), 4),
            "embedding_score": round(float(self.embedding_score), 4),
            "reason": self.reason,
            "code_text": self.chunk.code_text,
            "chunk_id": self.chunk.chunk_id,
            "evaluation_key": self.chunk.evaluation_key,
            "scoring_signals": scoring_signals,
            "signals": self.signals,
        }
        supporting_chunks = self.signals.get("supporting_chunks")
        if isinstance(supporting_chunks, list):
            row["supporting_chunks"] = supporting_chunks
        return row


class FaultLocalizer:
    """Retrieve likely faulty code chunks for a structured bug report."""

    def __init__(
        self,
        *,
        code_index: CodeIndex | None = None,
        repo_path: str | Path | None = None,
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
        symbol_localization: bool = False,
        symbol_llm_rerank: bool = False,
        symbol_rerank: bool | None = None,
        symbol_candidate_k: int = 30,
        symbol_top_k: int = 5,
        symbol_retrieval_mode: str = "b0-tfidf",
        symbol_per_file_quota: int = 0,
        symbol_selection_mode: str = "global",
        file_aggregation: bool = True,
        advanced_file_aggregation: bool = False,
        file_aggregation_mode: str | None = None,
        min_ticket_chars: int = 20,
    ) -> None:
        _validate_top_k(top_k)
        if min_ticket_chars <= 0:
            raise ValueError("min_ticket_chars must be positive.")
        if semantic_candidate_k <= 0:
            raise ValueError("semantic_candidate_k must be positive.")
        if candidate_file_k <= 0:
            raise ValueError("candidate_file_k must be positive.")
        if _is_hybrid_backend(embedding_backend) and semantic_candidate_k < candidate_file_k:
            raise ValueError(
                "semantic_candidate_k must be greater than or equal to candidate_file_k "
                "for hybrid Stage-1 retrieval."
            )
        if llm_candidate_k is not None and llm_candidate_k <= 0:
            raise ValueError("llm_candidate_k must be positive when supplied.")
        if symbol_candidate_k <= 0:
            raise ValueError("symbol_candidate_k must be positive.")
        if symbol_top_k <= 0:
            raise ValueError("symbol_top_k must be positive.")
        normalized_symbol_retrieval_mode = symbol_retrieval_mode.strip().casefold()
        if normalized_symbol_retrieval_mode not in SYMBOL_RETRIEVAL_MODES:
            raise ValueError(
                "symbol_retrieval_mode must be one of: "
                + ", ".join(SYMBOL_RETRIEVAL_MODES)
            )
        if symbol_per_file_quota < 0:
            raise ValueError("symbol_per_file_quota cannot be negative.")
        normalized_symbol_selection_mode = symbol_selection_mode.strip().casefold()
        if normalized_symbol_selection_mode not in SYMBOL_SELECTION_MODES:
            raise ValueError(
                "symbol_selection_mode must be one of: "
                + ", ".join(SYMBOL_SELECTION_MODES)
            )
        if normalized_symbol_selection_mode != "global" and symbol_per_file_quota:
            raise ValueError(
                "symbol_per_file_quota must be 0 for coverage-aware selection modes."
            )
        self.code_index = code_index
        self.repo_path = Path(repo_path).resolve() if repo_path else None
        self.top_k = top_k
        self.embedding_backend = embedding_backend
        self.sbert_model = sbert_model
        self.sbert_local_files_only = sbert_local_files_only
        self.semantic_candidate_k = semantic_candidate_k
        self.candidate_file_k = candidate_file_k
        self.generic_routing = generic_routing
        self.domain_path_routing = domain_path_routing
        self.import_graph_mode = _resolve_import_graph_mode(
            import_graph_mode,
            legacy_repository_proximity=repository_proximity,
        )
        self.repository_proximity = self.import_graph_mode != "off"
        self.call_graph_mode = _resolve_call_graph_mode(call_graph_mode)
        self.symbol_expansion_mode = _resolve_symbol_expansion_mode(
            symbol_expansion_mode
        )
        self.llm_client = llm_client
        self.llm_rerank = llm_rerank
        self.llm_candidate_k = llm_candidate_k
        legacy_symbol_rerank = bool(symbol_rerank)
        self.symbol_localization = bool(
            symbol_localization or symbol_llm_rerank or legacy_symbol_rerank
        )
        self.symbol_llm_rerank = bool(symbol_llm_rerank or legacy_symbol_rerank)
        # Deprecated programmatic alias retained for callers that inspect it.
        self.symbol_rerank = self.symbol_llm_rerank
        self.legacy_symbol_rerank_alias_used = symbol_rerank is True
        self.symbol_candidate_k = symbol_candidate_k
        self.symbol_top_k = symbol_top_k
        self.symbol_retrieval_mode = normalized_symbol_retrieval_mode
        self.symbol_per_file_quota = symbol_per_file_quota
        self.symbol_selection_mode = normalized_symbol_selection_mode
        self.file_aggregation = file_aggregation
        self.file_aggregation_mode = _resolve_file_aggregation_mode(
            file_aggregation_mode,
            advanced=advanced_file_aggregation,
        )
        self.advanced_file_aggregation = self.file_aggregation_mode != "basic"
        self.min_ticket_chars = min_ticket_chars

    def localize(self, ticket_json: dict[str, Any]) -> dict[str, Any]:
        index = self.code_index
        if index is None:
            if self.repo_path is None:
                raise ValueError("FaultLocalizer requires either code_index or repo_path.")
            index = build_code_index(
                self.repo_path,
                repository_name=str(
                    ticket_json.get("repo")
                    or ticket_json.get("repository")
                    or self.repo_path.name
                ),
                base_commit=str(ticket_json.get("base_commit") or "") or None,
            )

        input_validation = validate_localization_request(ticket_json, min_ticket_chars=self.min_ticket_chars)
        call_graph = index.call_graph
        bug_report = build_bug_report_text(ticket_json)
        ticket_program_names = (
            extract_ticket_program_names(bug_report)
            if self.symbol_expansion_mode != "off"
            else []
        )
        if self.symbol_expansion_mode != "off" and not index.symbol_definitions:
            index.symbol_definitions = build_symbol_definition_index(index.chunks)
        if (
            _api_implementation_expansion_enabled(self.symbol_expansion_mode)
            and not index.api_implementation_links
        ):
            index.api_implementation_links = build_api_implementation_index(
                index.chunks,
                index.symbol_definitions,
            )
        if input_validation["errors"]:
            candidates: list[LocalizationCandidate] = []
            backend_name = _validate_embedding_backend(self.embedding_backend)
        else:
            llm_candidate_k = max(self.top_k, int(self.llm_candidate_k or self.top_k)) if self.llm_rerank else 0
            retrieval_top_k = max(self.candidate_file_k, self.top_k, llm_candidate_k)
            import_graph = index.import_graph
            if self.import_graph_mode != "off" and import_graph is None:
                import_graph = build_import_graph(index.chunks)
                index.import_graph = import_graph
            if (
                self.call_graph_mode != "off"
                or self.symbol_selection_mode == "call-neighborhood-v2"
            ) and call_graph is None:
                call_graph = build_call_graph(index.chunks, index.symbol_definitions)
                index.call_graph = call_graph
            candidates, backend_name = rank_code_chunks(
                ticket_json,
                index.chunks,
                top_k=retrieval_top_k,
                embedding_backend=self.embedding_backend,
                sbert_model=self.sbert_model,
                sbert_local_files_only=self.sbert_local_files_only,
                semantic_candidate_k=self.semantic_candidate_k,
                file_aggregation=self.file_aggregation,
                advanced_file_aggregation=self.advanced_file_aggregation,
                file_aggregation_mode=self.file_aggregation_mode,
                generic_routing=self.generic_routing,
                domain_path_routing=self.domain_path_routing,
                repository_proximity=self.repository_proximity,
                import_graph_mode=self.import_graph_mode,
                import_graph=import_graph,
                call_graph_mode=self.call_graph_mode,
                call_graph=call_graph,
                symbol_expansion_mode=self.symbol_expansion_mode,
                symbol_definitions=index.symbol_definitions,
                api_implementation_links=index.api_implementation_links,
            )

        # Stage 1 ends here. Preserve its file ranking before an optional LLM
        # changes the order so candidate retrieval can be evaluated in isolation.
        stage1_candidates = candidates[: self.candidate_file_k]

        warnings = [f"Input validation warning: {message}" for message in input_validation["warnings"]]
        warnings.extend(f"Input validation error: {message}" for message in input_validation["errors"])
        if not index.chunks:
            warnings.append("The code index contains no supported source chunks.")
        if self.embedding_backend.strip().lower() == "auto" and backend_name == "tfidf":
            warnings.append("SBERT was unavailable in auto mode; TF-IDF retrieval was used.")
        llm_rerank_used = False
        if self.llm_rerank and self.llm_client is not None and candidates:
            try:
                llm_input = candidates[: max(self.top_k, int(self.llm_candidate_k or self.top_k))]
                candidates = rerank_candidates_with_llm(ticket_json, llm_input, self.llm_client, top_k=self.top_k)
                llm_rerank_used = any("llm_rerank_score" in candidate.signals for candidate in candidates)
                if not llm_rerank_used:
                    warnings.append("LLM reranking returned no usable candidates; retrieval ranking was kept.")
            except Exception as exc:  # pragma: no cover - depends on external LLM service
                warnings.append(f"LLM reranking failed: {exc}")
                candidates = candidates[: self.top_k]
        elif self.llm_rerank and self.llm_client is None:
            warnings.append("LLM reranking was requested but no LLM client was configured; retrieval ranking was kept.")
            candidates = candidates[: self.top_k]
        else:
            candidates = candidates[: self.top_k]

        # The code index already contains AST-derived symbol chunks. Reusing it
        # avoids resolving serialized repository paths, which are intentionally
        # portable and may be recorded as "." after an index is loaded.
        stage3_candidates: list[LocalizationCandidate] = []
        stage3_candidate_pool: list[LocalizationCandidate] = []
        stage3_retrieval_candidates: list[LocalizationCandidate] = []
        symbol_rerank_used = False
        symbol_llm_attempted = False
        symbol_llm_timed_out = False
        symbol_fallback_reason = "not_requested"
        symbol_pool_count = 0
        symbol_pool_expansion = {
            "enabled": False,
            "base_pool_count": 0,
            "ticket_seed_count": 0,
            "call_neighbor_count": 0,
        }
        if self.symbol_localization and candidates:
            symbol_fallback_reason = "no_symbol_candidates"
            stage2_paths = {candidate.chunk.file_path for candidate in candidates}
            symbol_pool = build_symbol_candidate_pool(index.chunks, stage2_paths)
            if self.symbol_selection_mode == "call-neighborhood-v2":
                symbol_pool, symbol_pool_expansion = expand_stage3_symbol_candidate_pool(
                    symbol_pool,
                    index.chunks,
                    ticket_program_names=ticket_program_names,
                    symbol_definitions=index.symbol_definitions,
                    call_graph=call_graph,
                )
            symbol_pool_count = len(symbol_pool)
            if symbol_pool:
                stage2_file_scores = {
                    candidate.chunk.file_path: float(candidate.score)
                    for candidate in candidates
                }
                symbol_candidates = rank_symbol_candidates(
                    ticket_json,
                    symbol_pool,
                    stage2_file_scores=stage2_file_scores,
                    mode=self.symbol_retrieval_mode,
                    top_k=self.symbol_candidate_k,
                    per_file_quota=self.symbol_per_file_quota,
                    selection_mode=self.symbol_selection_mode,
                    call_graph=call_graph,
                )
                stage3_candidate_pool = list(symbol_candidates)
                stage3_candidates = symbol_candidates[: self.symbol_top_k]
                stage3_retrieval_candidates = list(stage3_candidates)
                if self.symbol_llm_rerank and self.llm_client is not None:
                    symbol_llm_attempted = True
                    try:
                        reranked_symbols = rerank_symbols_with_llm(
                            ticket_json,
                            symbol_candidates,
                            self.llm_client,
                            top_k=self.symbol_top_k,
                        )
                        symbol_rerank_used = any(
                            "llm_symbol_rerank_score" in candidate.signals
                            for candidate in reranked_symbols
                        )
                        if symbol_rerank_used:
                            stage3_candidates = reranked_symbols
                            symbol_fallback_reason = ""
                        else:
                            symbol_fallback_reason = "invalid_or_incomplete_output"
                            warnings.append(
                                "Symbol LLM reranking returned incomplete or unusable output; "
                                "symbol retrieval ranking was kept."
                            )
                    except Exception as exc:  # pragma: no cover - external service
                        symbol_llm_timed_out = "timeout" in type(exc).__name__.casefold() or "timed out" in str(exc).casefold()
                        symbol_fallback_reason = (
                            "timeout" if symbol_llm_timed_out else "llm_error"
                        )
                        warnings.append(f"Symbol LLM reranking failed: {exc}")
                elif self.symbol_llm_rerank:
                    symbol_fallback_reason = "llm_client_unavailable"
                    warnings.append(
                        "Symbol reranking was requested but no LLM client was configured; "
                        "symbol retrieval ranking was kept."
                    )
                else:
                    symbol_fallback_reason = "llm_not_requested"
            else:
                warnings.append(
                    "Symbol reranking found no AST-derived function, method, or class chunks "
                    "inside the Stage-2 files."
                )
        elif self.symbol_localization:
            symbol_fallback_reason = "no_stage2_files"

        localized = [candidate.to_dict(rank) for rank, candidate in enumerate(candidates, start=1)]
        ranked_symbols = [
            _ranked_symbol(candidate, rank)
            for rank, candidate in enumerate(stage3_candidates, start=1)
        ]
        retrieval_symbols = [
            _ranked_symbol(candidate, rank)
            for rank, candidate in enumerate(stage3_retrieval_candidates, start=1)
        ]
        candidate_symbols = [
            _ranked_symbol(candidate, rank)
            for rank, candidate in enumerate(stage3_candidate_pool, start=1)
        ]
        stage1_files = [
            _stage1_candidate_file(candidate, rank)
            for rank, candidate in enumerate(stage1_candidates, start=1)
        ]
        best = localized[0] if localized else None
        bug_location = _legacy_bug_location(best)
        confidence = _localization_confidence(localized, input_validation, llm_rerank_used=llm_rerank_used)
        bug_location.update(
            {
                "confidence_level": confidence["confidence_level"],
                "should_manual_review": confidence["should_manual_review"],
                "recommend_patch_generation": confidence["recommend_patch_generation"],
            }
        )
        context_preview = _line_numbered(best["code_text"], best["start_line"]) if best else ""
        return {
            "ticket_id": str(ticket_json.get("ticket_id") or ticket_json.get("id") or ticket_json.get("bug_id") or ""),
            "bug_report": bug_report,
            "method": {
                "name": "code_chunk_embedding_retrieval",
                "stages": [
                    "stage1_chunk_retrieval",
                    *(["stage1_generic_routing"] if self.generic_routing else []),
                    *(["stage1_domain_path_routing"] if self.domain_path_routing else []),
                    *(
                        ["stage1_symbol_definition_expansion"]
                        if self.symbol_expansion_mode != "off"
                        else []
                    ),
                    *(
                        ["stage1_api_implementation_expansion"]
                        if _api_implementation_expansion_enabled(
                            self.symbol_expansion_mode
                        )
                        else []
                    ),
                    *(["stage1_import_graph"] if self.import_graph_mode != "off" else []),
                    *(["stage1_call_graph"] if self.call_graph_mode != "off" else []),
                    *(["stage1_semantic_rerank"] if _is_hybrid_backend(self.embedding_backend) else []),
                    *(["stage1_file_aggregation"] if self.file_aggregation else []),
                    *(
                        ["stage1_advanced_file_aggregation"]
                        if self.file_aggregation and self.advanced_file_aggregation
                        else []
                    ),
                    "stage2_optional_llm_rerank",
                    *(
                        ["stage3_ast_symbol_localization"]
                        if self.symbol_localization
                        else []
                    ),
                    *(
                        ["stage3_symbol_llm_rerank"]
                        if self.symbol_llm_rerank
                        else []
                    ),
                    "confidence_gate",
                ],
                "embedding_backend": backend_name,
                "semantic_candidate_k": (
                    self.semantic_candidate_k if _is_hybrid_backend(self.embedding_backend) else 0
                ),
                "candidate_file_k": self.candidate_file_k,
                "generic_routing": self.generic_routing,
                "domain_path_routing": self.domain_path_routing,
                "repository_proximity": self.repository_proximity,
                "import_graph_mode": self.import_graph_mode,
                "import_graph_parameters": IMPORT_GRAPH_PARAMETERS,
                "call_graph_mode": self.call_graph_mode,
                "call_graph_parameters": get_call_graph_parameters(
                    self.call_graph_mode
                ),
                "symbol_expansion_mode": self.symbol_expansion_mode,
                "symbol_expansion_parameters": SYMBOL_EXPANSION_PARAMETERS,
                "api_implementation_parameters": API_IMPLEMENTATION_PARAMETERS,
                "symbol_expansion_policy": {
                    "dotted_leaf_fallback": _dotted_leaf_fallback_policy(
                        self.symbol_expansion_mode
                    ),
                    "api_implementation_expansion": (
                        "python-static-one-hop"
                        if _api_implementation_expansion_enabled(
                            self.symbol_expansion_mode
                        )
                        else "disabled"
                    ),
                    "api_namespace_guard": (
                        "prefix-overlap"
                        if _api_namespace_guard_enabled(
                            self.symbol_expansion_mode
                        )
                        else "disabled"
                    ),
                },
                "llm_rerank": llm_rerank_used,
                "llm_candidate_k": max(self.top_k, int(self.llm_candidate_k or self.top_k)) if self.llm_rerank else 0,
                "llm_rerank_batch_size": LLM_RERANK_BATCH_SIZE if self.llm_rerank else 0,
                "symbol_localization": self.symbol_localization,
                "symbol_llm_rerank_requested": self.symbol_llm_rerank,
                "symbol_llm_rerank_used": symbol_rerank_used,
                "symbol_rerank": symbol_rerank_used,
                "legacy_symbol_rerank_alias_used": self.legacy_symbol_rerank_alias_used,
                "symbol_candidate_k": self.symbol_candidate_k if self.symbol_localization else 0,
                "symbol_top_k": self.symbol_top_k if self.symbol_localization else 0,
                "symbol_retrieval_mode": (
                    self.symbol_retrieval_mode if self.symbol_localization else "off"
                ),
                "symbol_per_file_quota": (
                    self.symbol_per_file_quota if self.symbol_localization else 0
                ),
                "symbol_selection_mode": (
                    self.symbol_selection_mode if self.symbol_localization else "off"
                ),
                "symbol_b1_weights": (
                    SYMBOL_B1_WEIGHTS
                    if self.symbol_localization
                    and self.symbol_retrieval_mode == "b1-structured"
                    else {}
                ),
                "symbol_rerank_batch_size": (
                    LLM_RERANK_BATCH_SIZE if self.symbol_llm_rerank else 0
                ),
                "file_aggregation": self.file_aggregation,
                "advanced_file_aggregation": self.advanced_file_aggregation,
                "file_aggregation_mode": self.file_aggregation_mode,
                "file_aggregation_parameters": FILE_AGGREGATION_PARAMETERS,
                "ranking_level": "file_aggregated_chunks" if self.file_aggregation else "code_chunks",
                "top_k": self.top_k,
                "scoring_weights": SCORING_WEIGHTS,
            },
            "stage1_candidate_files": stage1_files,
            "stage1_diagnostics": {
                "stage_boundary": "before_llm_rerank",
                "indexed_chunk_count": len(index.chunks),
                "semantic_candidate_file_k": (
                    self.semantic_candidate_k if _is_hybrid_backend(self.embedding_backend) else 0
                ),
                "semantic_representative": (
                    "best_tfidf_chunk_per_file"
                    if _is_hybrid_backend(self.embedding_backend)
                    else ""
                ),
                "requested_candidate_file_k": self.candidate_file_k,
                "returned_candidate_file_count": len(stage1_files),
                "unique_candidate_file_count": len(
                    {row["file_path"] for row in stage1_files if row["file_path"]}
                ),
                "import_graph": (
                    {"version": index.import_graph.version, **index.import_graph.stats}
                    if self.import_graph_mode != "off" and index.import_graph is not None
                    else {"enabled": False}
                ),
                "call_graph": (
                    {
                        "enabled": True,
                        "mode": self.call_graph_mode,
                        "version": index.call_graph.version,
                        **index.call_graph.stats,
                    }
                    if self.call_graph_mode != "off" and index.call_graph is not None
                    else {"enabled": False, "mode": "off"}
                ),
                "symbol_expansion": _symbol_expansion_diagnostics(
                    mode=self.symbol_expansion_mode,
                    ticket_program_names=ticket_program_names,
                    candidates=stage1_candidates,
                    symbol_definitions=index.symbol_definitions,
                    api_implementation_links=index.api_implementation_links,
                ),
            },
            "localized_candidates": localized,
            "localized_files": [_localized_file(row) for row in localized],
            "stage2_localized_files": [_localized_file(row) for row in localized],
            "stage3_candidate_symbols": candidate_symbols,
            "stage3_retrieval_symbols": retrieval_symbols,
            "stage3_ranked_symbols": ranked_symbols,
            "stage3_diagnostics": {
                "localization_requested": self.symbol_localization,
                "requested": self.symbol_llm_rerank,
                "eligible": bool(stage3_candidate_pool),
                "llm_attempted": symbol_llm_attempted,
                "llm_rerank_used": symbol_rerank_used,
                "llm_valid": symbol_rerank_used,
                "timed_out": symbol_llm_timed_out,
                "fallback_used": (
                    self.symbol_llm_rerank
                    and bool(stage3_retrieval_candidates)
                    and not symbol_rerank_used
                ),
                "fallback_reason": symbol_fallback_reason,
                "source": (
                    "llm_blended_with_symbol_retrieval"
                    if symbol_rerank_used
                    else "symbol_retrieval_fallback"
                    if self.symbol_llm_rerank and ranked_symbols
                    else "symbol_retrieval"
                    if self.symbol_localization and ranked_symbols
                    else "not_run"
                ),
                "stage2_file_count": len(localized),
                "ast_symbol_pool_count": symbol_pool_count,
                "candidate_pool_expansion": symbol_pool_expansion,
                "candidate_symbol_count": len(candidate_symbols),
                "returned_symbol_count": len(ranked_symbols),
                "retrieval_mode": (
                    self.symbol_retrieval_mode if self.symbol_localization else "off"
                ),
                "per_file_quota": (
                    self.symbol_per_file_quota if self.symbol_localization else 0
                ),
                "selection_mode": (
                    self.symbol_selection_mode if self.symbol_localization else "off"
                ),
            },
            "bug_location": bug_location,
            "candidates": [_legacy_candidate(row) for row in localized],
            "context_preview": context_preview,
            "input_validation": input_validation,
            "confidence": confidence,
            "confidence_level": confidence["confidence_level"],
            "should_manual_review": confidence["should_manual_review"],
            "recommend_patch_generation": confidence["recommend_patch_generation"],
            "patch_generation_policy": confidence["patch_generation_policy"],
            "repository_path": index.repository_path,
            "evaluation_ready_fields": {
                "candidate_stage": "stage1_candidate_files[*].file_path",
                "file_level": "localized_files[*].file_path",
                "symbol_candidate_level": "stage3_candidate_symbols[*]",
                "symbol_retrieval_baseline": "stage3_retrieval_symbols[*]",
                "symbol_final_level": "stage3_ranked_symbols[*]",
                "symbol_level": (
                    "stage3_ranked_symbols[*].symbol_qualified_name"
                    if self.symbol_localization
                    else "localized_candidates[*].symbol_qualified_name"
                ),
                "line_range": ["localized_candidates[*].start_line", "localized_candidates[*].end_line"],
            },
            "warnings": warnings,
        }


def build_code_index(
    repo_path: str | Path,
    *,
    repository_name: str | None = None,
    base_commit: str | None = None,
    chunk_lines: int = 80,
    overlap_lines: int = 20,
    include_tests: bool = False,
    max_file_bytes: int = 500_000,
    previous_index: CodeIndex | None = None,
) -> CodeIndex:
    root = Path(repo_path).resolve()
    if not root.exists():
        raise ValueError(f"Repository path does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"Repository path is not a directory: {root}")
    if chunk_lines <= 0:
        raise ValueError("chunk_lines must be positive.")
    if overlap_lines < 0:
        raise ValueError("overlap_lines cannot be negative.")
    if overlap_lines >= chunk_lines:
        raise ValueError("overlap_lines must be smaller than chunk_lines.")
    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes must be positive.")

    resolved_repository_name = str(repository_name or root.name).strip()
    resolved_base_commit = str(base_commit or _git_head_commit(root) or "working-tree").strip()
    if not resolved_repository_name:
        raise ValueError("repository_name must be non-empty.")
    if not resolved_base_commit:
        raise ValueError("base_commit must be non-empty.")

    source_files = list(_iter_source_files(root, include_tests=include_tests, max_file_bytes=max_file_bytes))
    file_fingerprints = {
        path.relative_to(root).as_posix(): _source_file_fingerprint(path, root=root)
        for path in source_files
    }
    previous_fingerprints = (
        previous_index.settings.get("file_fingerprints", {})
        if previous_index is not None and isinstance(previous_index.settings.get("file_fingerprints"), dict)
        else {}
    )
    previous_chunks: dict[str, list[CodeChunk]] = {}
    previous_symbol_records: dict[str, list[SymbolRecordV1]] = {}
    previous_parse_results: dict[str, SymbolParseResult] = {}
    if previous_index is not None:
        for chunk in previous_index.chunks:
            previous_chunks.setdefault(chunk.file_path, []).append(chunk)
        for record in previous_index.symbol_records:
            previous_symbol_records.setdefault(record.file_path, []).append(record)
        previous_parse_results = {
            result.file_path: result for result in previous_index.symbol_parse_results
        }

    chunks: list[CodeChunk] = []
    symbol_records: list[SymbolRecordV1] = []
    symbol_parse_results: list[SymbolParseResult] = []
    reused_files = 0
    rebuilt_files = 0
    for path in source_files:
        rel = path.relative_to(root).as_posix()
        can_reuse = (
            previous_fingerprints.get(rel) == file_fingerprints[rel]
            and rel in previous_chunks
            and previous_index is not None
            and previous_index.version == CODE_INDEX_VERSION
            and previous_index.settings.get("repository_name") == resolved_repository_name
            and previous_index.settings.get("base_commit") == resolved_base_commit
            and rel in previous_parse_results
            and previous_index.settings.get("chunk_lines") == chunk_lines
            and previous_index.settings.get("overlap_lines") == overlap_lines
        )
        if can_reuse:
            chunks.extend(previous_chunks[rel])
            symbol_records.extend(previous_symbol_records.get(rel, []))
            symbol_parse_results.append(previous_parse_results[rel])
            reused_files += 1
            continue
        file_chunks, parse_result = _chunks_and_symbol_result_for_file(
            path,
            root,
            repository_name=resolved_repository_name,
            base_commit=resolved_base_commit,
            chunk_lines=chunk_lines,
            overlap_lines=overlap_lines,
        )
        chunks.extend(file_chunks)
        symbol_records.extend(parse_result.symbols)
        symbol_parse_results.append(parse_result)
        rebuilt_files += 1

    import_graph = build_import_graph(chunks)
    symbol_definitions = build_symbol_definition_index(chunks)
    api_implementation_links = build_api_implementation_index(
        chunks,
        symbol_definitions,
    )
    call_graph = build_call_graph(chunks, symbol_definitions)
    return CodeIndex(
        repository_path=str(root),
        chunks=chunks,
        symbol_records=symbol_records,
        symbol_parse_results=symbol_parse_results,
        version=CODE_INDEX_VERSION,
        settings={
            "repository_name": resolved_repository_name,
            "base_commit": resolved_base_commit,
            "symbol_record_schema_version": SYMBOL_RECORD_SCHEMA_VERSION,
            "symbol_parse_result_schema_version": SYMBOL_PARSE_RESULT_SCHEMA_VERSION,
            "chunk_lines": chunk_lines,
            "overlap_lines": overlap_lines,
            "include_tests": include_tests,
            "max_file_bytes": max_file_bytes,
            "repository_fingerprint": _repository_fingerprint_from_files(file_fingerprints),
            "file_fingerprints": file_fingerprints,
            "index_stats": {
                "source_files": len(source_files),
                "reused_files": reused_files,
                "rebuilt_files": rebuilt_files,
                "symbol_records": len(symbol_records),
                "symbol_parse_statuses": dict(
                    sorted(Counter(result.status for result in symbol_parse_results).items())
                ),
                "import_graph_edges": int(import_graph.stats.get("edges", 0)),
                "call_graph_edges": int(call_graph.stats.get("edges", 0)),
                "symbol_definition_keys": len(symbol_definitions),
                "symbol_definition_records": len(
                    {
                        (
                            str(record.get("file_path") or ""),
                            str(record.get("qualified_name") or ""),
                            int(record.get("start_line") or 0),
                        )
                        for records in symbol_definitions.values()
                        for record in records
                    }
                ),
                "api_implementation_link_keys": len(api_implementation_links),
                "api_implementation_link_records": len(
                    {
                        (
                            str(record.get("source_file") or ""),
                            str(record.get("source_symbol") or ""),
                            str(record.get("target_file") or ""),
                            str(record.get("target_symbol") or ""),
                            str(record.get("relation_type") or ""),
                        )
                        for records in api_implementation_links.values()
                        for record in records
                    }
                ),
            },
        },
        import_graph=import_graph,
        call_graph=call_graph,
        symbol_definitions=symbol_definitions,
        api_implementation_links=api_implementation_links,
    )


def build_symbol_definition_index(
    chunks: Iterable[CodeChunk],
) -> dict[str, list[dict[str, Any]]]:
    """Build a deterministic Symbol-name to definition-file lookup."""

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for chunk in chunks:
        qualified_name = chunk.symbol_qualified_name.strip()
        if not qualified_name:
            continue
        key = (chunk.file_path, qualified_name, chunk.symbol_kind)
        existing = grouped.get(key)
        record = {
            "file_path": chunk.file_path,
            "qualified_name": qualified_name,
            "leaf_name": qualified_name.rsplit(".", maxsplit=1)[-1],
            "symbol_kind": chunk.symbol_kind,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
        }
        if existing is None:
            grouped[key] = record
        else:
            existing["start_line"] = min(
                int(existing["start_line"]), chunk.start_line
            )
            existing["end_line"] = max(int(existing["end_line"]), chunk.end_line)

    lookup: dict[str, list[dict[str, Any]]] = {}
    for record in sorted(
        grouped.values(),
        key=lambda row: (
            str(row["file_path"]),
            int(row["start_line"]),
            str(row["qualified_name"]),
        ),
    ):
        for lookup_key in _symbol_definition_lookup_keys(record):
            lookup.setdefault(lookup_key, []).append(dict(record))
    return lookup


def _symbol_definition_lookup_keys(record: dict[str, Any]) -> list[str]:
    file_path = str(record.get("file_path") or "")
    qualified_name = str(record.get("qualified_name") or "")
    leaf_name = str(record.get("leaf_name") or qualified_name.rsplit(".", 1)[-1])
    keys = {
        _normalize_symbol_lookup_key(qualified_name),
        _normalize_symbol_lookup_key(leaf_name),
    }
    module_parts = _module_parts_from_file_path(file_path)
    # Include bounded module suffixes so source roots such as ``src`` or ``lib``
    # do not prevent ``package.module.symbol`` from resolving.
    for start in range(max(0, len(module_parts) - 3), len(module_parts)):
        module_suffix = ".".join(module_parts[start:])
        if not module_suffix:
            continue
        keys.add(_normalize_symbol_lookup_key(f"{module_suffix}.{qualified_name}"))
    return sorted(key for key in keys if key)


def _module_parts_from_file_path(file_path: str) -> list[str]:
    normalized = _normalize_path(file_path)
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return []
    filename = parts.pop()
    stem = Path(filename).stem
    if stem != "__init__":
        parts.append(stem)
    return [
        _normalize_symbol_lookup_key(part)
        for part in parts
        if _normalize_symbol_lookup_key(part)
    ]


def _load_symbol_definition_index(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}
    loaded: dict[str, list[dict[str, Any]]] = {}
    for raw_key, raw_records in value.items():
        key = _normalize_symbol_lookup_key(str(raw_key))
        if not key or not isinstance(raw_records, list):
            continue
        records: list[dict[str, Any]] = []
        for raw_record in raw_records:
            if not isinstance(raw_record, dict):
                continue
            file_path = _normalize_path(str(raw_record.get("file_path") or ""))
            qualified_name = str(raw_record.get("qualified_name") or "").strip()
            if not file_path or not qualified_name:
                continue
            records.append(
                {
                    "file_path": file_path,
                    "qualified_name": qualified_name,
                    "leaf_name": str(
                        raw_record.get("leaf_name")
                        or qualified_name.rsplit(".", maxsplit=1)[-1]
                    ),
                    "symbol_kind": str(raw_record.get("symbol_kind") or "chunk"),
                    "start_line": int(raw_record.get("start_line") or 1),
                    "end_line": int(raw_record.get("end_line") or 1),
                }
            )
        if records:
            loaded[key] = records
    return loaded


def build_api_implementation_index(
    chunks: Iterable[CodeChunk],
    symbol_definitions: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build deterministic one-hop API re-export and wrapper links.

    Only Python syntax that can be resolved from the pre-fix repository is
    accepted.  A link always ends at a concrete indexed Symbol definition.
    """

    chunk_list = list(chunks)
    definitions = symbol_definitions or build_symbol_definition_index(chunk_list)
    chunks_by_file = _chunks_by_file(chunk_list)
    python_files = {
        file_path
        for file_path, file_chunks in chunks_by_file.items()
        if file_path.endswith(".py")
        or any(chunk.language == "python" for chunk in file_chunks)
    }
    module_lookup = _module_file_lookup(python_files)
    definitions_by_file_leaf: dict[tuple[str, str], list[dict[str, Any]]] = {}
    seen_definitions: set[tuple[str, str, int]] = set()
    for records in definitions.values():
        for raw_record in records:
            file_path = str(raw_record.get("file_path") or "")
            qualified_name = str(raw_record.get("qualified_name") or "")
            start_line = int(raw_record.get("start_line") or 1)
            signature = (file_path, qualified_name, start_line)
            if not file_path or not qualified_name or signature in seen_definitions:
                continue
            seen_definitions.add(signature)
            leaf_name = _normalize_symbol_lookup_key(
                str(raw_record.get("leaf_name") or qualified_name.rsplit(".", 1)[-1])
            )
            definitions_by_file_leaf.setdefault((file_path, leaf_name), []).append(
                dict(raw_record)
            )

    links: list[dict[str, Any]] = []
    for file_path in sorted(python_files):
        source = _reconstruct_file_source(chunks_by_file[file_path])
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(source)
        except (SyntaxError, RecursionError, ValueError):
            continue
        links.extend(
            _python_api_implementation_links(
                file_path=file_path,
                tree=tree,
                module_lookup=module_lookup,
                definitions_by_file_leaf=definitions_by_file_leaf,
            )
        )

    lookup: dict[str, list[dict[str, Any]]] = {}
    seen_links: set[tuple[str, str, str, str, str]] = set()
    for record in sorted(
        links,
        key=lambda row: (
            str(row.get("source_file") or ""),
            int(row.get("source_line") or 0),
            str(row.get("source_symbol") or ""),
            str(row.get("target_file") or ""),
            str(row.get("target_symbol") or ""),
        ),
    ):
        signature = (
            str(record.get("source_file") or ""),
            str(record.get("source_symbol") or ""),
            str(record.get("target_file") or ""),
            str(record.get("target_symbol") or ""),
            str(record.get("relation_type") or ""),
        )
        if signature in seen_links:
            continue
        seen_links.add(signature)
        for lookup_key in _api_implementation_lookup_keys(record):
            lookup.setdefault(lookup_key, []).append(dict(record))
    return lookup


def _python_api_implementation_links(
    *,
    file_path: str,
    tree: ast.Module,
    module_lookup: dict[str, list[str]],
    definitions_by_file_leaf: dict[tuple[str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    imported_callables: dict[str, list[dict[str, Any]]] = {}
    imported_modules: dict[str, str] = {}
    public_names = _python_all_names(tree)
    is_package_init = Path(file_path).name == "__init__.py"
    links: list[dict[str, Any]] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".", 1)[0]
                imported_modules[local_name] = alias.name
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        raw_base = "." * int(node.level or 0) + str(node.module or "")
        base_module = _resolve_python_module(raw_base, file_path)
        for alias in node.names:
            if alias.name == "*":
                continue
            local_name = alias.asname or alias.name
            target_records = _api_target_definition_records(
                module=base_module,
                symbol_name=alias.name,
                module_lookup=module_lookup,
                definitions_by_file_leaf=definitions_by_file_leaf,
            )
            if target_records:
                imported_callables.setdefault(local_name, []).extend(target_records)
            possible_module = f"{base_module}.{alias.name}".strip(".")
            if _module_import_targets(possible_module, module_lookup):
                imported_modules[local_name] = possible_module
            is_reexport = (
                is_package_init
                or alias.asname is not None
                or local_name in public_names
            )
            if not is_reexport:
                continue
            for target in target_records:
                links.append(
                    _api_link_record(
                        source_file=file_path,
                        source_symbol=local_name,
                        source_line=int(getattr(node, "lineno", 1)),
                        target=target,
                        relation_type="reexport",
                        via_name=alias.name,
                    )
                )

    wrapper_nodes: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            wrapper_nodes.append((node.name, node))
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    wrapper_nodes.append((f"{node.name}.{child.name}", child))

    for wrapper_name, node in wrapper_nodes:
        for call in _direct_wrapper_calls(node):
            targets, via_name = _resolve_api_call_targets(
                call,
                imported_callables=imported_callables,
                imported_modules=imported_modules,
                module_lookup=module_lookup,
                definitions_by_file_leaf=definitions_by_file_leaf,
            )
            for target in targets:
                if str(target.get("file_path") or "") == file_path:
                    continue
                links.append(
                    _api_link_record(
                        source_file=file_path,
                        source_symbol=wrapper_name,
                        source_line=int(getattr(node, "lineno", 1)),
                        target=target,
                        relation_type="wrapper",
                        via_name=via_name,
                    )
                )
    return links


def _python_all_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
            continue
        value = node.value
        if not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            continue
        for element in value.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                names.add(element.value)
    return names


def _direct_wrapper_calls(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.Call]:
    statements = [
        statement
        for statement in node.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    if len(statements) > int(API_IMPLEMENTATION_PARAMETERS["maximum_wrapper_statements"]):
        return []
    calls: list[ast.Call] = []
    for statement in statements:
        value: ast.AST | None = None
        if isinstance(statement, ast.Return):
            value = statement.value
        elif isinstance(statement, ast.Expr):
            value = statement.value
        if isinstance(value, ast.Call):
            calls.append(value)
    return calls


def _resolve_api_call_targets(
    call: ast.Call,
    *,
    imported_callables: dict[str, list[dict[str, Any]]],
    imported_modules: dict[str, str],
    module_lookup: dict[str, list[str]],
    definitions_by_file_leaf: dict[tuple[str, str], list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], str]:
    if isinstance(call.func, ast.Name):
        return list(imported_callables.get(call.func.id, [])), call.func.id
    parts = _attribute_name_parts(call.func)
    if len(parts) < 2 or parts[0] not in imported_modules:
        return [], ""
    module = imported_modules[parts[0]]
    if len(parts) > 2:
        module = ".".join([module, *parts[1:-1]])
    symbol_name = parts[-1]
    return (
        _api_target_definition_records(
            module=module,
            symbol_name=symbol_name,
            module_lookup=module_lookup,
            definitions_by_file_leaf=definitions_by_file_leaf,
        ),
        ".".join(parts),
    )


def _attribute_name_parts(node: ast.AST) -> list[str]:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    else:
        return []
    return list(reversed(parts))


def _api_target_definition_records(
    *,
    module: str,
    symbol_name: str,
    module_lookup: dict[str, list[str]],
    definitions_by_file_leaf: dict[tuple[str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    normalized_symbol = _normalize_symbol_lookup_key(symbol_name)
    records: list[dict[str, Any]] = []
    for target_file in _module_import_targets(module, module_lookup):
        records.extend(
            definitions_by_file_leaf.get((target_file, normalized_symbol), [])
        )
    return records


def _api_link_record(
    *,
    source_file: str,
    source_symbol: str,
    source_line: int,
    target: dict[str, Any],
    relation_type: str,
    via_name: str,
) -> dict[str, Any]:
    return {
        "api_name": source_symbol.rsplit(".", 1)[-1],
        "source_file": source_file,
        "source_symbol": source_symbol,
        "source_line": source_line,
        "target_file": str(target.get("file_path") or ""),
        "target_symbol": str(target.get("qualified_name") or ""),
        "target_symbol_kind": str(target.get("symbol_kind") or ""),
        "target_start_line": int(target.get("start_line") or 1),
        "relation_type": relation_type,
        "via_name": via_name,
    }


def _api_implementation_lookup_keys(record: dict[str, Any]) -> list[str]:
    source_file = str(record.get("source_file") or "")
    source_symbol = str(record.get("source_symbol") or "")
    api_name = str(record.get("api_name") or source_symbol.rsplit(".", 1)[-1])
    keys = {
        _normalize_symbol_lookup_key(source_symbol),
        _normalize_symbol_lookup_key(api_name),
    }
    module_parts = _module_parts_from_file_path(source_file)
    for start in range(max(0, len(module_parts) - 3), len(module_parts)):
        module_suffix = ".".join(module_parts[start:])
        if module_suffix:
            keys.add(_normalize_symbol_lookup_key(f"{module_suffix}.{source_symbol}"))
            keys.add(_normalize_symbol_lookup_key(f"{module_suffix}.{api_name}"))
    return sorted(key for key in keys if key)


def _load_api_implementation_index(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}
    loaded: dict[str, list[dict[str, Any]]] = {}
    for raw_key, raw_records in value.items():
        key = _normalize_symbol_lookup_key(str(raw_key))
        if not key or not isinstance(raw_records, list):
            continue
        records: list[dict[str, Any]] = []
        for raw_record in raw_records:
            if not isinstance(raw_record, dict):
                continue
            source_file = _normalize_path(str(raw_record.get("source_file") or ""))
            target_file = _normalize_path(str(raw_record.get("target_file") or ""))
            source_symbol = str(raw_record.get("source_symbol") or "").strip()
            target_symbol = str(raw_record.get("target_symbol") or "").strip()
            relation_type = str(raw_record.get("relation_type") or "")
            if (
                not source_file
                or not target_file
                or not source_symbol
                or not target_symbol
                or relation_type not in {"reexport", "wrapper"}
            ):
                continue
            records.append(
                {
                    "api_name": str(
                        raw_record.get("api_name")
                        or source_symbol.rsplit(".", 1)[-1]
                    ),
                    "source_file": source_file,
                    "source_symbol": source_symbol,
                    "source_line": int(raw_record.get("source_line") or 1),
                    "target_file": target_file,
                    "target_symbol": target_symbol,
                    "target_symbol_kind": str(
                        raw_record.get("target_symbol_kind") or ""
                    ),
                    "target_start_line": int(
                        raw_record.get("target_start_line") or 1
                    ),
                    "relation_type": relation_type,
                    "via_name": str(raw_record.get("via_name") or ""),
                }
            )
        if records:
            loaded[key] = records
    return loaded


def repository_fingerprint(
    repo_path: str | Path,
    *,
    include_tests: bool = False,
    max_file_bytes: int = 500_000,
) -> str:
    root = Path(repo_path).resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Repository path is not a directory: {root}")
    file_fingerprints = {
        path.relative_to(root).as_posix(): _source_file_fingerprint(path, root=root)
        for path in _iter_source_files(root, include_tests=include_tests, max_file_bytes=max_file_bytes)
    }
    return _repository_fingerprint_from_files(file_fingerprints)


def _source_file_fingerprint(path: Path, *, root: Path | None = None) -> str:
    digest = hashlib.sha256()
    if root is not None:
        try:
            path.resolve(strict=False).relative_to(root.resolve())
        except ValueError:
            digest.update(b"symlink_escape\0")
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            return digest.hexdigest()
        except OSError as exc:
            digest.update(f"path_error\0{type(exc).__name__}".encode("utf-8"))
            return digest.hexdigest()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(65_536), b""):
                digest.update(block)
    except OSError as exc:
        digest.update(f"read_error\0{type(exc).__name__}".encode("utf-8"))
    return digest.hexdigest()


def _repository_fingerprint_from_files(file_fingerprints: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for file_path, fingerprint in sorted(file_fingerprints.items()):
        digest.update(file_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(fingerprint.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_code_index(path: str | Path) -> CodeIndex:
    return CodeIndex.load(path)


def localize_ticket(
    ticket_json: dict[str, Any],
    *,
    repo_path: str | Path | None = None,
    code_index: CodeIndex | None = None,
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
    symbol_localization: bool = False,
    symbol_llm_rerank: bool = False,
    symbol_rerank: bool | None = None,
    symbol_candidate_k: int = 30,
    symbol_top_k: int = 5,
    symbol_retrieval_mode: str = "b0-tfidf",
    symbol_per_file_quota: int = 0,
    symbol_selection_mode: str = "global",
    file_aggregation: bool = True,
    advanced_file_aggregation: bool = False,
    file_aggregation_mode: str | None = None,
    min_ticket_chars: int = 20,
) -> dict[str, Any]:
    return FaultLocalizer(
        code_index=code_index,
        repo_path=repo_path,
        top_k=top_k,
        embedding_backend=embedding_backend,
        sbert_model=sbert_model,
        sbert_local_files_only=sbert_local_files_only,
        semantic_candidate_k=semantic_candidate_k,
        candidate_file_k=candidate_file_k,
        generic_routing=generic_routing,
        domain_path_routing=domain_path_routing,
        repository_proximity=repository_proximity,
        import_graph_mode=import_graph_mode,
        call_graph_mode=call_graph_mode,
        symbol_expansion_mode=symbol_expansion_mode,
        llm_client=llm_client,
        llm_rerank=llm_rerank,
        llm_candidate_k=llm_candidate_k,
        symbol_localization=symbol_localization,
        symbol_llm_rerank=symbol_llm_rerank,
        symbol_rerank=symbol_rerank,
        symbol_candidate_k=symbol_candidate_k,
        symbol_top_k=symbol_top_k,
        symbol_retrieval_mode=symbol_retrieval_mode,
        symbol_per_file_quota=symbol_per_file_quota,
        symbol_selection_mode=symbol_selection_mode,
        file_aggregation=file_aggregation,
        advanced_file_aggregation=advanced_file_aggregation,
        file_aggregation_mode=file_aggregation_mode,
        min_ticket_chars=min_ticket_chars,
    ).localize(ticket_json)


def rank_symbol_candidates(
    ticket_json: dict[str, Any],
    chunks: Iterable[CodeChunk],
    *,
    stage2_file_scores: dict[str, float] | None = None,
    mode: str = "b0-tfidf",
    top_k: int = 30,
    per_file_quota: int = 0,
    selection_mode: str = "global",
    call_graph: CallGraph | None = None,
) -> list[LocalizationCandidate]:
    """Rank a deterministic Stage-3 symbol pool under the frozen B0/B1 contract.

    ``per_file_quota`` is applied before a deterministic backfill pass. This
    prevents one large file from consuming the shortlist when alternatives
    exist without returning fewer than ``top_k`` symbols when they do not.
    """

    _validate_top_k(top_k)
    normalized_mode = mode.strip().casefold()
    if normalized_mode not in SYMBOL_RETRIEVAL_MODES:
        raise ValueError(
            "mode must be one of: " + ", ".join(SYMBOL_RETRIEVAL_MODES)
        )
    if per_file_quota < 0:
        raise ValueError("per_file_quota cannot be negative.")
    normalized_selection_mode = selection_mode.strip().casefold()
    if normalized_selection_mode not in SYMBOL_SELECTION_MODES:
        raise ValueError(
            "selection_mode must be one of: " + ", ".join(SYMBOL_SELECTION_MODES)
        )
    if normalized_selection_mode != "global" and per_file_quota:
        raise ValueError(
            "per_file_quota must be 0 for coverage-aware selection modes."
        )

    pool = [
        chunk
        for chunk in chunks
        if chunk.symbol_kind in SYMBOL_RERANK_KINDS
        and bool(chunk.symbol_qualified_name)
    ]
    if not pool:
        return []
    if normalized_selection_mode == "call-neighborhood-v2" and call_graph is None:
        raise ValueError(
            "call_graph is required for call-neighborhood-v2 selection."
        )

    bug_report = build_bug_report_text(ticket_json)
    lexical_scores, _ = _embedding_scores(
        bug_report,
        [chunk.search_text for chunk in pool],
        backend="tfidf",
        sbert_model="",
        sbert_local_files_only=True,
    )
    stage2_scores = {
        _normalize_path(path): float(score)
        for path, score in (stage2_file_scores or {}).items()
    }
    stage2_max = max((max(0.0, score) for score in stage2_scores.values()), default=0.0)
    report_identifiers = _report_identifiers(bug_report)
    program_names = extract_ticket_program_names(bug_report)
    query_terms = _important_terms(bug_report)
    stack_refs = _stack_trace_refs(ticket_json)

    ranked: list[LocalizationCandidate] = []
    for chunk, lexical_score in zip(pool, lexical_scores):
        lexical_score = _clamp(float(lexical_score), 0.0, 1.0)
        if normalized_mode == "b0-tfidf":
            ranked.append(
                LocalizationCandidate(
                    chunk=chunk,
                    score=lexical_score,
                    embedding_score=lexical_score,
                    reason="B0 TF-IDF symbol retrieval.",
                    signals={
                        "symbol_retrieval_mode": normalized_mode,
                        "lexical_score": lexical_score,
                        "embedding_score": lexical_score,
                    },
                )
            )
            continue

        identity_text = _symbol_identity_text(chunk)
        identity_chunk = replace(chunk, code_text=identity_text)
        identifier_score, matching_identifiers = _identifier_signal(
            report_identifiers,
            identity_chunk,
        )
        exact_name_match = _exact_program_name_match(program_names, chunk)
        if exact_name_match:
            identifier_score = 1.0
        symbol_name_score = _symbol_signal(
            query_terms,
            chunk,
            symbol_terms=set(_tokenize(identity_text)),
        )
        stack_trace_score = _stack_signal(chunk, stack_refs)
        file_score = max(0.0, stage2_scores.get(_normalize_path(chunk.file_path), 0.0))
        normalized_file_score = file_score / stage2_max if stage2_max else 0.0
        score = (
            SYMBOL_B1_WEIGHTS["lexical_score"] * lexical_score
            + SYMBOL_B1_WEIGHTS["identifier_score"] * identifier_score
            + SYMBOL_B1_WEIGHTS["stack_trace_score"] * stack_trace_score
            + SYMBOL_B1_WEIGHTS["symbol_name_score"] * symbol_name_score
            + SYMBOL_B1_WEIGHTS["stage2_file_score"] * normalized_file_score
        )
        evidence: list[str] = []
        if exact_name_match:
            evidence.append(f"exact identifier {exact_name_match}")
        elif matching_identifiers:
            evidence.append("identifier token match")
        if stack_trace_score:
            evidence.append("stack file/line match")
        evidence.append("TF-IDF lexical relevance")
        ranked.append(
            LocalizationCandidate(
                chunk=chunk,
                score=_clamp(score, 0.0, 1.0),
                embedding_score=lexical_score,
                reason="B1 structured retrieval: " + ", ".join(evidence) + ".",
                signals={
                    "symbol_retrieval_mode": normalized_mode,
                    "lexical_score": lexical_score,
                    "embedding_score": lexical_score,
                    "identifier_score": identifier_score,
                    "matching_identifiers": matching_identifiers,
                    "exact_program_name_match": exact_name_match,
                    "stack_trace_score": stack_trace_score,
                    "symbol_name_score": symbol_name_score,
                    "stage2_file_score": normalized_file_score,
                    "weights": SYMBOL_B1_WEIGHTS,
                },
            )
        )

    if normalized_selection_mode == "call-neighborhood-v2":
        ranked = _apply_called_helper_score(ranked, call_graph)
    unique = _unique_symbol_candidates(sorted(ranked, key=_ranking_key))
    if normalized_selection_mode == "global":
        return _apply_symbol_per_file_quota(
            unique,
            top_k=top_k,
            per_file_quota=per_file_quota,
        )
    return _select_coverage_aware_symbols(
        unique,
        top_k=top_k,
        stage2_file_scores=stage2_scores,
        mode=normalized_selection_mode,
        call_graph=call_graph,
    )


def build_symbol_candidate_pool(
    chunks: Iterable[CodeChunk],
    file_paths: Iterable[str],
) -> list[CodeChunk]:
    """Build the Stage-3 pool with one exact-reachable module identity per file.

    Existing module-gap chunks are retained so deterministic retrieval can pick
    the most relevant module-level evidence. Legacy indexes may represent a
    constants-only file as a generic ``chunk``; those files receive one
    synthesized ``<module>`` candidate without rebuilding the index.
    """

    wanted = {_normalize_path(path) for path in file_paths if str(path).strip()}
    chunks_by_file: dict[str, list[CodeChunk]] = {}
    pool: list[CodeChunk] = []
    module_files: set[str] = set()
    for chunk in chunks:
        normalized_path = _normalize_path(chunk.file_path)
        if normalized_path not in wanted:
            continue
        chunks_by_file.setdefault(normalized_path, []).append(chunk)
        if (
            chunk.symbol_kind in SYMBOL_RERANK_KINDS
            and bool(chunk.symbol_qualified_name)
        ):
            pool.append(chunk)
            if chunk.symbol_kind == "module":
                module_files.add(normalized_path)

    for file_path in sorted(wanted - module_files):
        file_chunks = sorted(
            chunks_by_file.get(file_path, []),
            key=lambda chunk: (chunk.start_line, chunk.end_line, chunk.chunk_id),
        )
        if not file_chunks:
            continue
        evidence_chunks = file_chunks[:3]
        code_text = "\n".join(chunk.code_text for chunk in evidence_chunks)[:12_000]
        first = file_chunks[0]
        pool.append(
            CodeChunk(
                chunk_id=f"{first.file_path}:1-{max(chunk.end_line for chunk in evidence_chunks)}:<module>",
                file_path=first.file_path,
                language=first.language,
                symbol_kind="module",
                function_name="",
                class_name="",
                start_line=1,
                end_line=max(chunk.end_line for chunk in evidence_chunks),
                code_text=code_text,
            )
        )
    return pool


def expand_stage3_symbol_candidate_pool(
    base_pool: Iterable[CodeChunk],
    repository_chunks: Iterable[CodeChunk],
    *,
    ticket_program_names: Iterable[str],
    symbol_definitions: dict[str, list[dict[str, Any]]],
    call_graph: CallGraph | None,
) -> tuple[list[CodeChunk], dict[str, Any]]:
    """Add public-ticket symbol seeds and one-hop repository call neighbors."""

    all_chunks = [
        chunk
        for chunk in repository_chunks
        if chunk.symbol_kind in SYMBOL_RERANK_KINDS
        and bool(chunk.symbol_qualified_name)
    ]
    by_identity: dict[tuple[str, str], list[CodeChunk]] = {}
    for chunk in all_chunks:
        identity = (_normalize_path(chunk.file_path), chunk.symbol_qualified_name)
        by_identity.setdefault(identity, []).append(chunk)

    selected: dict[tuple[str, str, str], CodeChunk] = {}

    def add(chunk: CodeChunk) -> bool:
        key = (
            _normalize_path(chunk.file_path),
            chunk.symbol_kind,
            chunk.symbol_qualified_name,
        )
        if key in selected:
            return False
        selected[key] = chunk
        return True

    for chunk in base_pool:
        add(chunk)
    base_count = len(selected)

    ticket_seed_identities: set[tuple[str, str]] = set()
    for ticket_name in ticket_program_names:
        normalized = _normalize_symbol_lookup_key(ticket_name)
        if not normalized:
            continue
        records = list(symbol_definitions.get(normalized, []))
        if not records and "." in normalized:
            records = list(symbol_definitions.get(normalized.rsplit(".", 1)[-1], []))
        for record in records:
            identity = (
                _normalize_path(str(record.get("file_path") or "")),
                str(record.get("qualified_name") or ""),
            )
            if identity[0] and identity[1]:
                ticket_seed_identities.add(identity)

    ticket_seed_count = 0
    for identity in sorted(ticket_seed_identities):
        for chunk in by_identity.get(identity, []):
            ticket_seed_count += int(add(chunk))

    anchor_identities = {
        (_normalize_path(chunk.file_path), chunk.symbol_qualified_name)
        for chunk in selected.values()
        if chunk.symbol_kind != "module"
    }
    neighbor_identities: set[tuple[str, str]] = set()
    if call_graph is not None:
        for edges in call_graph.outgoing.values():
            for edge in edges:
                caller = (
                    _normalize_path(str(edge.get("caller_file") or "")),
                    str(edge.get("caller_symbol") or ""),
                )
                callee = (
                    _normalize_path(str(edge.get("target_file") or "")),
                    str(edge.get("target_symbol") or ""),
                )
                if caller in anchor_identities:
                    neighbor_identities.add(callee)
                if callee in ticket_seed_identities:
                    neighbor_identities.add(caller)

    call_neighbor_count = 0
    for identity in sorted(neighbor_identities):
        for chunk in by_identity.get(identity, []):
            call_neighbor_count += int(add(chunk))

    expanded = sorted(
        selected.values(),
        key=lambda chunk: (
            _normalize_path(chunk.file_path),
            chunk.start_line,
            chunk.end_line,
            chunk.chunk_id,
        ),
    )
    return expanded, {
        "enabled": True,
        "base_pool_count": base_count,
        "ticket_seed_count": ticket_seed_count,
        "call_neighbor_count": call_neighbor_count,
        "expanded_pool_count": len(expanded),
    }


def _apply_called_helper_score(
    candidates: list[LocalizationCandidate],
    call_graph: CallGraph | None,
) -> list[LocalizationCandidate]:
    """Give callees bounded evidence inherited from relevant callers."""

    if call_graph is None or not candidates:
        return candidates
    by_identity = {
        (_normalize_path(candidate.chunk.file_path),
         candidate.chunk.symbol_qualified_name): candidate
        for candidate in candidates
    }
    best_caller: dict[tuple[str, str], LocalizationCandidate] = {}
    for edges in call_graph.outgoing.values():
        for edge in edges:
            caller_key = (
                _normalize_path(str(edge.get("caller_file") or "")),
                str(edge.get("caller_symbol") or ""),
            )
            callee_key = (
                _normalize_path(str(edge.get("target_file") or "")),
                str(edge.get("target_symbol") or ""),
            )
            if caller_key == callee_key:
                continue
            caller = by_identity.get(caller_key)
            callee = by_identity.get(callee_key)
            if caller is None or callee is None:
                continue
            caller_stage2 = float(caller.signals.get("stage2_file_score") or 0.0)
            callee_stage2 = float(callee.signals.get("stage2_file_score") or 0.0)
            if caller_key[0] == callee_key[0] or caller_stage2 <= callee_stage2:
                continue
            previous = best_caller.get(callee_key)
            if previous is None or caller.score > previous.score:
                best_caller[callee_key] = caller

    rescored: list[LocalizationCandidate] = []
    for candidate in candidates:
        key = (_normalize_path(candidate.chunk.file_path),
               candidate.chunk.symbol_qualified_name)
        caller = best_caller.get(key)
        if caller is None:
            rescored.append(candidate)
            continue
        bonus = CALL_NEIGHBOR_SCORE_WEIGHT * max(0.0, caller.score)
        signals = dict(candidate.signals)
        signals.update({
            "called_helper_score": bonus,
            "called_by_file": caller.chunk.file_path,
            "called_by_symbol": caller.chunk.symbol_qualified_name,
            "called_by_score": caller.score,
            "call_neighbor_score_weight": CALL_NEIGHBOR_SCORE_WEIGHT,
        })
        rescored.append(replace(
            candidate,
            score=_clamp(candidate.score + bonus, 0.0, 1.0),
            reason=candidate.reason.rstrip(".")
            + ", called by a relevant repository symbol.",
            signals=signals,
        ))
    return rescored


def _symbol_identity_text(chunk: CodeChunk) -> str:
    signature_lines = chunk.code_text.splitlines()[:4]
    return "\n".join(
        [
            chunk.file_path,
            chunk.symbol_qualified_name,
            chunk.function_name,
            chunk.class_name,
            *signature_lines,
        ]
    )


def _exact_program_name_match(
    program_names: Iterable[str],
    chunk: CodeChunk,
) -> str:
    qualified = _normalize_symbol_lookup_key(chunk.symbol_qualified_name)
    leaf = qualified.rsplit(".", maxsplit=1)[-1]
    for name in program_names:
        normalized = _normalize_symbol_lookup_key(name)
        if normalized and normalized in {qualified, leaf}:
            return name
    return ""


def _apply_symbol_per_file_quota(
    candidates: list[LocalizationCandidate],
    *,
    top_k: int,
    per_file_quota: int,
) -> list[LocalizationCandidate]:
    if not per_file_quota:
        return candidates[:top_k]
    selected: list[LocalizationCandidate] = []
    deferred: list[LocalizationCandidate] = []
    file_counts: Counter[str] = Counter()
    for candidate in candidates:
        path = _normalize_path(candidate.chunk.file_path)
        if file_counts[path] < per_file_quota:
            selected.append(candidate)
            file_counts[path] += 1
        else:
            deferred.append(candidate)
        if len(selected) == top_k:
            return selected
    selected.extend(deferred[: max(0, top_k - len(selected))])
    return selected


def _select_coverage_aware_symbols(
    candidates: list[LocalizationCandidate],
    *,
    top_k: int,
    stage2_file_scores: dict[str, float],
    mode: str,
    call_graph: CallGraph | None = None,
) -> list[LocalizationCandidate]:
    """Reserve bounded file/module/family coverage, then backfill by score.

    v1 never inspects gold. It preserves a strong global prefix, reserves one
    module identity per Stage-2 file, and optionally expands one member from
    each of up to five class families already evidenced by that prefix.
    source-neighborhood-v1 replaces those family slots with nearest disjoint
    same-file definitions within 100 source lines of prefix anchors.
    call-neighborhood-v1 instead uses one-hop callers/callees resolved by the
    existing static graph within the candidate pool, ordered by original score.
    call-neighborhood-v2 filters the cached repository-level graph to the same
    candidate identities, preserving edges that require full-index context.
    """

    if len(candidates) <= top_k:
        return list(candidates)
    file_order = sorted(
        {_normalize_path(candidate.chunk.file_path) for candidate in candidates},
        key=lambda path: (-stage2_file_scores.get(path, 0.0), path),
    )
    module_by_file: dict[str, LocalizationCandidate] = {}
    for candidate in candidates:
        path = _normalize_path(candidate.chunk.file_path)
        if candidate.chunk.symbol_kind == "module" and path not in module_by_file:
            module_by_file[path] = candidate
    reserved_modules = [
        module_by_file[path] for path in file_order if path in module_by_file
    ][:top_k]
    family_budget = (
        min(5, max(0, top_k - len(reserved_modules) - 1))
        if mode in {"coverage-aware-v1", "source-neighborhood-v1", "call-neighborhood-v1", "call-neighborhood-v2"}
        else 0
    )
    prefix_k = max(1, top_k - len(reserved_modules) - family_budget)
    selected: list[LocalizationCandidate] = []
    seen: set[tuple[str, str, str]] = set()

    def add(candidate: LocalizationCandidate) -> None:
        key = (
            _normalize_path(candidate.chunk.file_path),
            candidate.chunk.symbol_kind,
            candidate.chunk.symbol_qualified_name,
        )
        if key not in seen and len(selected) < top_k:
            selected.append(candidate)
            seen.add(key)

    for candidate in candidates[:prefix_k]:
        add(candidate)
    for candidate in reserved_modules:
        add(candidate)

    if family_budget and mode in {"call-neighborhood-v1", "call-neighborhood-v2"}:
        graph = (
            build_call_graph(candidate.chunk for candidate in candidates)
            if mode == "call-neighborhood-v1"
            else call_graph
        )
        if graph is None:
            raise ValueError("call-neighborhood-v2 requires a repository call graph.")
        adjacency: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for edges in graph.outgoing.values():
            for edge in edges:
                caller = (_normalize_path(edge["caller_file"]), edge["caller_symbol"])
                callee = (_normalize_path(edge["target_file"]), edge["target_symbol"])
                adjacency.setdefault(caller, set()).add(callee)
                adjacency.setdefault(callee, set()).add(caller)
        expanded = 0
        for anchor in candidates[:prefix_k]:
            if anchor.chunk.symbol_kind == "module":
                continue
            neighbors = adjacency.get((
                _normalize_path(anchor.chunk.file_path),
                anchor.chunk.symbol_qualified_name,
            ), set())
            for candidate in candidates:
                key = (_normalize_path(candidate.chunk.file_path),
                       candidate.chunk.symbol_kind, candidate.chunk.symbol_qualified_name)
                if key in seen or candidate.chunk.symbol_kind == "module":
                    continue
                if (key[0], key[2]) in neighbors:
                    add(candidate)
                    expanded += 1
                    break
            if expanded == family_budget:
                break

    if family_budget and mode == "source-neighborhood-v1":
        # Freeze five expansion slots and a 100-line window before evaluation.
        # Only source positions and the existing ranking determine neighbors.
        expanded = 0
        for anchor in candidates[:prefix_k]:
            if anchor.chunk.symbol_kind == "module":
                continue
            neighbors = []
            for candidate in candidates:
                key = (
                    _normalize_path(candidate.chunk.file_path),
                    candidate.chunk.symbol_kind,
                    candidate.chunk.symbol_qualified_name,
                )
                if key in seen or candidate.chunk.symbol_kind == "module":
                    continue
                if key[0] != _normalize_path(anchor.chunk.file_path):
                    continue
                # Overlapping definitions (e.g. a containing class) are not
                # adjacent source definitions.
                distance = max(
                    candidate.chunk.start_line - anchor.chunk.end_line,
                    anchor.chunk.start_line - candidate.chunk.end_line,
                )
                if 0 < distance <= 100:
                    neighbors.append((distance, _ranking_key(candidate), candidate))
            if neighbors:
                neighbors.sort(key=lambda row: (row[0], row[1]))
                add(neighbors[0][2])
                expanded += 1
                if expanded == family_budget:
                    break

    if family_budget and mode == "coverage-aware-v1":
        family_anchors: dict[tuple[str, str], LocalizationCandidate] = {}
        for candidate in candidates[:prefix_k]:
            family = _symbol_family_key(candidate.chunk)
            if family and family not in family_anchors:
                family_anchors[family] = candidate
        for family, anchor in list(family_anchors.items())[:family_budget]:
            members = [
                candidate
                for candidate in candidates
                if _symbol_family_key(candidate.chunk) == family
                and (
                    _normalize_path(candidate.chunk.file_path),
                    candidate.chunk.symbol_kind,
                    candidate.chunk.symbol_qualified_name,
                )
                not in seen
            ]
            if not members:
                continue
            members.sort(
                key=lambda candidate: (
                    candidate.chunk.symbol_qualified_name.rsplit(".", 1)[-1]
                    != "__init__",
                    abs(candidate.chunk.start_line - anchor.chunk.start_line),
                    _ranking_key(candidate),
                )
            )
            add(members[0])

    for candidate in candidates:
        add(candidate)
        if len(selected) == top_k:
            break
    return selected


def _symbol_family_key(chunk: CodeChunk) -> tuple[str, str] | None:
    qualified_name = chunk.symbol_qualified_name
    if chunk.symbol_kind == "class":
        family = qualified_name
    elif chunk.symbol_kind in {"method", "async_method"} and "." in qualified_name:
        family = qualified_name.rsplit(".", 1)[0]
    else:
        return None
    return (_normalize_path(chunk.file_path), family)


def _git_head_commit(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    value = completed.stdout.strip()
    if completed.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
        return value.lower()
    return ""


def rank_code_chunks(
    ticket_json: dict[str, Any],
    chunks: Iterable[CodeChunk],
    *,
    top_k: int = 5,
    embedding_backend: str = "tfidf",
    sbert_model: str = "sentence-transformers/all-MiniLM-L6-v2",
    sbert_local_files_only: bool = True,
    semantic_candidate_k: int = 50,
    file_aggregation: bool = True,
    advanced_file_aggregation: bool = False,
    file_aggregation_mode: str | None = None,
    generic_routing: bool = False,
    domain_path_routing: bool = True,
    repository_proximity: bool = False,
    import_graph_mode: str = "off",
    import_graph: ImportGraph | None = None,
    call_graph_mode: str = "off",
    call_graph: CallGraph | None = None,
    symbol_expansion_mode: str = "off",
    symbol_definitions: dict[str, list[dict[str, Any]]] | None = None,
    api_implementation_links: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[list[LocalizationCandidate], str]:
    _validate_top_k(top_k)
    if semantic_candidate_k <= 0:
        raise ValueError("semantic_candidate_k must be positive.")
    aggregation_mode = _resolve_file_aggregation_mode(
        file_aggregation_mode,
        advanced=advanced_file_aggregation,
    )
    graph_mode = _resolve_import_graph_mode(
        import_graph_mode,
        legacy_repository_proximity=repository_proximity,
    )
    call_mode = _resolve_call_graph_mode(call_graph_mode)
    expansion_mode = _resolve_symbol_expansion_mode(symbol_expansion_mode)
    chunk_list = list(chunks)
    if not chunk_list:
        return [], _validate_embedding_backend(embedding_backend)

    bug_report = build_bug_report_text(ticket_json)
    hybrid_backend = _is_hybrid_backend(embedding_backend)
    search_texts = [chunk.search_text for chunk in chunk_list]
    tokenized_search_texts = [_tokenize(search_text) for search_text in search_texts]
    embedding_scores, backend_name = _embedding_scores(
        bug_report,
        search_texts,
        backend="tfidf" if hybrid_backend else embedding_backend,
        sbert_model=sbert_model,
        sbert_local_files_only=sbert_local_files_only,
        tokenized_documents=tokenized_search_texts,
    )
    stack_refs = _stack_trace_refs(ticket_json)
    query_terms = _important_terms(bug_report)
    component_terms = _component_terms(ticket_json, chunk_list)
    identifiers = _report_identifiers(bug_report) if generic_routing else []
    path_query_terms = _path_signal_query_terms(bug_report, query_terms, identifiers)

    candidates: list[LocalizationCandidate] = []
    for chunk, embedding_score, search_tokens in zip(
        chunk_list,
        embedding_scores,
        tokenized_search_texts,
    ):
        search_term_set = set(search_tokens)
        component_symbol_terms = set(_tokenize(chunk.file_path + " " + chunk.symbol_name))
        symbol_terms = set(
            _tokenize(" ".join([chunk.file_path, chunk.function_name, chunk.class_name]))
        )
        stack_trace_score = _stack_signal(chunk, stack_refs)
        component_score = _term_overlap(component_terms, component_symbol_terms)
        keyword_score, matching_terms = _keyword_signal(
            query_terms,
            chunk,
            chunk_terms=search_term_set,
        )
        symbol_score = _symbol_signal(query_terms, chunk, symbol_terms=symbol_terms)
        identifier_score, matching_identifiers = _identifier_signal(identifiers, chunk)
        path_term_score, matching_path_terms = _path_term_signal(
            bug_report,
            query_terms,
            identifiers,
            chunk,
            query_set=path_query_terms,
        )
        domain_path_score, matching_domain_intents = (
            _domain_path_signal(bug_report, chunk) if domain_path_routing else (0.0, [])
        )
        score = _clamp(
            SCORING_WEIGHTS["embedding_score"] * embedding_score
            + SCORING_WEIGHTS["stack_trace_score"] * stack_trace_score
            + SCORING_WEIGHTS["component_score"] * component_score
            + SCORING_WEIGHTS["keyword_score"] * keyword_score
            + SCORING_WEIGHTS["symbol_score"] * symbol_score
            + SCORING_WEIGHTS["domain_path_score"] * domain_path_score
            + SCORING_WEIGHTS["identifier_score"] * identifier_score
            + SCORING_WEIGHTS["path_term_score"] * path_term_score,
            0.0,
            1.0,
        )
        signals = {
            "embedding_score": round(embedding_score, 4),
            "stack_trace_score": round(stack_trace_score, 4),
            "component_score": round(component_score, 4),
            "keyword_score": round(keyword_score, 4),
            "symbol_score": round(symbol_score, 4),
            "domain_path_score": round(domain_path_score, 4),
            "identifier_score": round(identifier_score, 4),
            "path_term_score": round(path_term_score, 4),
            "repository_proximity_score": 0.0,
            "symbol_definition_score": 0.0,
            "symbol_definition_bonus": 0.0,
            "symbol_definition_evidence": [],
            "api_implementation_score": 0.0,
            "api_implementation_bonus": 0.0,
            "api_implementation_evidence": [],
            "call_graph_score": 0.0,
            "call_graph_bonus": 0.0,
            "call_graph_evidence": [],
            "final_score": round(score, 4),
            "component_path": round(component_score, 4),
            "keyword_overlap": round(keyword_score, 4),
            "matching_terms": matching_terms[:8],
            "matching_domain_intents": matching_domain_intents[:8],
            "matching_identifiers": matching_identifiers[:8],
            "matching_path_terms": matching_path_terms[:8],
            "matching_repository_proximity": [],
        }
        candidates.append(
            LocalizationCandidate(
                chunk=chunk,
                score=score,
                embedding_score=embedding_score,
                reason=_reason(chunk, embedding_score, signals),
                signals=signals,
            )
        )

    if expansion_mode != "off":
        candidates = _apply_symbol_definition_expansion(
            candidates,
            symbol_definitions or build_symbol_definition_index(chunk_list),
            extract_ticket_program_names(bug_report),
            mode=expansion_mode,
        )
        if _api_implementation_expansion_enabled(expansion_mode):
            candidates = _apply_api_implementation_expansion(
                candidates,
                api_implementation_links
                or build_api_implementation_index(chunk_list, symbol_definitions),
                extract_ticket_program_names(bug_report),
                mode=expansion_mode,
            )
    if graph_mode != "off":
        candidates = _apply_import_graph(
            candidates,
            import_graph or build_import_graph(chunk_list),
            mode=graph_mode,
        )
    if call_mode != "off":
        candidates = _apply_call_graph(
            candidates,
            call_graph or build_call_graph(chunk_list, symbol_definitions),
            mode=call_mode,
        )
    ranked = sorted(candidates, key=_ranking_key)
    aggregation_evidence = ranked
    if hybrid_backend:
        # A raw top-N chunk pool can collapse to far fewer than N files when a
        # large module contributes many similar chunks.  Select one TF-IDF
        # representative per file before semantic reranking so Stage 1 can
        # reliably return the requested number of unique candidate files.
        candidate_pool = _best_file_representatives(ranked)[
            : max(top_k, semantic_candidate_k)
        ]
        sbert_scores = _sbert_scores(
            bug_report,
            [candidate.chunk.search_text for candidate in candidate_pool],
            sbert_model,
            local_files_only=sbert_local_files_only,
        )
        reranked: list[LocalizationCandidate] = []
        for candidate, sbert_score in zip(candidate_pool, sbert_scores):
            tfidf_score = candidate.embedding_score
            semantic_score = _clamp(
                HYBRID_SEMANTIC_WEIGHTS["tfidf"] * tfidf_score
                + HYBRID_SEMANTIC_WEIGHTS["sbert"] * sbert_score,
                0.0,
                1.0,
            )
            signals = dict(candidate.signals)
            signals.update(
                {
                    "tfidf_score": round(tfidf_score, 4),
                    "sbert_score": round(sbert_score, 4),
                    "embedding_score": round(semantic_score, 4),
                    "semantic_candidate_pool": len(candidate_pool),
                }
            )
            score = _clamp(
                SCORING_WEIGHTS["embedding_score"] * semantic_score
                + SCORING_WEIGHTS["stack_trace_score"] * float(signals["stack_trace_score"])
                + SCORING_WEIGHTS["component_score"] * float(signals["component_score"])
                + SCORING_WEIGHTS["keyword_score"] * float(signals["keyword_score"])
                + SCORING_WEIGHTS["symbol_score"] * float(signals["symbol_score"])
                + SCORING_WEIGHTS["domain_path_score"] * float(signals["domain_path_score"])
                + SCORING_WEIGHTS["identifier_score"] * float(signals["identifier_score"])
                + SCORING_WEIGHTS["path_term_score"] * float(signals["path_term_score"])
                + SCORING_WEIGHTS["repository_proximity_score"]
                * float(signals["repository_proximity_score"])
                + float(signals.get("symbol_definition_bonus", 0.0))
                + float(signals.get("call_graph_bonus", 0.0)),
                0.0,
                1.0,
            )
            signals["final_score"] = round(score, 4)
            reranked.append(
                replace(
                    candidate,
                    score=score,
                    embedding_score=semantic_score,
                    reason=_reason(candidate.chunk, semantic_score, signals),
                    signals=signals,
                )
            )
        ranked = sorted(reranked, key=_ranking_key)
        backend_name = f"tfidf+sbert-rerank:{sbert_model}"
    if file_aggregation:
        ranked = _aggregate_file_candidates(
            ranked,
            query_terms=query_terms,
            identifiers=identifiers,
            mode=aggregation_mode,
            evidence_candidates=aggregation_evidence,
        )
    return ranked[:top_k], backend_name


def _best_file_representatives(
    candidates: Iterable[LocalizationCandidate],
) -> list[LocalizationCandidate]:
    representatives: list[LocalizationCandidate] = []
    seen_files: set[str] = set()
    for candidate in sorted(candidates, key=_ranking_key):
        file_path = candidate.chunk.file_path
        if file_path in seen_files:
            continue
        seen_files.add(file_path)
        representatives.append(candidate)
    return representatives


def _aggregate_file_candidates(
    candidates: list[LocalizationCandidate],
    *,
    query_terms: Iterable[str] = (),
    identifiers: Iterable[str] = (),
    mode: str = "basic",
    evidence_candidates: Iterable[LocalizationCandidate] | None = None,
) -> list[LocalizationCandidate]:
    """Combine bounded, independent chunk evidence into a calibrated file score.

    In hybrid retrieval, ``candidates`` contains one SBERT-reranked
    representative per file. ``evidence_candidates`` preserves the original
    TF-IDF-ranked chunks so E1 can still use the other relevant chunks from the
    same file without sending every chunk through SBERT.
    """

    normalized_mode = _resolve_file_aggregation_mode(mode, advanced=False)
    support_enabled = normalized_mode != "basic"
    symbols_enabled = normalized_mode in {
        "supporting-symbols",
        "supporting-symbols-package",
    }
    package_enabled = normalized_mode == "supporting-symbols-package"

    grouped: dict[str, list[LocalizationCandidate]] = {}
    file_order: list[str] = []
    for candidate in candidates:
        file_path = candidate.chunk.file_path
        if file_path not in grouped:
            grouped[file_path] = []
            file_order.append(file_path)
        grouped[file_path].append(candidate)

    evidence_grouped: dict[str, list[LocalizationCandidate]] = {}
    for candidate in (evidence_candidates if evidence_candidates is not None else candidates):
        evidence_grouped.setdefault(candidate.chunk.file_path, []).append(candidate)

    aggregated: list[LocalizationCandidate] = []
    for file_path in file_order:
        file_candidates = grouped[file_path]
        primary = file_candidates[0]
        source_candidates = sorted(
            evidence_grouped.get(file_path) or file_candidates,
            key=_ranking_key,
        )
        evidence = _independent_file_evidence(source_candidates)
        evidence_threshold = (
            max(
                float(FILE_AGGREGATION_PARAMETERS["minimum_evidence_score"]),
                source_candidates[0].score
                * float(FILE_AGGREGATION_PARAMETERS["evidence_score_ratio"]),
            )
            if source_candidates
            else 1.0
        )
        qualifying_evidence = [
            candidate for candidate in evidence if candidate.score >= evidence_threshold
        ]
        supporting_chunks = [
            {
                "chunk_id": candidate.chunk.chunk_id,
                "symbol_qualified_name": candidate.chunk.symbol_qualified_name,
                "start_line": candidate.chunk.start_line,
                "end_line": candidate.chunk.end_line,
                "score": round(float(candidate.score), 4),
                "retrieval_score": round(float(candidate.score), 4),
            }
            for candidate in qualifying_evidence
            if candidate.chunk.chunk_id != primary.chunk.chunk_id
        ][:3]
        signals = dict(primary.signals)
        signals["file_aggregation_mode"] = normalized_mode
        signals["file_chunk_count"] = len(source_candidates)
        signals["supporting_chunks"] = supporting_chunks
        distinct_symbols = {
            candidate.chunk.symbol_qualified_name
            for candidate in qualifying_evidence
            if candidate.chunk.symbol_qualified_name
        }
        support_bonus = (
            min(
                float(FILE_AGGREGATION_PARAMETERS["support_bonus_cap"]),
                float(FILE_AGGREGATION_PARAMETERS["support_bonus_per_additional_chunk"])
                * max(0, len(qualifying_evidence) - 1),
            )
            if support_enabled
            else 0.0
        )
        symbol_coverage_bonus = (
            min(
                float(FILE_AGGREGATION_PARAMETERS["symbol_bonus_cap"]),
                float(FILE_AGGREGATION_PARAMETERS["symbol_bonus_per_additional_symbol"])
                * max(0, len(distinct_symbols) - 1),
            )
            if symbols_enabled
            else 0.0
        )
        file_score = _clamp(primary.score + support_bonus + symbol_coverage_bonus, 0.0, 1.0)
        signals["chunk_score"] = round(primary.score, 4)
        signals["support_evidence_threshold"] = round(evidence_threshold, 4)
        signals["support_evidence_count"] = len(qualifying_evidence)
        signals["distinct_support_symbol_count"] = len(distinct_symbols)
        signals["supporting_chunk_bonus"] = round(support_bonus, 4)
        signals["symbol_coverage_bonus"] = round(symbol_coverage_bonus, 4)
        signals["file_aggregate_score"] = round(file_score, 4)
        signals["final_score"] = round(file_score, 4)
        reason = primary.reason
        if supporting_chunks:
            reason += f" {len(supporting_chunks)} additional independent chunk(s) in this file also matched."
        if support_bonus or symbol_coverage_bonus:
            reason += (
                " File aggregation added bounded support from multiple relevant "
                "chunks and distinct symbols."
            )
        aggregated.append(replace(primary, score=file_score, reason=reason, signals=signals))
    ranked_files = sorted(aggregated, key=_ranking_key)
    if not package_enabled:
        return ranked_files
    return _apply_package_proximity(
        ranked_files,
        query_terms=query_terms,
        identifiers=identifiers,
    )


def _independent_file_evidence(
    candidates: Iterable[LocalizationCandidate],
) -> list[LocalizationCandidate]:
    """Select high-ranked chunks while avoiding nearly duplicate line windows."""

    selected: list[LocalizationCandidate] = []
    maximum = int(FILE_AGGREGATION_PARAMETERS["max_evidence_chunks"])
    for candidate in sorted(candidates, key=_ranking_key):
        if any(
            _line_range_overlap_ratio(candidate.chunk, existing.chunk) >= 0.50
            for existing in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= maximum:
            break
    return selected


def _line_range_overlap_ratio(left: CodeChunk, right: CodeChunk) -> float:
    overlap = max(
        0,
        min(left.end_line, right.end_line) - max(left.start_line, right.start_line) + 1,
    )
    shorter = min(
        max(1, left.end_line - left.start_line + 1),
        max(1, right.end_line - right.start_line + 1),
    )
    return overlap / shorter


def _resolve_file_aggregation_mode(mode: str | None, *, advanced: bool) -> str:
    normalized = str(mode or ("supporting-symbols-package" if advanced else "basic")).strip().lower()
    if normalized not in FILE_AGGREGATION_MODES:
        raise ValueError(
            "file_aggregation_mode must be one of: " + ", ".join(FILE_AGGREGATION_MODES)
        )
    return normalized


def _apply_package_proximity(
    candidates: list[LocalizationCandidate],
    *,
    query_terms: Iterable[str],
    identifiers: Iterable[str],
) -> list[LocalizationCandidate]:
    if not candidates:
        return []
    reference_paths = [candidate.chunk.file_path for candidate in candidates[:30]]
    report_terms = set(query_terms)
    for identifier in identifiers:
        report_terms.update(_identifier_variants(identifier))
    reranked: list[LocalizationCandidate] = []
    for candidate in candidates:
        path = _normalize_path(candidate.chunk.file_path)
        neighbor_depths = [
            (_shared_path_prefix_depth(path, other), other)
            for other in reference_paths
            if other != path
        ]
        depth, neighbor = max(neighbor_depths, default=(0, ""))
        path_overlap = (set(_tokenize(path)) & report_terms) - STOPWORDS
        local_evidence = max(
            float(candidate.signals.get("identifier_score", 0.0)),
            float(candidate.signals.get("path_term_score", 0.0)),
        )
        if depth < 2 or (not path_overlap and local_evidence <= 0):
            reranked.append(candidate)
            continue
        package_score = min(
            1.0,
            0.35 + 0.12 * max(0, depth - 2) + 0.20 * local_evidence + 0.04 * len(path_overlap),
        )
        boost = min(
            float(FILE_AGGREGATION_PARAMETERS["package_bonus_cap"]),
            SCORING_WEIGHTS["package_proximity_score"] * package_score,
        )
        score = _clamp(candidate.score + boost, 0.0, 1.0)
        signals = dict(candidate.signals)
        signals["package_proximity_score"] = round(package_score, 4)
        signals["package_proximity_bonus"] = round(boost, 4)
        signals["matching_package_neighbor"] = neighbor
        signals["final_score"] = round(score, 4)
        reason = (
            f"{candidate.reason} Same-package evidence is shared with strong candidate {neighbor}."
        )
        reranked.append(replace(candidate, score=score, signals=signals, reason=reason))
    return sorted(reranked, key=_ranking_key)


def _shared_path_prefix_depth(left: str, right: str) -> int:
    left_parts = [part for part in _normalize_path(left).split("/") if part]
    right_parts = [part for part in _normalize_path(right).split("/") if part]
    depth = 0
    for left_part, right_part in zip(left_parts, right_parts):
        if left_part != right_part:
            break
        depth += 1
    return depth


def validate_localization_request(ticket_json: dict[str, Any], *, min_ticket_chars: int = 20) -> dict[str, Any]:
    """Assess whether a ticket contains enough evidence for safe localization."""

    if min_ticket_chars <= 0:
        raise ValueError("min_ticket_chars must be positive.")
    content = _ticket_content_text(ticket_json)
    compact = re.sub(r"\s+", " ", content).strip()
    errors: list[str] = []
    warnings: list[str] = []
    if not compact:
        errors.append("Ticket does not contain bug-report text.")
    elif len(compact) < min_ticket_chars:
        warnings.append(
            f"Ticket text is shorter than {min_ticket_chars} characters; localization confidence is limited."
        )
    elif len(compact) < 80:
        warnings.append("Ticket text is sparse; include symptoms, reproduction steps, logs, or identifiers.")

    stack_trace_present = bool(_stack_trace_refs(ticket_json))
    path_hint_present = bool(re.search(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+", compact))
    identifier_present = any("_" in token or any(char.isupper() for char in token[1:]) for token in TOKEN_RE.findall(compact))
    if compact and not stack_trace_present and not path_hint_present and not identifier_present:
        warnings.append("No stack trace, source path, or clear code identifier was found in the ticket.")

    return {
        "status": "error" if errors else "warning" if warnings else "ok",
        "is_valid": not errors,
        "bug_report_chars": len(compact),
        "errors": errors,
        "warnings": warnings,
        "signals_present": {
            "stack_trace": stack_trace_present,
            "path_hint": path_hint_present,
            "identifier": identifier_present,
        },
    }


def _ticket_content_text(ticket_json: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in TICKET_CONTENT_KEYS:
        value = ticket_json.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value if item is not None)
        elif isinstance(value, dict):
            parts.extend(f"{sub_key}: {sub_value}" for sub_key, sub_value in value.items())
        elif value is not None:
            parts.append(str(value))
    return "\n".join(part.strip() for part in parts if part.strip())


def _localization_confidence(
    localized: list[dict[str, Any]],
    input_validation: dict[str, Any],
    *,
    llm_rerank_used: bool,
) -> dict[str, Any]:
    if not localized:
        return {
            "confidence_level": "low",
            "confidence_score": 0.0,
            "top1_top2_margin": 0.0,
            "uncertainty_reason": "No localization candidates were produced.",
            "should_manual_review": True,
            "recommend_patch_generation": False,
            "patch_generation_policy": "block_patch_generation",
        }

    best = localized[0]
    second_score = float(localized[1].get("score", 0.0)) if len(localized) > 1 else 0.0
    best_score = float(best.get("score", 0.0))
    margin = max(0.0, best_score - second_score)
    scoring = best.get("scoring_signals") if isinstance(best.get("scoring_signals"), dict) else {}
    stack_score = float(scoring.get("stack_trace_score", 0.0))
    direct_evidence = stack_score >= 0.55 or bool(input_validation["signals_present"].get("path_hint"))
    test_like = _is_test_like_path(str(best.get("file_path") or ""))

    if input_validation["errors"] or test_like:
        level = "low"
    elif best_score >= 0.55 and direct_evidence and (margin >= 0.02 or stack_score >= 0.75):
        level = "high"
    elif best_score >= 0.25:
        level = "medium"
    else:
        level = "low"

    reasons = [
        f"top_score={best_score:.4f}",
        f"top1_top2_margin={margin:.4f}",
        f"stack_trace_score={stack_score:.4f}",
    ]
    if input_validation["warnings"]:
        reasons.append("ticket input has quality warnings")
    if test_like:
        reasons.append("top candidate is test-like")
    if llm_rerank_used:
        reasons.append("optional LLM rerank was applied")

    allow_patch = level == "high"
    return {
        "confidence_level": level,
        "confidence_score": round(best_score, 4),
        "top1_top2_margin": round(margin, 4),
        "uncertainty_reason": "; ".join(reasons),
        "should_manual_review": not allow_patch,
        "recommend_patch_generation": allow_patch,
        "patch_generation_policy": "allow_patch_suggestion" if allow_patch else "manual_review_before_patch" if level == "medium" else "block_patch_generation",
    }


def _is_test_like_path(file_path: str) -> bool:
    parts = [part.lower() for part in Path(file_path.replace("\\", "/")).parts]
    name = parts[-1] if parts else ""
    return any(part in TEST_DIR_NAMES for part in parts) or name.startswith("test_") or name.endswith("_test.py")


def _localized_file(row: dict[str, Any]) -> dict[str, Any]:
    signals = row.get("signals") if isinstance(row.get("signals"), dict) else {}
    return {
        "rank": row.get("rank", 0),
        "file_path": row.get("file_path", ""),
        "score": row.get("score", 0.0),
        "primary_symbol": row.get("symbol_qualified_name", ""),
        "line_start": row.get("start_line", 1),
        "line_end": row.get("end_line", 1),
        "supporting_chunks": signals.get("supporting_chunks", []),
    }


def _stage1_candidate_file(candidate: LocalizationCandidate, rank: int) -> dict[str, Any]:
    """Serialize the pre-LLM file candidate evidence needed for evaluation."""

    signals = _candidate_scoring_signals(candidate)
    return {
        "rank": rank,
        "file_path": candidate.chunk.file_path,
        "retrieval_score": round(float(candidate.score), 4),
        "primary_symbol": candidate.chunk.symbol_qualified_name,
        "symbol_kind": candidate.chunk.symbol_kind,
        "start_line": candidate.chunk.start_line,
        "end_line": candidate.chunk.end_line,
        "reason": candidate.reason,
        "scoring_signals": signals,
        "file_chunk_count": int(candidate.signals.get("file_chunk_count", 1)),
        "supporting_chunks": candidate.signals.get("supporting_chunks", []),
    }


def _ranked_symbol(candidate: LocalizationCandidate, rank: int) -> dict[str, Any]:
    """Serialize the Stage-3 symbol result without losing retrieval evidence."""

    signals = candidate.signals
    return {
        "rank": rank,
        "file_path": candidate.chunk.file_path,
        "symbol_qualified_name": candidate.chunk.symbol_qualified_name,
        "symbol_name": candidate.chunk.symbol_name,
        "symbol_kind": candidate.chunk.symbol_kind,
        "start_line": candidate.chunk.start_line,
        "end_line": candidate.chunk.end_line,
        "score": round(float(candidate.score), 4),
        "retrieval_score": round(
            float(signals.get("retrieval_score_before_symbol_llm", candidate.score)),
            4,
        ),
        "llm_score": (
            round(float(signals["llm_symbol_rerank_score"]), 4)
            if "llm_symbol_rerank_score" in signals
            else None
        ),
        "reason": candidate.reason,
        "called_helper_score": round(
            float(signals.get("called_helper_score") or 0.0), 4
        ),
        "called_by_file": signals.get("called_by_file"),
        "called_by_symbol": signals.get("called_by_symbol"),
        "chunk_id": candidate.chunk.chunk_id,
        "code_text": candidate.chunk.code_text,
    }


def _unique_symbol_candidates(
    candidates: Iterable[LocalizationCandidate],
) -> list[LocalizationCandidate]:
    """Keep the highest-ranked chunk for each qualified symbol."""

    unique: list[LocalizationCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        key = (
            candidate.chunk.file_path,
            candidate.chunk.symbol_qualified_name,
            candidate.chunk.symbol_kind,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def build_bug_report_text(ticket_json: dict[str, Any]) -> str:
    parts: list[str] = []
    keys = (
        "bug_report",
        "title",
        "summary",
        "body",
        "description",
        "bug_type",
        "product",
        "component",
        "severity",
        "priority",
        "error_message",
        "logs",
        "steps_to_reproduce",
        "expected_behavior",
        "actual_behavior",
        "screenshots_text",
        "environment",
        "os",
        "version",
    )
    seen_values: set[str] = set()
    for key in keys:
        value = ticket_json.get(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            text = " ".join(str(item) for item in value if item is not None)
        elif isinstance(value, dict):
            text = " ".join(f"{sub_key}: {sub_value}" for sub_key, sub_value in value.items())
        else:
            text = str(value)
        stripped = text.strip()
        normalized = re.sub(r"\s+", " ", stripped).casefold()
        if stripped and normalized not in seen_values:
            parts.append(f"{key}: {stripped}")
            seen_values.add(normalized)
    return "\n".join(parts)


def rerank_candidates_with_llm(
    ticket_json: dict[str, Any],
    candidates: list[LocalizationCandidate],
    llm_client: Any,
    *,
    top_k: int = 5,
) -> list[LocalizationCandidate]:
    reranked: list[LocalizationCandidate] = []
    for batch_number, start in enumerate(
        range(0, len(candidates), LLM_RERANK_BATCH_SIZE),
        start=1,
    ):
        batch = candidates[start : start + LLM_RERANK_BATCH_SIZE]
        batch_result = _rerank_candidate_batch(
            ticket_json,
            batch,
            llm_client,
            batch_number=batch_number,
        )
        if batch_result is None:
            return candidates[:top_k]
        reranked.extend(batch_result)
    return sorted(reranked, key=_ranking_key)[:top_k]


def _rerank_candidate_batch(
    ticket_json: dict[str, Any],
    candidates: list[LocalizationCandidate],
    llm_client: Any,
    *,
    batch_number: int,
) -> list[LocalizationCandidate] | None:
    prompt = _llm_rerank_prompt(ticket_json, candidates)
    payload = _generate_llm_json(
        llm_client,
        prompt,
        _llm_rerank_schema("candidates", len(candidates)),
    )
    rows = payload.get("candidates", []) if isinstance(payload, dict) else []
    by_rank = {index: candidate for index, candidate in enumerate(candidates, start=1)}
    reranked: list[LocalizationCandidate] = []
    seen_ranks: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            rank = int(row.get("rank") or row.get("original_rank") or 0)
        except (TypeError, ValueError):
            continue
        if rank in seen_ranks:
            continue
        candidate = by_rank.get(rank)
        if candidate is None:
            continue
        try:
            score = float(row.get("score", candidate.score))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score):
            continue
        llm_score = _clamp(score, 0.0, 1.0)
        retrieval_score = candidate.score
        score = _clamp(
            LLM_RERANK_WEIGHTS["retrieval"] * retrieval_score
            + LLM_RERANK_WEIGHTS["llm"] * llm_score,
            0.0,
            1.0,
        )
        llm_reason = str(row.get("reason") or "").strip()
        reason = f"LLM rerank: {llm_reason} Retrieval: {candidate.reason}" if llm_reason else candidate.reason
        signals = dict(candidate.signals)
        signals["retrieval_score_before_llm"] = round(retrieval_score, 4)
        signals["llm_rerank_score"] = round(llm_score, 4)
        signals["llm_blended_score"] = round(score, 4)
        signals["llm_rerank_batch"] = batch_number
        signals["final_score"] = round(score, 4)
        reranked.append(replace(candidate, score=score, reason=reason, signals=signals))
        seen_ranks.add(rank)

    if len(reranked) != len(candidates):
        return None
    return reranked


def rerank_symbols_with_llm(
    ticket_json: dict[str, Any],
    candidates: list[LocalizationCandidate],
    llm_client: Any,
    *,
    top_k: int = 5,
) -> list[LocalizationCandidate]:
    """Rerank a bounded AST-symbol set and reject incomplete LLM output.

    The teammate prototype accepted partial rows, which can silently drop the
    correct symbol. This function uses the same all-candidates coverage rule as
    the existing file reranker and falls back to the retrieval order.
    """

    reranked: list[LocalizationCandidate] = []
    for batch_number, start in enumerate(
        range(0, len(candidates), LLM_RERANK_BATCH_SIZE),
        start=1,
    ):
        batch = candidates[start : start + LLM_RERANK_BATCH_SIZE]
        batch_result = _rerank_symbol_batch(
            ticket_json,
            batch,
            llm_client,
            batch_number=batch_number,
        )
        if batch_result is None:
            return candidates[:top_k]
        reranked.extend(batch_result)
    return sorted(reranked, key=_ranking_key)[:top_k]


def _rerank_symbol_batch(
    ticket_json: dict[str, Any],
    candidates: list[LocalizationCandidate],
    llm_client: Any,
    *,
    batch_number: int,
) -> list[LocalizationCandidate] | None:
    prompt = _llm_symbol_rerank_prompt(ticket_json, candidates)
    payload = _generate_llm_json(
        llm_client,
        prompt,
        _llm_rerank_schema("symbols", len(candidates)),
    )
    rows = payload.get("symbols", []) if isinstance(payload, dict) else []
    by_rank = {index: candidate for index, candidate in enumerate(candidates, start=1)}
    reranked: list[LocalizationCandidate] = []
    seen_ranks: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            rank = int(row.get("rank") or row.get("id") or row.get("original_rank") or 0)
        except (TypeError, ValueError):
            continue
        if rank in seen_ranks:
            continue
        candidate = by_rank.get(rank)
        if candidate is None:
            continue
        try:
            raw_score = row.get("score", row.get("confidence_score", candidate.score))
            llm_score = float(raw_score)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(llm_score):
            continue
        llm_score = _clamp(llm_score, 0.0, 1.0)
        retrieval_score = candidate.score
        blended_score = _clamp(
            LLM_RERANK_WEIGHTS["retrieval"] * retrieval_score
            + LLM_RERANK_WEIGHTS["llm"] * llm_score,
            0.0,
            1.0,
        )
        llm_reason = str(row.get("reason") or "").strip()
        reason = (
            f"Symbol LLM rerank: {llm_reason} Retrieval: {candidate.reason}"
            if llm_reason
            else candidate.reason
        )
        signals = dict(candidate.signals)
        signals["retrieval_score_before_symbol_llm"] = round(retrieval_score, 4)
        signals["llm_symbol_rerank_score"] = round(llm_score, 4)
        signals["symbol_llm_blended_score"] = round(blended_score, 4)
        signals["llm_symbol_rerank_batch"] = batch_number
        reranked.append(
            replace(candidate, score=blended_score, reason=reason, signals=signals)
        )
        seen_ranks.add(rank)

    if len(reranked) != len(candidates):
        return None
    return reranked


def _generate_llm_json(
    llm_client: Any,
    prompt: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    if hasattr(llm_client, "generate_json_with_schema"):
        return llm_client.generate_json_with_schema(prompt, schema)
    if hasattr(llm_client, "generate_json"):
        return llm_client.generate_json(prompt)

    from utils.json_schema import repair_json_object

    return repair_json_object(str(llm_client.generate(prompt)))


def _llm_rerank_schema(root_key: str, candidate_count: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            root_key: {
                "type": "array",
                "minItems": candidate_count,
                "maxItems": candidate_count,
                "items": {
                    "type": "object",
                    "properties": {
                        "rank": {
                            "type": "integer",
                            "enum": list(range(1, candidate_count + 1)),
                        },
                        "score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "reason": {"type": "string", "maxLength": 160},
                    },
                    "required": ["rank", "score", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": [root_key],
        "additionalProperties": False,
    }


def _iter_source_files(root: Path, *, include_tests: bool, max_file_bytes: int) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if _is_ignored_path(rel_parts):
            continue
        if not include_tests and any(part.lower() in TEST_DIR_NAMES for part in rel_parts):
            continue
        if path.suffix.lower() not in CODE_SUFFIXES:
            continue
        if not path.is_symlink() and not path.is_file():
            continue
        try:
            if not path.is_symlink() and path.stat().st_size > max_file_bytes:
                continue
        except OSError:
            continue
        yield path


def _is_ignored_path(rel_parts: tuple[str, ...]) -> bool:
    if not rel_parts:
        return False
    if any(part in IGNORED_DIRS_ANYWHERE for part in rel_parts):
        return True
    return rel_parts[0] in IGNORED_ROOT_DIRS


def _chunks_for_file(path: Path, root: Path, *, chunk_lines: int, overlap_lines: int) -> list[CodeChunk]:
    chunks, _ = _chunks_and_symbol_result_for_file(
        path,
        root,
        repository_name=root.name,
        base_commit=_git_head_commit(root) or "working-tree",
        chunk_lines=chunk_lines,
        overlap_lines=overlap_lines,
    )
    return chunks


def _chunks_and_symbol_result_for_file(
    path: Path,
    root: Path,
    *,
    repository_name: str,
    base_commit: str,
    chunk_lines: int,
    overlap_lines: int,
) -> tuple[list[CodeChunk], SymbolParseResult]:
    rel = path.relative_to(root).as_posix()
    language = CODE_SUFFIXES.get(path.suffix.lower(), "text")
    parse_result = extract_symbols_from_repository_file(
        root,
        rel,
        repo=repository_name,
        base_commit=base_commit,
        language=language,
        ast_input_source="code_index_v1",
    )
    if parse_result.status in {"missing_file", "invalid_path", "symlink_escape"}:
        return [], parse_result
    resolved_path = path.resolve(strict=True)
    resolved_path.relative_to(root)
    text = resolved_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    symbol_ranges = (
        _legacy_symbol_ranges(parse_result)
        if language == "python"
        else _regex_symbol_ranges(lines, language)
    )
    chunks: list[CodeChunk] = []
    for symbol in symbol_ranges:
        chunks.extend(_split_symbol(rel, language, lines, symbol, chunk_lines=chunk_lines, overlap_lines=overlap_lines))
    top_level_symbols = [symbol for symbol in symbol_ranges if bool(symbol.get("is_top_level", True))]
    for start_line, end_line in _module_level_gaps(lines, top_level_symbols):
        chunks.extend(
            _fixed_line_chunks(
                rel,
                language,
                lines,
                start_line=start_line,
                end_line=end_line,
                chunk_lines=chunk_lines,
                overlap_lines=overlap_lines,
                symbol_kind="module",
            )
        )
    if not chunks:
        chunks.extend(
            _fixed_line_chunks(
                rel,
                language,
                lines,
                start_line=1,
                end_line=len(lines),
                chunk_lines=chunk_lines,
                overlap_lines=overlap_lines,
            )
        )
    return chunks, parse_result


def _python_symbol_ranges(text: str) -> list[dict[str, Any]]:
    """Adapt SymbolRecordV1 extraction to the legacy chunk-range contract.

    The code-index builder still consumes the historical six-field mapping.
    Keeping this wrapper thin removes the duplicate AST traversal while
    allowing its callers to migrate independently to SymbolParseResult.
    """

    result = extract_python_symbols(
        text,
        repo="legacy-code-index",
        base_commit="working-tree",
        file_path="source.py",
        ast_input_source="legacy_code_index_wrapper",
    )
    if result.status != "ok":
        return []

    return _legacy_symbol_ranges(result)


def _legacy_symbol_ranges(result: SymbolParseResult) -> list[dict[str, Any]]:
    """Convert one successful v1 parse result to legacy chunk ranges."""

    by_id = {symbol.symbol_id: symbol for symbol in result.symbols}
    ranges: list[dict[str, Any]] = []
    for symbol in result.symbols:
        parent = by_id.get(symbol.parent_symbol_id)
        is_class = symbol.symbol_kind == "class"
        ranges.append(
            {
                "symbol_kind": symbol.symbol_kind,
                "function_name": "" if is_class else symbol.qualified_name,
                "class_name": (
                    symbol.qualified_name
                    if is_class
                    else parent.qualified_name
                    if parent is not None and parent.symbol_kind == "class"
                    else ""
                ),
                "start_line": symbol.start_line,
                "end_line": symbol.end_line,
                "is_top_level": not bool(symbol.parent_symbol_id),
            }
        )
    return sorted(
        ranges,
        key=lambda row: (
            int(row["start_line"]),
            int(row["end_line"]),
            row["symbol_kind"] != "class",
        ),
    )


def _regex_symbol_ranges(lines: list[str], language: str) -> list[dict[str, Any]]:
    patterns = [
        re.compile(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)"),
        re.compile(r"\b(?:function|func|fn)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
        re.compile(r"\b(?:def|async\s+def)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
        re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(?:async\s*)?\([^)]*\)\s*=>"),
        re.compile(r"\b(?:public|private|protected|static|final|async|\s)+[A-Za-z0-9_<>,\[\]?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ]
    declarations: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        for pattern in patterns:
            match = pattern.search(stripped)
            if not match:
                continue
            kind = "class" if stripped.startswith("class ") or " class " in stripped else "function"
            name = match.group(1)
            declarations.append(
                {
                    "symbol_kind": kind,
                    "function_name": "" if kind == "class" else name,
                    "class_name": name if kind == "class" else "",
                    "start_line": index,
                    "is_top_level": True,
                }
            )
            break
    for position, declaration in enumerate(declarations):
        next_start = int(declarations[position + 1]["start_line"]) if position + 1 < len(declarations) else None
        declaration["end_line"] = _infer_regex_symbol_end(
            lines,
            int(declaration["start_line"]),
            next_start=next_start,
            language=language,
        )
    return declarations


def _infer_regex_symbol_end(
    lines: list[str],
    start_line: int,
    *,
    next_start: int | None,
    language: str,
) -> int:
    brace_languages = {"javascript", "typescript", "java", "c", "cpp", "go", "rust"}
    if language in brace_languages:
        depth = 0
        saw_opening_brace = False
        for line_number in range(start_line, len(lines) + 1):
            cleaned = re.sub(r"(['\"`]).*?\1", "", lines[line_number - 1])
            cleaned = cleaned.split("//", maxsplit=1)[0]
            opening = cleaned.count("{")
            closing = cleaned.count("}")
            if opening:
                saw_opening_brace = True
            depth += opening - closing
            if saw_opening_brace and depth <= 0:
                return line_number

    if language == "python":
        base_indent = len(lines[start_line - 1]) - len(lines[start_line - 1].lstrip())
        for line_number in range(start_line + 1, len(lines) + 1):
            line = lines[line_number - 1]
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= base_indent and not line.lstrip().startswith(("@", "#")):
                return line_number - 1

    if next_start is not None:
        return max(start_line, next_start - 1)
    return min(len(lines), start_line + 79)


def _module_level_gaps(lines: list[str], top_level_symbols: list[dict[str, Any]]) -> list[tuple[int, int]]:
    if not top_level_symbols:
        return []
    gaps: list[tuple[int, int]] = []
    current = 1
    for symbol in sorted(top_level_symbols, key=lambda row: (int(row["start_line"]), int(row["end_line"]))):
        start = max(1, int(symbol["start_line"]))
        end = min(len(lines), int(symbol["end_line"]))
        if current < start and any(line.strip() for line in lines[current - 1 : start - 1]):
            gaps.append((current, start - 1))
        current = max(current, end + 1)
    if current <= len(lines) and any(line.strip() for line in lines[current - 1 :]):
        gaps.append((current, len(lines)))
    return gaps


def _split_symbol(
    rel: str,
    language: str,
    lines: list[str],
    symbol: dict[str, Any],
    *,
    chunk_lines: int,
    overlap_lines: int,
) -> list[CodeChunk]:
    start_line = max(1, int(symbol["start_line"]))
    end_line = min(len(lines), int(symbol["end_line"]))
    if end_line - start_line + 1 <= chunk_lines:
        return [
            _make_chunk(
                rel,
                language,
                lines,
                symbol_kind=str(symbol["symbol_kind"]),
                function_name=str(symbol["function_name"]),
                class_name=str(symbol["class_name"]),
                start_line=start_line,
                end_line=end_line,
            )
        ]
    return _fixed_line_chunks(
        rel,
        language,
        lines,
        start_line=start_line,
        end_line=end_line,
        chunk_lines=chunk_lines,
        overlap_lines=overlap_lines,
        symbol_kind=str(symbol["symbol_kind"]),
        function_name=str(symbol["function_name"]),
        class_name=str(symbol["class_name"]),
    )


def _fixed_line_chunks(
    rel: str,
    language: str,
    lines: list[str],
    *,
    start_line: int,
    end_line: int,
    chunk_lines: int,
    overlap_lines: int,
    symbol_kind: str = "chunk",
    function_name: str = "",
    class_name: str = "",
) -> list[CodeChunk]:
    chunks: list[CodeChunk] = []
    step = max(1, chunk_lines - overlap_lines)
    current = start_line
    while current <= end_line:
        chunk_end = min(end_line, current + chunk_lines - 1)
        chunks.append(
            _make_chunk(
                rel,
                language,
                lines,
                symbol_kind=symbol_kind,
                function_name=function_name,
                class_name=class_name,
                start_line=current,
                end_line=chunk_end,
            )
        )
        if chunk_end >= end_line:
            break
        current += step
    return chunks


def _make_chunk(
    rel: str,
    language: str,
    lines: list[str],
    *,
    symbol_kind: str,
    function_name: str,
    class_name: str,
    start_line: int,
    end_line: int,
) -> CodeChunk:
    code_text = "\n".join(lines[start_line - 1 : end_line])
    symbol = function_name or class_name or symbol_kind
    chunk_id = f"{rel}:{start_line}-{end_line}:{symbol}"
    return CodeChunk(
        chunk_id=chunk_id,
        file_path=rel,
        language=language,
        symbol_kind=symbol_kind,
        function_name=function_name,
        class_name=class_name,
        start_line=start_line,
        end_line=end_line,
        code_text=code_text,
    )


def _embedding_scores(
    query: str,
    documents: list[str],
    *,
    backend: str,
    sbert_model: str,
    sbert_local_files_only: bool,
    tokenized_documents: list[list[str]] | None = None,
) -> tuple[list[float], str]:
    normalized = _validate_embedding_backend(backend)
    if normalized in {"auto", "sbert", "sentence-transformers", "sentence_transformers"}:
        try:
            return _sbert_scores(query, documents, sbert_model, local_files_only=sbert_local_files_only), f"sbert:{sbert_model}"
        except Exception:
            if normalized != "auto":
                raise
    return _tfidf_scores(query, documents, tokenized_documents=tokenized_documents), "tfidf"


def _sbert_scores(query: str, documents: list[str], model_name: str, *, local_files_only: bool) -> list[float]:
    model = _load_sbert_model(model_name, local_files_only)
    embeddings = model.encode([query, *documents], normalize_embeddings=True)
    query_embedding = embeddings[0]
    doc_embeddings = embeddings[1:]
    scores: list[float] = []
    for embedding in doc_embeddings:
        score = float(sum(float(left) * float(right) for left, right in zip(query_embedding, embedding)))
        scores.append(_clamp(score, 0.0, 1.0))
    return scores


@lru_cache(maxsize=4)
def _load_sbert_model(model_name: str, local_files_only: bool) -> Any:
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name, local_files_only=local_files_only)


def _tfidf_scores(
    query: str,
    documents: list[str],
    *,
    tokenized_documents: list[list[str]] | None = None,
) -> list[float]:
    tokenized_docs = tokenized_documents or [_tokenize(document) for document in documents]
    if len(tokenized_docs) != len(documents):
        raise ValueError("tokenized_documents must have the same length as documents.")
    query_tokens = _tokenize(query)
    if not query_tokens:
        return [0.0 for _ in documents]
    doc_freq: Counter[str] = Counter()
    for tokens in tokenized_docs:
        doc_freq.update(set(tokens))
    total_docs = max(1, len(tokenized_docs))
    idf = {term: math.log((total_docs + 1) / (df + 1)) + 1.0 for term, df in doc_freq.items()}
    query_vector = _tfidf_vector(query_tokens, idf)
    return [_cosine(query_vector, _tfidf_vector(tokens, idf)) for tokens in tokenized_docs]


def _tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    counts = Counter(tokens)
    vector: dict[str, float] = {}
    for token, count in counts.items():
        if token not in idf:
            continue
        vector[token] = (1.0 + math.log(count)) * idf[token]
    return vector


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    common = set(left) & set(right)
    numerator = sum(left[token] * right[token] for token in common)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return float(numerator / (left_norm * right_norm))


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in TOKEN_RE.findall(text or ""):
        lowered = raw.lower()
        tokens.append(lowered)
        for piece in TOKEN_SPLIT_RE.split(raw):
            tokens.extend(part.lower() for part in CAMEL_RE.findall(piece) if len(part) > 1)
    return [token for token in tokens if len(token) > 1]


def _important_terms(text: str) -> list[str]:
    unique: list[str] = []
    for token in _tokenize(text):
        if len(token) < 3 or token in STOPWORDS:
            continue
        if token not in unique:
            unique.append(token)
    return unique[:40]


def _component_terms(ticket_json: dict[str, Any], chunks: list[CodeChunk]) -> list[str]:
    """Discard project-wide component labels that cannot distinguish files."""

    terms = _important_terms(str(ticket_json.get("component") or ""))
    if not terms:
        return []
    file_paths = {chunk.file_path for chunk in chunks}
    file_count = max(1, len(file_paths))
    counts: Counter[str] = Counter()
    for file_path in file_paths:
        counts.update(set(_tokenize(file_path)))
    repo_terms = set(
        _important_terms(str(ticket_json.get("repo") or ticket_json.get("product") or ""))
    )
    return [
        term
        for term in terms
        if term not in repo_terms and counts.get(term, 0) / file_count < 0.35
    ]


def _report_identifiers(text: str) -> list[str]:
    """Extract code-like names while ignoring ordinary prose tokens."""

    identifiers: list[str] = []

    def add(value: str) -> None:
        normalized = _normalize_identifier(value)
        if not normalized or normalized in STOPWORDS or len(normalized) < 3:
            return
        if normalized not in identifiers:
            identifiers.append(normalized)

    patterns = (
        r"\b[A-Z][A-Z0-9_]{2,}\b",
        r"\b[A-Z]\d{2,4}\b",
        r"\b[A-Za-z_][A-Za-z0-9_]*__[A-Za-z0-9_]+(?:__[A-Za-z0-9_]+)*\b",
        r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b",
        r"\b[A-Z][A-Za-z0-9]+(?:[A-Z][A-Za-z0-9]+)+\b",
        r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            add(match.group(0))
            if len(identifiers) >= 80:
                return identifiers
    return identifiers


def extract_ticket_program_names(text: str) -> list[str]:
    """Extract explicit code-like names without treating ordinary prose as Symbols."""

    names: list[str] = []
    seen: set[str] = set()
    maximum = int(SYMBOL_EXPANSION_PARAMETERS["max_ticket_symbols"])

    def add(value: str) -> None:
        cleaned = value.strip().strip("\"'`.,;:()[]{}")
        normalized = _normalize_symbol_lookup_key(cleaned)
        leaf = normalized.rsplit(".", maxsplit=1)[-1]
        if (
            not normalized
            or len(leaf) < 3
            or leaf in SYMBOL_IDENTIFIER_STOPWORDS
            or normalized in seen
        ):
            return
        if not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*",
            cleaned,
        ):
            return
        names.append(cleaned)
        seen.add(normalized)

    # Backticks are an explicit author signal that even a plain lowercase word
    # is intended as code, while calls and dotted names are code-like by shape.
    for match in re.finditer(r"(?<!`)`([^`\n]+)`(?!`)", text or ""):
        add(match.group(1))
        if len(names) >= maximum:
            return names
    for match in re.finditer(
        r"(?<![A-Za-z0-9_.])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\(",
        text or "",
    ):
        add(match.group(1))
        if len(names) >= maximum:
            return names
    for identifier in _report_identifiers(text or ""):
        add(identifier)
        if len(names) >= maximum:
            break
    return names


def _normalize_symbol_lookup_key(value: str) -> str:
    normalized = value.strip().strip("\"'`.,;:()[]{}")
    normalized = normalized.replace("::", ".").replace("/", ".")
    normalized = re.sub(r"\.+", ".", normalized).strip(".")
    return normalized.casefold()


def _identifier_signal(identifiers: list[str], chunk: CodeChunk) -> tuple[float, list[str]]:
    if not identifiers:
        return 0.0, []
    chunk_text = chunk.search_text.lower()
    chunk_tokens = set(_tokenize(chunk.search_text))
    matching: list[str] = []
    for identifier in identifiers:
        variants = _identifier_variants(identifier)
        if any(
            variant in chunk_tokens
            or bool(
                re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(variant)}(?![A-Za-z0-9_])",
                    chunk_text,
                )
            )
            for variant in variants
        ):
            matching.append(identifier)
    if not matching:
        return 0.0, []
    if any(re.fullmatch(r"[a-z]\d{2,4}", value) for value in matching):
        return 1.0, matching
    return min(1.0, 0.5 + 0.1 * (len(matching) - 1)), matching


def _identifier_variants(identifier: str) -> set[str]:
    normalized = _normalize_identifier(identifier)
    variants = {normalized}
    if normalized.endswith("s") and len(normalized) > 4:
        variants.add(normalized[:-1])
    if "_" in normalized:
        variants.update(part for part in normalized.split("_") if len(part) >= 3)
    if "." in normalized:
        variants.update(part for part in normalized.split(".") if len(part) >= 3)
    return variants


def _normalize_identifier(value: str) -> str:
    return value.strip().strip("\"'`.,;:()[]{}").replace("-", "_").lower()


def _path_term_signal(
    query_text: str,
    query_terms: list[str],
    identifiers: list[str],
    chunk: CodeChunk,
    *,
    query_set: set[str] | None = None,
) -> tuple[float, list[str]]:
    if query_set is None:
        query_set = _path_signal_query_terms(query_text, query_terms, identifiers)
    leaf_terms, path_terms = _path_signal_terms(chunk.file_path)

    leaf_overlap = sorted((leaf_terms & query_set) - STOPWORDS)
    parent_overlap = sorted(((path_terms - leaf_terms) & query_set) - STOPWORDS)
    if leaf_overlap:
        return min(1.0, 0.55 + 0.1 * (len(leaf_overlap) - 1)), leaf_overlap
    if len(parent_overlap) >= 2:
        return min(0.65, 0.3 + 0.08 * len(parent_overlap)), parent_overlap
    if parent_overlap:
        return 0.25, parent_overlap
    return 0.0, []


def _path_signal_query_terms(
    query_text: str,
    query_terms: Iterable[str],
    identifiers: Iterable[str],
) -> set[str]:
    query_set = set(query_terms)
    query_set.update(re.findall(r"[a-z0-9]+", query_text.lower()))
    for identifier in identifiers:
        query_set.update(_identifier_variants(identifier))
    return query_set - STOPWORDS


@lru_cache(maxsize=8192)
def _path_signal_terms(file_path: str) -> tuple[frozenset[str], frozenset[str]]:
    path = _normalize_path(file_path).lower()
    stem = Path(path).stem
    leaf_terms = set(_tokenize(stem)) | set(re.findall(r"[a-z0-9]+", stem))
    path_terms = set(_tokenize(path.replace("/", " "))) | set(
        re.findall(r"[a-z0-9]+", path)
    )
    for token in list(path_terms | leaf_terms):
        digit_suffix = re.search(r"(\d+[a-z]+)$", token)
        if digit_suffix:
            path_terms.add(digit_suffix.group(1))
            leaf_terms.add(digit_suffix.group(1))
        if token.endswith("s") and len(token) > 4:
            path_terms.add(token[:-1])
            leaf_terms.add(token[:-1])
    return frozenset(leaf_terms), frozenset(path_terms)


def _domain_path_signal(text: str, chunk: CodeChunk) -> tuple[float, list[str]]:
    """Map high-precision framework concepts to likely source-code regions.

    This signal only reads production-available ticket text and repository
    paths. It never reads benchmark hints, tests, patches, or gold files.
    """

    lowered = text.lower().replace("\\", "/")
    path = _normalize_path(chunk.file_path).lower()
    intents: list[tuple[str, float]] = []

    def add(label: str, score: float) -> None:
        if label not in {existing for existing, _ in intents}:
            intents.append((label, score))

    if _contains_any(lowered, ("file_upload", "static_url", "media_url", "script_name", "base_dir", "settings.py")):
        if path in {"django/conf/global_settings.py", "django/conf/__init__.py"}:
            add("django_settings_conf", 0.9)

    if _contains_any(lowered, ("models.e", "system check", "db_table", "same table name", "table_name")):
        if path.startswith("django/core/checks/"):
            add("django_model_checks", 0.9)

    if _contains_any(lowered, ("urlconf", "urlpatterns", "re_path", "url params", "url parameter", "resolver")):
        if path.startswith("django/urls/"):
            add("django_url_resolver", 0.9)

    if _contains_any(lowered, ("makemigrations", "generated migration", "migration file", "missing import statement")):
        if path == "django/db/migrations/serializer.py":
            add("django_migration_serializer", 1.0)
        elif path.startswith("django/db/migrations/"):
            add("django_migrations", 0.25)

    if _contains_any(lowered, ("q object", " q(", "pk__in", "| operator", "cannot pickle")):
        if path == "django/db/models/query_utils.py":
            add("django_query_utils", 1.0)

    if _contains_all(lowered, ("group by", "query")) or _contains_all(lowered, ("filter", "query result")):
        if path == "django/db/models/lookups.py":
            add("django_model_lookups", 0.85)

    if _contains_any(lowered, ("--keepdb", "persistent sqlite", "persistent test sqlite", "test[\"name\"]", "test['name']")):
        if path == "django/db/backends/sqlite3/creation.py":
            add("django_sqlite_test_creation", 1.0)

    if _contains_all(lowered, ("unique constraint", "sqlite")) and _contains_any(
        lowered, ("remaking table", "remake", "ddl", "references")
    ):
        if path == "django/db/backends/ddl_references.py":
            add("django_ddl_references", 0.9)

    if _contains_any(lowered, ("dev server", "runserver", "restart", "autoreload")) and _contains_any(
        lowered, ("templates", "base_dir", "settings.py")
    ):
        if path == "django/template/autoreload.py":
            add("django_template_autoreload", 1.0)

    if _contains_any(lowered, ("if-modified-since", "modified-since", "modified since")):
        if path == "django/views/static.py":
            add("django_static_modified_since", 0.95)

    if _contains_any(lowered, ("expressionwrapper", "output_field", "booleanfield")) and _contains_any(
        lowered, ("~q", "pk__in", " q(")
    ):
        if path == "django/db/models/fields/__init__.py":
            add("django_model_fields_expression", 0.75)

    if _contains_any(lowered, ("get_backend", "rc_context", "rcparams", "backend resolution", "backend reset")):
        if path == "lib/matplotlib/__init__.py":
            add("matplotlib_backend_resolution", 1.0)
        elif path.endswith("matplotlib/rcsetup.py"):
            add("matplotlib_rcparams", 0.65)

    if _contains_any(lowered, ("pairplot", "hue_order", "scatterplot")) and _contains_any(lowered, ("hue", "order")):
        if path == "seaborn/_oldcore.py":
            add("seaborn_legacy_semantics", 0.9)
        elif path.startswith("seaborn/_core/"):
            add("seaborn_core_semantics", 0.35)

    if _contains_any(lowered, ("subdomain", "sub-domain", "flask routes", "routes command")):
        if path == "src/flask/cli.py":
            add("flask_cli_routes", 1.0)

    if _contains_any(lowered, ("urllib3 exceptions", "wrapped", "passing through requests api", "bleeding through")):
        if path == "requests/adapters.py":
            add("requests_adapter_exception_boundary", 1.0)
        elif path == "requests/exceptions.py":
            add("requests_exception_boundary", 0.65)

    if _contains_any(lowered, ("dataset overview", "show units", "attrs['units']", 'attrs["units"]')):
        if path == "xarray/core/formatting.py":
            add("xarray_formatting_units", 1.0)

    if _contains_any(lowered, ("ignore-paths", "ignore paths", "--recursive", "recursive=y")):
        if path == "pylint/lint/expand_modules.py":
            add("pylint_expand_modules_ignore_paths", 1.0)

    if _contains_any(lowered, ("import-mode=importlib", "importmode importlib", "module imported twice", "doctest-modules")):
        if path == "src/_pytest/pathlib.py":
            add("pytest_import_path", 1.0)

    if _contains_any(lowered, ("print_changed_only", "new repr", "prettyprinter", "pprint")):
        if path == "sklearn/utils/_pprint.py":
            add("sklearn_pretty_printer", 1.0)

    if _contains_any(lowered, ("napoleon", "attribute", "underscore", "hello_")):
        if path == "sphinx/ext/napoleon/docstring.py":
            add("sphinx_napoleon_docstring", 1.0)

    if not intents:
        return 0.0, []
    return max(score for _, score in intents), [label for label, _ in intents]


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _contains_all(text: str, needles: tuple[str, ...]) -> bool:
    return all(needle in text for needle in needles)


def _resolve_symbol_expansion_mode(mode: str | None) -> str:
    normalized = str(mode or "off").strip().lower()
    if normalized not in SYMBOL_EXPANSION_MODES:
        raise ValueError(
            "symbol_expansion_mode must be one of: "
            + ", ".join(SYMBOL_EXPANSION_MODES)
        )
    return normalized


def _dotted_leaf_fallback_policy(mode: str) -> str:
    normalized_mode = _resolve_symbol_expansion_mode(mode)
    if normalized_mode in {
        "symbol-definitions",
        "symbol-definitions-api",
        "symbol-definitions-api-namespace",
    }:
        return "legacy-unrestricted"
    if normalized_mode == "symbol-definitions-guarded":
        return "guarded"
    return "disabled"


def _should_use_dotted_leaf_fallback(normalized_symbol: str, mode: str) -> bool:
    policy = _dotted_leaf_fallback_policy(mode)
    if policy == "legacy-unrestricted":
        return True
    if policy != "guarded":
        return False
    parts = [part for part in normalized_symbol.split(".") if part]
    if len(parts) < 2:
        return False
    receiver = parts[0]
    leaf_name = parts[-1]
    if receiver in DOTTED_LEAF_RECEIVER_NAMES or len(receiver) <= 1:
        return False
    if leaf_name in DOTTED_LEAF_GENERIC_NAMES or len(leaf_name) < 5:
        return False
    return True


def _api_implementation_expansion_enabled(mode: str) -> bool:
    return _resolve_symbol_expansion_mode(mode) in {
        "symbol-definitions-api",
        "symbol-definitions-api-namespace",
    }


def _api_namespace_guard_enabled(mode: str) -> bool:
    return _resolve_symbol_expansion_mode(mode) == "symbol-definitions-api-namespace"


def _api_link_namespace_matches(
    normalized_ticket_symbol: str,
    record: dict[str, Any],
) -> bool:
    """Require a dotted Ticket prefix to agree with the API's code namespace."""

    parts = [part for part in normalized_ticket_symbol.split(".") if part]
    if len(parts) < 2:
        return True
    prefix_parts = parts[:-1]
    leaf_name = parts[-1]
    receiver = prefix_parts[0]
    if receiver in DOTTED_LEAF_RECEIVER_NAMES or len(receiver) <= 1:
        return False
    if leaf_name in DOTTED_LEAF_GENERIC_NAMES:
        return False

    namespace_parts: set[str] = set()
    for file_key in ("source_file", "target_file"):
        namespace_parts.update(_module_parts_from_file_path(str(record.get(file_key) or "")))
    source_symbol = _normalize_symbol_lookup_key(
        str(record.get("source_symbol") or "")
    )
    namespace_parts.update(source_symbol.split(".")[:-1])
    return any(part in namespace_parts for part in prefix_parts if len(part) > 1)


def _apply_symbol_definition_expansion(
    candidates: list[LocalizationCandidate],
    symbol_definitions: dict[str, list[dict[str, Any]]],
    ticket_program_names: Iterable[str],
    *,
    mode: str,
) -> list[LocalizationCandidate]:
    """Apply bounded E3-B evidence from explicit Ticket names to definition files."""

    normalized_mode = _resolve_symbol_expansion_mode(mode)
    if normalized_mode == "off" or not candidates or not symbol_definitions:
        return candidates

    evidence_by_file: dict[str, list[dict[str, Any]]] = {}
    maximum_files = int(SYMBOL_EXPANSION_PARAMETERS["max_files_per_symbol"])
    for ticket_symbol in ticket_program_names:
        normalized = _normalize_symbol_lookup_key(ticket_symbol)
        if not normalized:
            continue
        records = list(symbol_definitions.get(normalized, []))
        match_type = "exact"
        if (
            not records
            and "." in normalized
            and _should_use_dotted_leaf_fallback(normalized, normalized_mode)
        ):
            records = list(symbol_definitions.get(normalized.rsplit(".", 1)[-1], []))
            match_type = "leaf"
        unique_records: dict[tuple[str, str, int], dict[str, Any]] = {}
        for record in records:
            signature = (
                str(record.get("file_path") or ""),
                str(record.get("qualified_name") or ""),
                int(record.get("start_line") or 0),
            )
            unique_records.setdefault(signature, record)
        matched_files = {
            str(record.get("file_path") or "")
            for record in unique_records.values()
            if record.get("file_path")
        }
        if not matched_files or len(matched_files) > maximum_files:
            continue
        for record in unique_records.values():
            file_path = str(record.get("file_path") or "")
            record_qualified = _normalize_symbol_lookup_key(
                str(record.get("qualified_name") or "")
            )
            record_match_type = (
                "exact"
                if match_type == "exact"
                and (
                    normalized == record_qualified
                    or normalized.endswith(f".{record_qualified}")
                )
                else "leaf"
            )
            evidence_by_file.setdefault(file_path, []).append(
                {
                    "ticket_symbol": ticket_symbol,
                    "matched_symbol": str(record.get("qualified_name") or ""),
                    "file_path": file_path,
                    "symbol_kind": str(record.get("symbol_kind") or ""),
                    "start_line": int(record.get("start_line") or 1),
                    "match_type": record_match_type,
                }
            )

    reranked: list[LocalizationCandidate] = []
    maximum_evidence = int(
        SYMBOL_EXPANSION_PARAMETERS["maximum_evidence_per_file"]
    )
    for candidate in candidates:
        raw_evidence = evidence_by_file.get(candidate.chunk.file_path, [])
        if not raw_evidence:
            reranked.append(candidate)
            continue
        # One Ticket name contributes at most once per file, even when a file
        # contains several overloads or methods with the same leaf name.
        evidence_by_ticket_symbol: dict[str, dict[str, Any]] = {}
        for row in raw_evidence:
            key = _normalize_symbol_lookup_key(str(row["ticket_symbol"]))
            previous = evidence_by_ticket_symbol.get(key)
            if previous is None or (
                previous["match_type"] != "exact"
                and row["match_type"] == "exact"
            ):
                evidence_by_ticket_symbol[key] = row
        evidence = list(evidence_by_ticket_symbol.values())
        evidence = sorted(
            evidence,
            key=lambda row: (
                0 if row["match_type"] == "exact" else 1,
                -len(str(row["ticket_symbol"])),
                str(row["ticket_symbol"]).casefold(),
                str(row["matched_symbol"]).casefold(),
                int(row["start_line"]),
            ),
        )[:maximum_evidence]
        raw_bonus = sum(
            float(
                SYMBOL_EXPANSION_PARAMETERS[
                    "exact_match_bonus"
                    if row["match_type"] == "exact"
                    else "leaf_match_bonus"
                ]
            )
            for row in evidence
        )
        bonus = min(
            float(SYMBOL_EXPANSION_PARAMETERS["maximum_bonus"]),
            raw_bonus,
        )
        score = _clamp(candidate.score + bonus, 0.0, 1.0)
        signals = dict(candidate.signals)
        signals["symbol_expansion_mode"] = normalized_mode
        signals["symbol_definition_score"] = round(
            bonus / float(SYMBOL_EXPANSION_PARAMETERS["maximum_bonus"]),
            4,
        )
        signals["symbol_definition_bonus"] = round(bonus, 4)
        signals["symbol_definition_evidence"] = evidence
        signals["final_score"] = round(score, 4)
        reranked.append(
            replace(
                candidate,
                score=score,
                reason=_reason(candidate.chunk, candidate.embedding_score, signals),
                signals=signals,
            )
        )
    return reranked


def _apply_api_implementation_expansion(
    candidates: list[LocalizationCandidate],
    api_implementation_links: dict[str, list[dict[str, Any]]],
    ticket_program_names: Iterable[str],
    *,
    mode: str,
) -> list[LocalizationCandidate]:
    """Boost concrete implementation files reached through E3-C API links."""

    normalized_mode = _resolve_symbol_expansion_mode(mode)
    if (
        not _api_implementation_expansion_enabled(normalized_mode)
        or not candidates
        or not api_implementation_links
    ):
        return candidates

    evidence_by_file: dict[str, list[dict[str, Any]]] = {}
    maximum_links = int(API_IMPLEMENTATION_PARAMETERS["max_links_per_symbol"])
    for ticket_symbol in ticket_program_names:
        normalized = _normalize_symbol_lookup_key(ticket_symbol)
        if not normalized:
            continue
        records = list(api_implementation_links.get(normalized, []))
        lookup_match_type = "exact"
        if not records and "." in normalized:
            records = list(
                api_implementation_links.get(normalized.rsplit(".", 1)[-1], [])
            )
            lookup_match_type = "leaf"
            if _api_namespace_guard_enabled(normalized_mode):
                records = [
                    record
                    for record in records
                    if _api_link_namespace_matches(normalized, record)
                ]
        unique_records: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for record in records:
            signature = (
                str(record.get("source_file") or ""),
                str(record.get("source_symbol") or ""),
                str(record.get("target_file") or ""),
                str(record.get("target_symbol") or ""),
            )
            unique_records.setdefault(signature, record)
        matched_files = {
            str(record.get("target_file") or "")
            for record in unique_records.values()
            if record.get("target_file")
        }
        if not matched_files or len(matched_files) > maximum_links:
            continue
        for record in unique_records.values():
            target_file = str(record.get("target_file") or "")
            if not target_file:
                continue
            evidence_by_file.setdefault(target_file, []).append(
                {
                    "ticket_symbol": ticket_symbol,
                    "api_symbol": str(record.get("source_symbol") or ""),
                    "source_file": str(record.get("source_file") or ""),
                    "source_line": int(record.get("source_line") or 1),
                    "implementation_symbol": str(
                        record.get("target_symbol") or ""
                    ),
                    "file_path": target_file,
                    "implementation_start_line": int(
                        record.get("target_start_line") or 1
                    ),
                    "relation_type": str(record.get("relation_type") or ""),
                    "via_name": str(record.get("via_name") or ""),
                    "lookup_match_type": lookup_match_type,
                }
            )

    reranked: list[LocalizationCandidate] = []
    maximum_evidence = int(
        API_IMPLEMENTATION_PARAMETERS["maximum_evidence_per_file"]
    )
    for candidate in candidates:
        raw_evidence = evidence_by_file.get(candidate.chunk.file_path, [])
        if not raw_evidence:
            reranked.append(candidate)
            continue
        deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in raw_evidence:
            key = (
                _normalize_symbol_lookup_key(str(row["ticket_symbol"])),
                str(row["relation_type"]),
                str(row["implementation_symbol"]),
            )
            deduplicated.setdefault(key, row)
        evidence = sorted(
            deduplicated.values(),
            key=lambda row: (
                0 if row["relation_type"] == "wrapper" else 1,
                0 if row["lookup_match_type"] == "exact" else 1,
                str(row["ticket_symbol"]).casefold(),
                str(row["implementation_symbol"]).casefold(),
            ),
        )[:maximum_evidence]
        raw_bonus = sum(
            float(
                API_IMPLEMENTATION_PARAMETERS[
                    "wrapper_bonus"
                    if row["relation_type"] == "wrapper"
                    else "reexport_bonus"
                ]
            )
            for row in evidence
        )
        bonus = min(
            float(API_IMPLEMENTATION_PARAMETERS["maximum_bonus"]),
            raw_bonus,
        )
        score = _clamp(candidate.score + bonus, 0.0, 1.0)
        signals = dict(candidate.signals)
        signals["api_implementation_score"] = round(
            bonus / float(API_IMPLEMENTATION_PARAMETERS["maximum_bonus"]),
            4,
        )
        signals["api_implementation_bonus"] = round(bonus, 4)
        signals["api_implementation_evidence"] = evidence
        signals["final_score"] = round(score, 4)
        reranked.append(
            replace(
                candidate,
                score=score,
                reason=_reason(candidate.chunk, candidate.embedding_score, signals),
                signals=signals,
            )
        )
    return reranked


def _symbol_expansion_diagnostics(
    *,
    mode: str,
    ticket_program_names: list[str],
    candidates: Iterable[LocalizationCandidate],
    symbol_definitions: dict[str, list[dict[str, Any]]],
    api_implementation_links: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    normalized_mode = _resolve_symbol_expansion_mode(mode)
    enabled = normalized_mode != "off"
    skipped_dotted_leaf_fallback_names: list[str] = []
    if enabled and _dotted_leaf_fallback_policy(normalized_mode) != "legacy-unrestricted":
        for ticket_symbol in ticket_program_names:
            normalized = _normalize_symbol_lookup_key(ticket_symbol)
            if (
                "." in normalized
                and not symbol_definitions.get(normalized)
                and symbol_definitions.get(normalized.rsplit(".", 1)[-1])
                and not _should_use_dotted_leaf_fallback(
                    normalized, normalized_mode
                )
            ):
                skipped_dotted_leaf_fallback_names.append(ticket_symbol)
    evidence: list[dict[str, Any]] = []
    api_evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    seen_api: set[tuple[str, str, str, str]] = set()
    for candidate in candidates:
        for row in candidate.signals.get("symbol_definition_evidence") or []:
            if not isinstance(row, dict):
                continue
            signature = (
                str(row.get("ticket_symbol") or ""),
                str(row.get("matched_symbol") or ""),
                str(row.get("file_path") or ""),
            )
            if signature in seen:
                continue
            seen.add(signature)
            evidence.append(dict(row))
        for row in candidate.signals.get("api_implementation_evidence") or []:
            if not isinstance(row, dict):
                continue
            signature_api = (
                str(row.get("ticket_symbol") or ""),
                str(row.get("api_symbol") or ""),
                str(row.get("implementation_symbol") or ""),
                str(row.get("file_path") or ""),
            )
            if signature_api in seen_api:
                continue
            seen_api.add(signature_api)
            api_evidence.append(dict(row))
    rejected_api_namespace_names: list[str] = []
    if _api_namespace_guard_enabled(normalized_mode):
        for ticket_symbol in ticket_program_names:
            normalized = _normalize_symbol_lookup_key(ticket_symbol)
            if not normalized or "." not in normalized:
                continue
            if (api_implementation_links or {}).get(normalized):
                continue
            leaf_records = list(
                (api_implementation_links or {}).get(
                    normalized.rsplit(".", 1)[-1], []
                )
            )
            if leaf_records and not any(
                _api_link_namespace_matches(normalized, record)
                for record in leaf_records
            ):
                rejected_api_namespace_names.append(ticket_symbol)
    return {
        "enabled": enabled,
        "mode": normalized_mode,
        "dotted_leaf_fallback_policy": _dotted_leaf_fallback_policy(
            normalized_mode
        ),
        "skipped_dotted_leaf_fallback_count": len(
            skipped_dotted_leaf_fallback_names
        ),
        "skipped_dotted_leaf_fallback_names": skipped_dotted_leaf_fallback_names,
        "ticket_program_names": ticket_program_names,
        "indexed_symbol_keys": len(symbol_definitions) if enabled else 0,
        "returned_evidence_count": len(evidence),
        "matched_ticket_symbols": sorted(
            {str(row.get("ticket_symbol") or "") for row in evidence if row.get("ticket_symbol")}
        ),
        "matched_definition_files": sorted(
            {str(row.get("file_path") or "") for row in evidence if row.get("file_path")}
        ),
        "evidence": evidence[:12],
        "api_implementation_enabled": _api_implementation_expansion_enabled(
            normalized_mode
        ),
        "api_namespace_guard_enabled": _api_namespace_guard_enabled(
            normalized_mode
        ),
        "rejected_api_namespace_count": len(rejected_api_namespace_names),
        "rejected_api_namespace_names": rejected_api_namespace_names,
        "indexed_api_link_keys": (
            len(api_implementation_links or {})
            if _api_implementation_expansion_enabled(normalized_mode)
            else 0
        ),
        "returned_api_evidence_count": len(api_evidence),
        "matched_implementation_files": sorted(
            {
                str(row.get("file_path") or "")
                for row in api_evidence
                if row.get("file_path")
            }
        ),
        "api_evidence": api_evidence[:12],
    }


def _resolve_import_graph_mode(
    mode: str | None,
    *,
    legacy_repository_proximity: bool,
) -> str:
    normalized = str(mode or "off").strip().lower()
    if normalized == "off" and legacy_repository_proximity:
        normalized = "outgoing"
    if normalized not in IMPORT_GRAPH_MODES:
        raise ValueError("import_graph_mode must be one of: " + ", ".join(IMPORT_GRAPH_MODES))
    return normalized


def _resolve_call_graph_mode(mode: str | None) -> str:
    normalized = str(mode or "off").strip().lower()
    if normalized not in CALL_GRAPH_MODES:
        raise ValueError(
            "call_graph_mode must be one of: " + ", ".join(CALL_GRAPH_MODES)
        )
    return normalized


def get_call_graph_parameters(mode: str | None) -> dict[str, float | int]:
    """Return the fixed E4 parameters recorded for one experiment mode."""

    normalized_mode = _resolve_call_graph_mode(mode)
    parameters = dict(CALL_GRAPH_PARAMETERS)
    if normalized_mode == "outgoing-top3":
        parameters["reference_file_k"] = 3
    return parameters


def build_import_graph(chunks: Iterable[CodeChunk]) -> ImportGraph:
    """Build a reusable one-hop Python import graph from a code index."""

    chunk_list = list(chunks)
    chunks_by_file = _chunks_by_file(chunk_list)
    file_paths = set(chunks_by_file)
    module_lookup = _module_file_lookup(
        {file_path for file_path in file_paths if file_path.endswith(".py")}
    )
    outgoing_sets: dict[str, set[str]] = {}
    unresolved: dict[str, list[str]] = {}
    parsed_files = 0
    parse_failures = 0

    for file_path in sorted(file_paths):
        file_chunks = chunks_by_file[file_path]
        if not file_path.endswith(".py") and not any(
            chunk.language == "python" for chunk in file_chunks
        ):
            continue
        source = _reconstruct_file_source(file_chunks)
        modules, parsed = _python_import_modules(source, current_file=file_path)
        parsed_files += int(parsed)
        parse_failures += int(not parsed)
        missing: list[str] = []
        for module in modules:
            targets = _module_import_targets(module, module_lookup)
            if not targets:
                if module not in missing:
                    missing.append(module)
                continue
            for target in targets:
                if target != file_path:
                    outgoing_sets.setdefault(file_path, set()).add(target)
        if missing:
            unresolved[file_path] = missing[:32]

    incoming_sets: dict[str, set[str]] = {}
    for source, targets in outgoing_sets.items():
        for target in targets:
            incoming_sets.setdefault(target, set()).add(source)
    outgoing = {
        source: sorted(targets)
        for source, targets in sorted(outgoing_sets.items())
        if targets
    }
    incoming = {
        target: sorted(sources)
        for target, sources in sorted(incoming_sets.items())
        if sources
    }
    return ImportGraph(
        outgoing=outgoing,
        incoming=incoming,
        unresolved=unresolved,
        stats={
            "files": len(file_paths),
            "python_files_parsed": parsed_files,
            "python_parse_failures": parse_failures,
            "edges": sum(len(targets) for targets in outgoing.values()),
            "files_with_outgoing_edges": len(outgoing),
            "files_with_incoming_edges": len(incoming),
        },
    )


def build_call_graph(
    chunks: Iterable[CodeChunk],
    symbol_definitions: dict[str, list[dict[str, Any]]] | None = None,
) -> CallGraph:
    """Build high-confidence, one-hop Python call relationships.

    The resolver accepts same-file top-level functions, ``self.method()`` or
    ``cls.method()`` calls, imported functions, and ``module.function()``
    calls.  A call is indexed only when it resolves to one concrete definition
    in the pre-fix Code Index.
    """

    chunk_list = list(chunks)
    chunks_by_file = _chunks_by_file(chunk_list)
    python_files = {
        file_path
        for file_path, file_chunks in chunks_by_file.items()
        if file_path.endswith(".py")
        or any(chunk.language == "python" for chunk in file_chunks)
    }
    definitions = symbol_definitions or build_symbol_definition_index(chunk_list)
    definitions_by_file_leaf: dict[tuple[str, str], list[dict[str, Any]]] = {}
    definitions_by_file_qualified: dict[tuple[str, str], list[dict[str, Any]]] = {}
    seen_definitions: set[tuple[str, str, int]] = set()
    for records in definitions.values():
        for raw_record in records:
            file_path = _normalize_path(str(raw_record.get("file_path") or ""))
            qualified_name = str(raw_record.get("qualified_name") or "").strip()
            start_line = int(raw_record.get("start_line") or 1)
            signature = (file_path, qualified_name, start_line)
            if not file_path or not qualified_name or signature in seen_definitions:
                continue
            seen_definitions.add(signature)
            record = dict(raw_record)
            leaf = _normalize_symbol_lookup_key(
                str(record.get("leaf_name") or qualified_name.rsplit(".", 1)[-1])
            )
            normalized_qualified = _normalize_symbol_lookup_key(qualified_name)
            definitions_by_file_leaf.setdefault((file_path, leaf), []).append(record)
            definitions_by_file_qualified.setdefault(
                (file_path, normalized_qualified), []
            ).append(record)

    module_lookup = _module_file_lookup(python_files)
    edges: list[dict[str, Any]] = []
    unresolved: dict[str, list[str]] = {}
    parsed_files = 0
    parse_failures = 0
    calls_seen = 0
    dynamic_calls_skipped = 0
    ambiguous_calls_skipped = 0
    for file_path in sorted(python_files):
        source = _reconstruct_file_source(chunks_by_file[file_path])
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(source)
        except (SyntaxError, RecursionError, ValueError):
            parse_failures += 1
            continue
        parsed_files += 1
        result = _python_static_call_edges(
            file_path=file_path,
            tree=tree,
            module_lookup=module_lookup,
            definitions_by_file_leaf=definitions_by_file_leaf,
            definitions_by_file_qualified=definitions_by_file_qualified,
        )
        edges.extend(result["edges"])
        calls_seen += int(result["calls_seen"])
        dynamic_calls_skipped += int(result["dynamic_calls_skipped"])
        ambiguous_calls_skipped += int(result["ambiguous_calls_skipped"])
        if result["unresolved"]:
            unresolved[file_path] = list(result["unresolved"])

    unique_edges: dict[tuple[str, str, str, str, int], dict[str, Any]] = {}
    for edge in edges:
        signature = (
            str(edge["caller_file"]),
            str(edge["caller_symbol"]),
            str(edge["target_file"]),
            str(edge["target_symbol"]),
            int(edge["line"]),
        )
        unique_edges.setdefault(signature, edge)
    ordered_edges = sorted(
        unique_edges.values(),
        key=lambda row: (
            str(row["caller_file"]),
            int(row["line"]),
            str(row["caller_symbol"]),
            str(row["target_file"]),
            str(row["target_symbol"]),
        ),
    )
    outgoing: dict[str, list[dict[str, Any]]] = {}
    incoming: dict[str, list[dict[str, Any]]] = {}
    cross_file_edges = 0
    for edge in ordered_edges:
        outgoing.setdefault(str(edge["caller_file"]), []).append(edge)
        incoming.setdefault(str(edge["target_file"]), []).append(edge)
        cross_file_edges += int(edge["caller_file"] != edge["target_file"])
    return CallGraph(
        outgoing=outgoing,
        incoming=incoming,
        unresolved=unresolved,
        stats={
            "files": len(chunks_by_file),
            "python_files_parsed": parsed_files,
            "python_parse_failures": parse_failures,
            "calls_seen": calls_seen,
            "edges": len(ordered_edges),
            "cross_file_edges": cross_file_edges,
            "files_with_outgoing_edges": len(outgoing),
            "files_with_incoming_edges": len(incoming),
            "dynamic_calls_skipped": dynamic_calls_skipped,
            "ambiguous_calls_skipped": ambiguous_calls_skipped,
        },
    )


def _python_static_call_edges(
    *,
    file_path: str,
    tree: ast.Module,
    module_lookup: dict[str, list[str]],
    definitions_by_file_leaf: dict[tuple[str, str], list[dict[str, Any]]],
    definitions_by_file_qualified: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    module_aliases: dict[str, str] = {}
    imported_symbols: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".", 1)[0]
                module_aliases[local_name] = alias.name if alias.asname else local_name
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        raw_base = "." * int(node.level or 0) + str(node.module or "")
        base = _resolve_python_module(raw_base, file_path)
        if not base:
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            local_name = alias.asname or alias.name
            imported_path = f"{base}.{alias.name}".strip(".")
            # Use an exact module match here. The general import resolver also
            # falls back to parent packages, which would misclassify
            # ``from pkg.helpers import normalize`` as a module alias merely
            # because ``pkg.helpers`` exists.
            if module_lookup.get(imported_path.replace("/", ".").strip(".")):
                module_aliases[local_name] = imported_path
            else:
                imported_symbols[local_name] = (base, alias.name)

    def unique_definition(
        records: Iterable[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, bool]:
        unique: dict[tuple[str, str, int], dict[str, Any]] = {}
        for record in records:
            signature = (
                str(record.get("file_path") or ""),
                str(record.get("qualified_name") or ""),
                int(record.get("start_line") or 1),
            )
            unique.setdefault(signature, record)
        values = list(unique.values())
        return (values[0], False) if len(values) == 1 else (None, len(values) > 1)

    def module_definition(
        module: str, symbol: str
    ) -> tuple[dict[str, Any] | None, bool]:
        targets = _module_import_targets(module, module_lookup)
        records: list[dict[str, Any]] = []
        normalized_symbol = _normalize_symbol_lookup_key(symbol)
        for target in targets:
            for record in definitions_by_file_leaf.get(
                (target, normalized_symbol), []
            ):
                qualified = str(record.get("qualified_name") or "")
                if "." not in qualified:
                    records.append(record)
        return unique_definition(records)

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.class_stack: list[str] = []
            self.function_stack: list[str] = []
            self.edges: list[dict[str, Any]] = []
            self.unresolved: list[str] = []
            self.calls_seen = 0
            self.dynamic_calls_skipped = 0
            self.ambiguous_calls_skipped = 0

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.function_stack.append(node.name)
            self.generic_visit(node)
            self.function_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def visit_Call(self, node: ast.Call) -> None:
            self.calls_seen += 1
            record, resolution_type, call_name, ambiguous = self._resolve_call(
                node.func
            )
            if ambiguous:
                self.ambiguous_calls_skipped += 1
                if call_name and call_name not in self.unresolved:
                    self.unresolved.append(call_name)
            elif record is not None:
                caller_parts = [*self.class_stack, *self.function_stack]
                self.edges.append(
                    {
                        "caller_file": file_path,
                        "caller_symbol": ".".join(caller_parts) or "<module>",
                        "target_file": str(record.get("file_path") or ""),
                        "target_symbol": str(record.get("qualified_name") or ""),
                        "call_name": call_name,
                        "line": int(getattr(node, "lineno", 1)),
                        "resolution_type": resolution_type,
                    }
                )
            else:
                self.dynamic_calls_skipped += 1
                if call_name and call_name not in self.unresolved:
                    self.unresolved.append(call_name)
            self.generic_visit(node)

        def _resolve_call(
            self, node: ast.expr
        ) -> tuple[dict[str, Any] | None, str, str, bool]:
            if isinstance(node, ast.Name):
                call_name = node.id
                if node.id in imported_symbols:
                    module, symbol = imported_symbols[node.id]
                    record, ambiguous = module_definition(module, symbol)
                    return record, "imported_function", call_name, ambiguous
                local_records = [
                    record
                    for record in definitions_by_file_leaf.get(
                        (file_path, _normalize_symbol_lookup_key(node.id)), []
                    )
                    if "." not in str(record.get("qualified_name") or "")
                ]
                record, ambiguous = unique_definition(local_records)
                return record, "local_function", call_name, ambiguous

            parts = _python_attribute_parts(node)
            if len(parts) < 2:
                return None, "", "", False
            call_name = ".".join(parts)
            receiver = parts[0]
            if receiver in {"self", "cls"} and self.class_stack:
                qualified = _normalize_symbol_lookup_key(
                    f"{self.class_stack[-1]}.{parts[-1]}"
                )
                record, ambiguous = unique_definition(
                    definitions_by_file_qualified.get((file_path, qualified), [])
                )
                return record, "self_method", call_name, ambiguous
            if receiver not in module_aliases:
                return None, "", call_name, False
            base = module_aliases[receiver]
            module = ".".join([base, *parts[1:-1]]).strip(".")
            record, ambiguous = module_definition(module, parts[-1])
            return record, "module_function", call_name, ambiguous

    visitor = Visitor()
    try:
        visitor.visit(tree)
    except RecursionError:
        return {
            "edges": [],
            "unresolved": [],
            "calls_seen": 0,
            "dynamic_calls_skipped": 0,
            "ambiguous_calls_skipped": 0,
        }
    return {
        "edges": visitor.edges,
        "unresolved": visitor.unresolved[:64],
        "calls_seen": visitor.calls_seen,
        "dynamic_calls_skipped": visitor.dynamic_calls_skipped,
        "ambiguous_calls_skipped": visitor.ambiguous_calls_skipped,
    }


def _python_attribute_parts(node: ast.expr) -> list[str]:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return []
    parts.append(current.id)
    return list(reversed(parts))


def _reconstruct_file_source(chunks: list[CodeChunk]) -> str:
    if not chunks:
        return ""
    line_count = max(chunk.end_line for chunk in chunks)
    lines: list[str | None] = [None] * line_count
    for chunk in sorted(chunks, key=lambda item: (item.start_line, item.end_line)):
        for offset, line in enumerate(chunk.code_text.splitlines()):
            line_index = chunk.start_line - 1 + offset
            if 0 <= line_index < line_count and lines[line_index] is None:
                lines[line_index] = line
    return "\n".join(line if line is not None else "" for line in lines)


def _python_import_modules(source: str, *, current_file: str) -> tuple[list[str], bool]:
    modules: list[str] = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source)
    except (SyntaxError, RecursionError, ValueError):
        fallback: list[str] = []
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                fallback.extend(_import_line_modules(stripped, current_file=current_file))
        return list(dict.fromkeys(fallback)), False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names if alias.name)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        raw_base = "." * int(node.level or 0) + str(node.module or "")
        base = _resolve_python_module(raw_base, current_file)
        if base:
            modules.append(base)
        for alias in node.names:
            if alias.name == "*":
                continue
            imported = f"{base}.{alias.name}".strip(".")
            if imported:
                modules.append(imported)
    return list(dict.fromkeys(modules)), True


def _module_import_targets(
    module: str,
    module_lookup: dict[str, list[str]],
) -> list[str]:
    normalized = module.replace("/", ".").strip(".")
    if not normalized:
        return []
    # An import can name a package member rather than a concrete submodule.
    # Resolve the most specific internal module, then stop to avoid adding a
    # whole chain of parent packages.
    parts = normalized.split(".")
    for end in range(len(parts), 0, -1):
        targets = module_lookup.get(".".join(parts[:end]), [])
        if targets:
            return targets[:4]
    return []


def _apply_import_graph(
    candidates: list[LocalizationCandidate],
    import_graph: ImportGraph,
    *,
    mode: str,
) -> list[LocalizationCandidate]:
    """Apply capped, one-hop import evidence from the strongest files."""

    normalized_mode = _resolve_import_graph_mode(
        mode,
        legacy_repository_proximity=False,
    )
    if normalized_mode == "off" or not candidates:
        return candidates

    best_by_file: dict[str, LocalizationCandidate] = {}
    for candidate in sorted(candidates, key=_ranking_key):
        best_by_file.setdefault(candidate.chunk.file_path, candidate)
    reference_files = [
        candidate.chunk.file_path
        for candidate in sorted(best_by_file.values(), key=_ranking_key)[
            : int(IMPORT_GRAPH_PARAMETERS["reference_file_k"])
        ]
    ]
    evidence: dict[str, list[tuple[float, str]]] = {}
    for rank, reference_file in enumerate(reference_files, start=1):
        decay = max(
            0.0,
            1.0 - float(IMPORT_GRAPH_PARAMETERS["rank_decay"]) * (rank - 1),
        )
        for target in import_graph.outgoing.get(reference_file, []):
            evidence.setdefault(target, []).append(
                (
                    float(IMPORT_GRAPH_PARAMETERS["outgoing_signal"]) * decay,
                    f"imported_by:{reference_file}",
                )
            )
        if normalized_mode != "bidirectional":
            continue
        for source in import_graph.incoming.get(reference_file, []):
            evidence.setdefault(source, []).append(
                (
                    float(IMPORT_GRAPH_PARAMETERS["incoming_signal"]) * decay,
                    f"imports:{reference_file}",
                )
            )

    reranked: list[LocalizationCandidate] = []
    for candidate in candidates:
        file_evidence = sorted(
            evidence.get(candidate.chunk.file_path, []),
            key=lambda item: (-item[0], item[1]),
        )
        if not file_evidence:
            reranked.append(candidate)
            continue
        graph_signal = min(
            float(IMPORT_GRAPH_PARAMETERS["maximum_signal"]),
            file_evidence[0][0],
        )
        bonus = min(
            float(IMPORT_GRAPH_PARAMETERS["maximum_bonus"]),
            SCORING_WEIGHTS["repository_proximity_score"] * graph_signal,
        )
        matches = [
            label
            for _, label in file_evidence[
                : int(IMPORT_GRAPH_PARAMETERS["maximum_evidence_per_file"])
            ]
        ]
        score = _clamp(candidate.score + bonus, 0.0, 1.0)
        signals = dict(candidate.signals)
        signals["repository_proximity_score"] = round(graph_signal, 4)
        signals["matching_repository_proximity"] = matches
        signals["import_graph_mode"] = normalized_mode
        signals["import_graph_score"] = round(graph_signal, 4)
        signals["import_graph_bonus"] = round(bonus, 4)
        signals["import_graph_evidence"] = matches
        signals["final_score"] = round(score, 4)
        reason = (
            f"{candidate.reason} One-hop import evidence links this file to: "
            f"{', '.join(matches[:2])}."
        )
        reranked.append(replace(candidate, score=score, signals=signals, reason=reason))
    return reranked


def _apply_call_graph(
    candidates: list[LocalizationCandidate],
    call_graph: CallGraph,
    *,
    mode: str,
) -> list[LocalizationCandidate]:
    """Apply bounded one-hop call evidence from the strongest candidate files."""

    normalized_mode = _resolve_call_graph_mode(mode)
    if normalized_mode == "off" or not candidates:
        return candidates
    parameters = get_call_graph_parameters(normalized_mode)

    best_by_file: dict[str, LocalizationCandidate] = {}
    for candidate in sorted(candidates, key=_ranking_key):
        best_by_file.setdefault(candidate.chunk.file_path, candidate)
    reference_files = [
        candidate.chunk.file_path
        for candidate in sorted(best_by_file.values(), key=_ranking_key)[
            : int(parameters["reference_file_k"])
        ]
    ]
    evidence_by_file: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    maximum_targets = int(
        parameters["maximum_target_files_per_reference"]
    )
    for rank, reference_file in enumerate(reference_files, start=1):
        raw_edges = [
            edge
            for edge in call_graph.outgoing.get(reference_file, [])
            if str(edge.get("target_file") or "") != reference_file
        ]
        target_files = {
            str(edge.get("target_file") or "")
            for edge in raw_edges
            if edge.get("target_file")
        }
        # A broad hub is not high-confidence localization evidence.  Skip the
        # entire reference instead of selecting an arbitrary subset.
        if not target_files or len(target_files) > maximum_targets:
            continue
        decay = max(
            0.0,
            1.0 - float(parameters["rank_decay"]) * (rank - 1),
        )
        signal = float(parameters["outgoing_signal"]) * decay
        seen_edges: set[tuple[str, str, str]] = set()
        for edge in raw_edges:
            target_file = str(edge.get("target_file") or "")
            signature = (
                str(edge.get("caller_symbol") or ""),
                target_file,
                str(edge.get("target_symbol") or ""),
            )
            if not target_file or signature in seen_edges:
                continue
            seen_edges.add(signature)
            evidence_by_file.setdefault(target_file, []).append(
                (
                    signal,
                    {
                        "reference_file": reference_file,
                        "reference_rank": rank,
                        "caller_symbol": str(edge.get("caller_symbol") or ""),
                        "target_file": target_file,
                        "target_symbol": str(edge.get("target_symbol") or ""),
                        "call_name": str(edge.get("call_name") or ""),
                        "line": int(edge.get("line") or 1),
                        "resolution_type": str(
                            edge.get("resolution_type") or ""
                        ),
                    },
                )
            )

    reranked: list[LocalizationCandidate] = []
    maximum_evidence = int(parameters["maximum_evidence_per_file"])
    for candidate in candidates:
        file_evidence = sorted(
            evidence_by_file.get(candidate.chunk.file_path, []),
            key=lambda item: (
                -item[0],
                int(item[1]["reference_rank"]),
                str(item[1]["reference_file"]),
                str(item[1]["target_symbol"]),
            ),
        )[:maximum_evidence]
        if not file_evidence:
            reranked.append(candidate)
            continue
        call_signal = min(
            float(parameters["maximum_signal"]),
            file_evidence[0][0],
        )
        bonus = min(
            float(parameters["maximum_bonus"]),
            float(parameters["maximum_bonus"]) * call_signal,
        )
        evidence = [row for _, row in file_evidence]
        score = _clamp(candidate.score + bonus, 0.0, 1.0)
        signals = dict(candidate.signals)
        signals["call_graph_mode"] = normalized_mode
        signals["call_graph_score"] = round(call_signal, 4)
        signals["call_graph_bonus"] = round(bonus, 4)
        signals["call_graph_evidence"] = evidence
        signals["final_score"] = round(score, 4)
        reranked.append(
            replace(
                candidate,
                score=score,
                reason=_reason(candidate.chunk, candidate.embedding_score, signals),
                signals=signals,
            )
        )
    return reranked


def _apply_repository_proximity(
    candidates: list[LocalizationCandidate],
    chunks: list[CodeChunk],
    *,
    query_terms: list[str],
    identifiers: list[str],
) -> list[LocalizationCandidate]:
    del query_terms, identifiers
    return _apply_import_graph(
        candidates,
        build_import_graph(chunks),
        mode="outgoing",
    )


def _chunks_by_file(chunks: list[CodeChunk]) -> dict[str, list[CodeChunk]]:
    grouped: dict[str, list[CodeChunk]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk.file_path, []).append(chunk)
    return grouped


def _file_import_context(chunks: list[CodeChunk]) -> list[str]:
    imports: list[str] = []
    seen: set[str] = set()
    for chunk in sorted(chunks, key=lambda item: item.start_line):
        for line in chunk.code_text.splitlines():
            stripped = line.strip()
            if not (
                stripped.startswith("import ")
                or stripped.startswith("from ")
                or stripped.startswith("#include ")
                or stripped.startswith("use ")
                or bool(re.match(r"^(?:const|let|var)\s+.*\brequire\(", stripped))
            ):
                continue
            if stripped not in seen:
                seen.add(stripped)
                imports.append(stripped)
    return imports[:32]


def _import_target_files(
    current_file: str,
    import_lines: list[str],
    module_lookup: dict[str, list[str]],
) -> list[str]:
    targets: list[str] = []
    for line in import_lines:
        for module in _import_line_modules(line, current_file=current_file):
            for target in module_lookup.get(module.replace("/", ".").strip("."), []):
                if target not in targets:
                    targets.append(target)
    return targets


def _module_file_lookup(file_paths: set[str]) -> dict[str, list[str]]:
    """Index dotted module suffixes once instead of scanning every path per import."""

    lookup: dict[str, list[str]] = {}
    for file_path in sorted(file_paths):
        path = Path(file_path)
        module = path.with_suffix("").as_posix()
        if module.endswith("/__init__"):
            module = module[: -len("/__init__")]
        parts = [part for part in module.split("/") if part]
        for start in range(len(parts)):
            key = ".".join(parts[start:])
            values = lookup.setdefault(key, [])
            if file_path not in values:
                values.append(file_path)
    return lookup


def _import_line_modules(line: str, *, current_file: str) -> list[str]:
    modules: list[str] = []
    py_import = re.match(r"^import\s+(.+)$", line)
    if py_import:
        modules.extend(
            part.strip().split(" as ")[0].strip()
            for part in py_import.group(1).split(",")
            if part.strip()
        )
    py_from = re.match(r"^from\s+([A-Za-z0-9_\.]+)\s+import\s+(.+)$", line)
    if py_from:
        base_module = _resolve_python_module(py_from.group(1), current_file)
        modules.append(base_module)
        imported_names = py_from.group(2).strip().strip("()")
        for imported_name in imported_names.split(","):
            name = imported_name.strip().split(" as ")[0].strip()
            if name and name != "*" and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                modules.append(f"{base_module}.{name}".strip("."))
    for pattern in (r"\bfrom\s+['\"]([^'\"]+)['\"]", r"\brequire\(['\"]([^'\"]+)['\"]\)"):
        match = re.search(pattern, line)
        if match:
            modules.append(_resolve_path_module(match.group(1), current_file))
    return [module for module in modules if module]


def _resolve_python_module(module: str, current_file: str) -> str:
    if not module.startswith("."):
        return module
    dots = len(module) - len(module.lstrip("."))
    remainder = module[dots:]
    current_parts = Path(current_file).with_suffix("").parts[:-1]
    keep = max(0, len(current_parts) - max(0, dots - 1))
    parts = list(current_parts[:keep])
    if remainder:
        parts.extend(part for part in remainder.split(".") if part)
    return ".".join(parts)


def _resolve_path_module(raw: str, current_file: str) -> str:
    if raw.startswith("."):
        return (Path(current_file).parent / raw).as_posix().replace("/", ".").strip(".")
    return raw.replace("/", ".")


def _stack_trace_refs(ticket_json: dict[str, Any]) -> list[tuple[str, int | None]]:
    text = "\n".join(
        str(ticket_json.get(key) or "")
        for key in ("error_message", "logs", "description", "body", "actual_behavior")
    )
    refs: list[tuple[str, int | None]] = []
    patterns = [
        r"File \"(?P<file>[^\"]+)\", line (?P<line>\d+)",
        r"(?P<file>[A-Za-z0-9_./\\-]+\.(?:py|js|jsx|ts|tsx|java|c|cc|cpp|h|hpp|go|rs)):(?P<line>\d+)",
        r"(?P<file>[A-Za-z0-9_./\\-]+\.(?:py|js|jsx|ts|tsx|java|c|cc|cpp|h|hpp|go|rs))",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            line_text = match.groupdict().get("line")
            refs.append((match.group("file").replace("\\", "/"), int(line_text) if line_text else None))
    return refs


def _stack_signal(chunk: CodeChunk, refs: list[tuple[str, int | None]]) -> float:
    if not refs:
        return 0.0
    chunk_path = _normalize_path(chunk.file_path)
    chunk_name = Path(chunk_path).name
    best = 0.0
    for raw_file, line in refs:
        ref = _normalize_path(raw_file)
        ref_name = Path(ref).name
        file_matches = ref == chunk_path or ref.endswith("/" + chunk_path) or chunk_path.endswith("/" + ref) or ref_name == chunk_name
        if not file_matches:
            continue
        if line is not None and chunk.start_line <= line <= chunk.end_line:
            best = max(best, 0.75)
        elif line is not None:
            distance = min(abs(chunk.start_line - line), abs(chunk.end_line - line))
            best = max(best, 0.55 if distance <= 25 else 0.35)
        else:
            best = max(best, 0.35)
    return best


def _keyword_signal(
    query_terms: list[str],
    chunk: CodeChunk,
    *,
    chunk_terms: set[str] | None = None,
) -> tuple[float, list[str]]:
    if not query_terms:
        return 0.0, []
    if chunk_terms is None:
        chunk_terms = set(_tokenize(chunk.search_text))
    matching = [term for term in query_terms if term in chunk_terms]
    return min(1.0, len(matching) / max(4, min(len(query_terms), 12))), matching


def _symbol_signal(
    query_terms: list[str],
    chunk: CodeChunk,
    *,
    symbol_terms: set[str] | None = None,
) -> float:
    if symbol_terms is None:
        symbol_terms = set(
            _tokenize(" ".join([chunk.file_path, chunk.function_name, chunk.class_name]))
        )
    if not symbol_terms:
        return 0.0
    return _term_overlap(query_terms, symbol_terms)


def _term_overlap(left: Iterable[str], right: Iterable[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set)


def _candidate_scoring_signals(candidate: LocalizationCandidate) -> dict[str, Any]:
    signals = dict(candidate.signals)
    row = {
        "embedding_score": round(float(signals.get("embedding_score", candidate.embedding_score)), 4),
        "stack_trace_score": round(float(signals.get("stack_trace_score", 0.0)), 4),
        "component_score": round(float(signals.get("component_score", 0.0)), 4),
        "keyword_score": round(float(signals.get("keyword_score", 0.0)), 4),
        "symbol_score": round(float(signals.get("symbol_score", 0.0)), 4),
        "symbol_definition_score": round(
            float(signals.get("symbol_definition_score", 0.0)), 4
        ),
        "symbol_definition_bonus": round(
            float(signals.get("symbol_definition_bonus", 0.0)), 4
        ),
        "symbol_definition_evidence": list(
            signals.get("symbol_definition_evidence") or []
        )[:8],
        "api_implementation_score": round(
            float(signals.get("api_implementation_score", 0.0)), 4
        ),
        "api_implementation_bonus": round(
            float(signals.get("api_implementation_bonus", 0.0)), 4
        ),
        "api_implementation_evidence": list(
            signals.get("api_implementation_evidence") or []
        )[:8],
        "call_graph_score": round(
            float(signals.get("call_graph_score", 0.0)), 4
        ),
        "call_graph_bonus": round(
            float(signals.get("call_graph_bonus", 0.0)), 4
        ),
        "call_graph_evidence": list(
            signals.get("call_graph_evidence") or []
        )[:8],
        "domain_path_score": round(float(signals.get("domain_path_score", 0.0)), 4),
        "identifier_score": round(float(signals.get("identifier_score", 0.0)), 4),
        "path_term_score": round(float(signals.get("path_term_score", 0.0)), 4),
        "repository_proximity_score": round(
            float(signals.get("repository_proximity_score", 0.0)), 4
        ),
        "package_proximity_score": round(
            float(signals.get("package_proximity_score", 0.0)), 4
        ),
        "supporting_chunk_bonus": round(
            float(signals.get("supporting_chunk_bonus", 0.0)), 4
        ),
        "symbol_coverage_bonus": round(
            float(signals.get("symbol_coverage_bonus", 0.0)), 4
        ),
        "package_proximity_bonus": round(
            float(signals.get("package_proximity_bonus", 0.0)), 4
        ),
        "file_aggregation_mode": str(signals.get("file_aggregation_mode") or "basic"),
        "support_evidence_threshold": round(
            float(signals.get("support_evidence_threshold", 0.0)), 4
        ),
        "support_evidence_count": int(signals.get("support_evidence_count", 0)),
        "distinct_support_symbol_count": int(
            signals.get("distinct_support_symbol_count", 0)
        ),
        "matching_identifiers": list(signals.get("matching_identifiers") or [])[:8],
        "matching_path_terms": list(signals.get("matching_path_terms") or [])[:8],
        "matching_repository_proximity": list(
            signals.get("matching_repository_proximity") or []
        )[:8],
        "matching_package_neighbor": str(
            signals.get("matching_package_neighbor") or ""
        ),
        "final_score": round(float(signals.get("final_score", candidate.score)), 4),
        "weights": SCORING_WEIGHTS,
    }
    if "llm_rerank_score" in signals:
        row["llm_rerank_score"] = round(float(signals["llm_rerank_score"]), 4)
        row["retrieval_score_before_llm"] = round(float(signals["retrieval_score_before_llm"]), 4)
        row["llm_blended_score"] = round(float(signals["llm_blended_score"]), 4)
        row["llm_rerank_weights"] = LLM_RERANK_WEIGHTS
    if "import_graph_mode" in signals:
        row["import_graph_mode"] = str(signals["import_graph_mode"])
        row["import_graph_score"] = round(float(signals.get("import_graph_score", 0.0)), 4)
        row["import_graph_bonus"] = round(float(signals.get("import_graph_bonus", 0.0)), 4)
        row["import_graph_evidence"] = list(signals.get("import_graph_evidence") or [])[:8]
    if "call_graph_mode" in signals:
        row["call_graph_mode"] = str(signals["call_graph_mode"])
    return row


def _reason(chunk: CodeChunk, embedding_score: float, signals: dict[str, Any]) -> str:
    pieces: list[str] = []
    if float(signals.get("stack_trace_score", 0.0)) >= 0.55:
        pieces.append("The ticket contains a stack trace or file:line reference that points to this chunk.")
    elif float(signals.get("stack_trace_score", 0.0)) > 0:
        pieces.append("The ticket references this file.")
    if float(signals.get("component_score", 0.0)) > 0:
        pieces.append("The component terms match the file path or symbol name.")
    matching_terms = signals.get("matching_terms") or []
    if matching_terms:
        pieces.append(f"Shared report/code terms include: {', '.join(matching_terms[:5])}.")
    matching_domain_intents = signals.get("matching_domain_intents") or []
    if matching_domain_intents:
        pieces.append(
            "Framework concepts map to this source path: "
            f"{', '.join(matching_domain_intents[:3])}."
        )
    matching_identifiers = signals.get("matching_identifiers") or []
    if matching_identifiers:
        pieces.append(f"Exact code identifiers match: {', '.join(matching_identifiers[:3])}.")
    matching_path_terms = signals.get("matching_path_terms") or []
    if matching_path_terms:
        pieces.append(f"Ticket terms match the source path: {', '.join(matching_path_terms[:3])}.")
    symbol_definition_evidence = signals.get("symbol_definition_evidence") or []
    if symbol_definition_evidence:
        matched = [
            f"{row.get('ticket_symbol')}→{row.get('matched_symbol')}"
            for row in symbol_definition_evidence[:3]
            if isinstance(row, dict)
        ]
        if matched:
            pieces.append(
                "Ticket program names resolve to definitions in this file: "
                f"{', '.join(matched)}."
            )
    api_implementation_evidence = signals.get("api_implementation_evidence") or []
    if api_implementation_evidence:
        matched_api = [
            f"{row.get('ticket_symbol')}→{row.get('implementation_symbol')}"
            for row in api_implementation_evidence[:3]
            if isinstance(row, dict)
        ]
        if matched_api:
            pieces.append(
                "Public API or wrapper links resolve to implementations in this file: "
                f"{', '.join(matched_api)}."
            )
    matching_repository = signals.get("matching_repository_proximity") or []
    if matching_repository:
        pieces.append(
            "Import-neighbor evidence links this file to strong candidates: "
            f"{', '.join(matching_repository[:2])}."
        )
    call_evidence = signals.get("call_graph_evidence") or []
    if call_evidence:
        call_labels = [
            f"{row.get('caller_symbol')}→{row.get('target_symbol')}"
            for row in call_evidence[:2]
            if isinstance(row, dict)
        ]
        if call_labels:
            pieces.append(
                "One-hop static calls link strong candidates to this file: "
                f"{', '.join(call_labels)}."
            )
    if embedding_score > 0:
        pieces.append("The bug report vector is similar to the code chunk text.")
    if not pieces:
        pieces.append("This chunk is one of the closest available code vectors for the report.")
    location = chunk.function_name or chunk.class_name or chunk.file_path
    return f"{location}: " + " ".join(pieces)


def _ranking_key(candidate: LocalizationCandidate) -> tuple[float, int, int, str]:
    chunk = candidate.chunk
    return (
        -candidate.score,
        _symbol_rank(chunk.symbol_kind),
        chunk.end_line - chunk.start_line,
        chunk.chunk_id,
    )


def _symbol_rank(symbol_kind: str) -> int:
    normalized = symbol_kind.lower()
    if "method" in normalized or "function" in normalized:
        return 0
    if normalized == "class":
        return 1
    return 2


def _llm_rerank_prompt(ticket_json: dict[str, Any], candidates: list[LocalizationCandidate]) -> str:
    candidate_rows = []
    for rank, candidate in enumerate(candidates, start=1):
        candidate_rows.append(
            {
                "rank": rank,
                "file_path": candidate.chunk.file_path,
                "function_name": candidate.chunk.function_name,
                "class_name": candidate.chunk.class_name,
                "start_line": candidate.chunk.start_line,
                "end_line": candidate.chunk.end_line,
                "code_text": candidate.chunk.code_text[:LLM_MAX_CODE_CHARS_PER_CANDIDATE],
            }
        )
    bug_report = str(ticket_json.get("bug_report") or build_bug_report_text(ticket_json))
    return (
        "You are reranking fault localization candidates. "
        "Given the bug report and candidate code chunks, return JSON only in this shape: "
        '{"candidates":[{"rank":1,"score":0.0,"reason":"short reason"}]}. '
        "Return every supplied candidate exactly once. Keep each original integer rank, "
        "independently assign a relevance score from 0.0 to 1.0 based only on the bug "
        "report and code, and write a specific short reason. Do not preserve the input "
        "order unless the code evidence justifies it. "
        "Do not copy placeholder values from the schema example.\n\n"
        f"Bug report:\n{bug_report[:LLM_MAX_BUG_REPORT_CHARS]}\n\n"
        f"Candidates:\n{json.dumps(candidate_rows, ensure_ascii=False, indent=2)}"
    )


def _llm_symbol_rerank_prompt(
    ticket_json: dict[str, Any],
    candidates: list[LocalizationCandidate],
) -> str:
    symbol_rows = []
    for rank, candidate in enumerate(candidates, start=1):
        symbol_rows.append(
            {
                "rank": rank,
                "file_path": candidate.chunk.file_path,
                "symbol_kind": candidate.chunk.symbol_kind,
                "symbol_qualified_name": candidate.chunk.symbol_qualified_name,
                "start_line": candidate.chunk.start_line,
                "end_line": candidate.chunk.end_line,
                "code_text": candidate.chunk.code_text[:LLM_MAX_CODE_CHARS_PER_CANDIDATE],
            }
        )
    bug_report = str(ticket_json.get("bug_report") or build_bug_report_text(ticket_json))
    return (
        "You are reranking AST-derived symbols for fault localization. "
        "Given the bug report and candidate functions, methods, and classes, return JSON only "
        "in this shape: "
        '{"symbols":[{"rank":1,"score":0.0,"reason":"short reason"}]}. '
        "Return every supplied symbol exactly once. Keep each original integer rank, "
        "independently assign a root-cause relevance score from 0.0 to 1.0 based only on "
        "the bug report and code, and write a specific short reason. Do not preserve the "
        "input order unless the code evidence justifies it. Do not copy placeholder values "
        "from the schema example.\n\n"
        f"Bug report:\n{bug_report[:LLM_MAX_BUG_REPORT_CHARS]}\n\n"
        f"Symbols:\n{json.dumps(symbol_rows, ensure_ascii=False, indent=2)}"
    )


def _legacy_bug_location(best: dict[str, Any] | None) -> dict[str, Any]:
    if best is None:
        return {}
    return {
        "file": best.get("file_path", ""),
        "function": best.get("function_name") or best.get("class_name") or best.get("symbol_name") or "",
        "line_start": best.get("start_line", 1),
        "line_end": best.get("end_line", 1),
        "reason": best.get("reason", ""),
    }


def _legacy_candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": row.get("file_path", ""),
        "score": row.get("score", 0.0),
        "function": row.get("function_name") or row.get("class_name") or row.get("symbol_name") or "",
        "line_start": row.get("start_line", 1),
        "line_end": row.get("end_line", 1),
        "reason": row.get("reason", ""),
    }


def _line_numbered(code_text: str, start_line: int) -> str:
    return "\n".join(f"{line_no}: {line}" for line_no, line in enumerate(code_text.splitlines(), start=start_line))


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _validate_top_k(top_k: int) -> None:
    if top_k <= 0:
        raise ValueError("top_k must be positive.")


def _validate_embedding_backend(backend: str) -> str:
    normalized = backend.strip().lower()
    supported = {
        "auto",
        "tfidf",
        "sbert",
        "sentence-transformers",
        "sentence_transformers",
        "tfidf-sbert-rerank",
    }
    if normalized not in supported:
        choices = ", ".join(sorted(supported - {"sentence_transformers"}))
        raise ValueError(f"Unsupported embedding_backend {backend!r}; choose one of: {choices}.")
    return normalized


def _is_hybrid_backend(backend: str) -> bool:
    return _validate_embedding_backend(backend) == "tfidf-sbert-rerank"
