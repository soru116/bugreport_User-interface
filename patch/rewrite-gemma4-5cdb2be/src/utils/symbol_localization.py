from __future__ import annotations

import ast
import hashlib
import io
import math
import tokenize
import warnings
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Sequence


SYMBOL_RECORD_SCHEMA_VERSION = "symbol-record-v1"
SYMBOL_PARSE_RESULT_SCHEMA_VERSION = "symbol-parse-result-v2"

PARSE_STATUSES = frozenset(
    {
        "ok",
        "no_symbols",
        "syntax_error",
        "parser_error",
        "unsupported_language",
        "missing_file",
        "invalid_path",
        "symlink_escape",
    }
)

CALLABLE_SYMBOL_KINDS = frozenset(
    {"function", "async_function", "method", "async_method"}
)


def normalize_repository_path(value: str) -> str:
    """Return one safe, portable repository-relative path.

    Paths in SymbolRecordV1 always use POSIX separators. Absolute paths,
    parent traversal, and an empty path are rejected at the schema boundary.
    """

    text = str(value or "").strip()
    if not text:
        raise ValueError("file_path must be a non-empty repository-relative path.")
    if PurePosixPath(text.replace("\\", "/")).is_absolute() or PureWindowsPath(text).is_absolute():
        raise ValueError("file_path must be repository-relative, not absolute.")
    parts = [part for part in text.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("file_path must not contain parent traversal.")
    return PurePosixPath(*parts).as_posix()


def make_stable_symbol_id(
    *,
    repo: str,
    base_commit: str,
    file_path: str,
    language: str,
    symbol_kind: str,
    qualified_name: str,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
) -> str:
    """Build the deterministic SymbolRecordV1 identity.

    The digest deliberately includes the source range. Two same-named symbols
    in the same file therefore remain distinct, while repeated extraction of
    the same repository snapshot produces the same ID.
    """

    normalized_path = normalize_repository_path(file_path)
    fields = (
        str(repo or "").strip(),
        str(base_commit or "").strip(),
        normalized_path,
        str(language or "").strip().casefold(),
        str(symbol_kind or "").strip(),
        str(qualified_name or "").strip(),
        str(int(start_line)),
        str(int(start_column)),
        str(int(end_line)),
        str(int(end_column)),
    )
    digest = hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True, slots=True)
class SymbolRecordV1:
    """Versioned, file-qualified AST symbol record.

    Line numbers are one-based. Columns follow Python AST and are zero-based.
    """

    symbol_id: str
    repo: str
    base_commit: str
    file_path: str
    language: str
    symbol_kind: str
    qualified_name: str
    display_name: str
    parent_symbol_id: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    signature: str
    source_file_rank: int
    source_file_score: float
    ast_input_source: str
    parse_status: str = "ok"
    schema_version: str = SYMBOL_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        normalized_path = normalize_repository_path(self.file_path)
        object.__setattr__(self, "file_path", normalized_path)
        object.__setattr__(self, "repo", self.repo.strip())
        object.__setattr__(self, "base_commit", self.base_commit.strip())
        object.__setattr__(self, "language", self.language.strip().casefold())
        object.__setattr__(self, "qualified_name", self.qualified_name.strip())
        object.__setattr__(self, "display_name", self.display_name.strip())
        object.__setattr__(self, "parent_symbol_id", self.parent_symbol_id.strip())
        object.__setattr__(self, "signature", self.signature.strip())
        object.__setattr__(self, "ast_input_source", self.ast_input_source.strip())

        if self.schema_version != SYMBOL_RECORD_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SYMBOL_RECORD_SCHEMA_VERSION!r}.")
        if not self.symbol_id.startswith("sha256:") or len(self.symbol_id) != 71:
            raise ValueError("symbol_id must be a sha256-prefixed 64-character digest.")
        if not self.repo:
            raise ValueError("repo must be non-empty.")
        if not self.base_commit:
            raise ValueError("base_commit must be non-empty.")
        if not self.language:
            raise ValueError("language must be non-empty.")
        if not self.symbol_kind:
            raise ValueError("symbol_kind must be non-empty.")
        if not self.qualified_name:
            raise ValueError("qualified_name must be non-empty.")
        if not self.display_name:
            raise ValueError("display_name must be non-empty.")
        if self.start_line <= 0 or self.end_line <= 0:
            raise ValueError("symbol line numbers must be positive.")
        if self.start_column < 0 or self.end_column < 0:
            raise ValueError("symbol columns must be non-negative.")
        if (self.end_line, self.end_column) < (self.start_line, self.start_column):
            raise ValueError("symbol end position must not precede its start position.")
        if self.source_file_rank < 0:
            raise ValueError("source_file_rank must be non-negative.")
        if not math.isfinite(float(self.source_file_score)):
            raise ValueError("source_file_score must be finite.")
        if not self.ast_input_source:
            raise ValueError("ast_input_source must be non-empty.")
        if self.parse_status != "ok":
            raise ValueError("SymbolRecordV1 parse_status must be 'ok'.")

    @classmethod
    def create(
        cls,
        *,
        repo: str,
        base_commit: str,
        file_path: str,
        language: str,
        symbol_kind: str,
        qualified_name: str,
        display_name: str,
        parent_symbol_id: str,
        start_line: int,
        start_column: int,
        end_line: int,
        end_column: int,
        signature: str,
        source_file_rank: int = 0,
        source_file_score: float = 0.0,
        ast_input_source: str = "stage2_retrieval_baseline",
    ) -> "SymbolRecordV1":
        symbol_id = make_stable_symbol_id(
            repo=repo,
            base_commit=base_commit,
            file_path=file_path,
            language=language,
            symbol_kind=symbol_kind,
            qualified_name=qualified_name,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
        )
        return cls(
            symbol_id=symbol_id,
            repo=repo,
            base_commit=base_commit,
            file_path=file_path,
            language=language,
            symbol_kind=symbol_kind,
            qualified_name=qualified_name,
            display_name=display_name,
            parent_symbol_id=parent_symbol_id,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
            signature=signature,
            source_file_rank=source_file_rank,
            source_file_score=float(source_file_score),
            ast_input_source=ast_input_source,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol_id": self.symbol_id,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "file_path": self.file_path,
            "language": self.language,
            "symbol_kind": self.symbol_kind,
            "qualified_name": self.qualified_name,
            "display_name": self.display_name,
            "parent_symbol_id": self.parent_symbol_id,
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "signature": self.signature,
            "source_file_rank": self.source_file_rank,
            "source_file_score": self.source_file_score,
            "ast_input_source": self.ast_input_source,
            "parse_status": self.parse_status,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SymbolRecordV1":
        return cls(
            schema_version=str(
                payload.get("schema_version") or SYMBOL_RECORD_SCHEMA_VERSION
            ),
            symbol_id=str(payload.get("symbol_id") or ""),
            repo=str(payload.get("repo") or ""),
            base_commit=str(payload.get("base_commit") or ""),
            file_path=str(payload.get("file_path") or ""),
            language=str(payload.get("language") or ""),
            symbol_kind=str(payload.get("symbol_kind") or ""),
            qualified_name=str(payload.get("qualified_name") or ""),
            display_name=str(payload.get("display_name") or ""),
            parent_symbol_id=str(payload.get("parent_symbol_id") or ""),
            start_line=int(payload.get("start_line") or 0),
            start_column=int(payload.get("start_column") or 0),
            end_line=int(payload.get("end_line") or 0),
            end_column=int(payload.get("end_column") or 0),
            signature=str(payload.get("signature") or ""),
            source_file_rank=int(payload.get("source_file_rank") or 0),
            source_file_score=float(payload.get("source_file_score") or 0.0),
            ast_input_source=str(payload.get("ast_input_source") or ""),
            parse_status=str(payload.get("parse_status") or "ok"),
        )


@dataclass(frozen=True, slots=True)
class SymbolParseResult:
    """One file's symbols plus an explicit parse outcome."""

    repo: str
    base_commit: str
    file_path: str
    language: str
    status: str
    symbols: tuple[SymbolRecordV1, ...] = ()
    error_type: str = ""
    message: str = ""
    error_line: int | None = None
    error_column: int | None = None
    schema_version: str = SYMBOL_PARSE_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo", self.repo.strip())
        object.__setattr__(self, "base_commit", self.base_commit.strip())
        if self.status == "invalid_path":
            if str(self.file_path or "").strip():
                raise ValueError("An invalid_path result must not serialize the unsafe path.")
            object.__setattr__(self, "file_path", "")
        else:
            object.__setattr__(self, "file_path", normalize_repository_path(self.file_path))
        object.__setattr__(self, "language", self.language.strip().casefold())
        object.__setattr__(self, "symbols", tuple(self.symbols))
        if self.schema_version != SYMBOL_PARSE_RESULT_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SYMBOL_PARSE_RESULT_SCHEMA_VERSION!r}."
            )
        if not self.repo:
            raise ValueError("repo must be non-empty.")
        if not self.base_commit:
            raise ValueError("base_commit must be non-empty.")
        if not self.language:
            raise ValueError("language must be non-empty.")
        if self.status not in PARSE_STATUSES:
            raise ValueError(f"Unsupported parse status: {self.status!r}.")
        if self.status == "ok" and not self.symbols:
            raise ValueError("An 'ok' parse result must contain at least one symbol.")
        if self.status != "ok" and self.symbols:
            raise ValueError("Only an 'ok' parse result may contain symbols.")
        if self.status in {
            "syntax_error",
            "parser_error",
            "missing_file",
            "invalid_path",
            "symlink_escape",
        } and not self.error_type:
            raise ValueError("Parse and path failures must include error_type.")

    @property
    def is_success(self) -> bool:
        return self.status in {"ok", "no_symbols"}

    def to_dict(self, *, include_symbols: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "file_path": self.file_path,
            "language": self.language,
            "status": self.status,
            "symbol_count": len(self.symbols),
            "error_type": self.error_type,
            "message": self.message,
            "error_line": self.error_line,
            "error_column": self.error_column,
        }
        if include_symbols:
            payload["symbols"] = [symbol.to_dict() for symbol in self.symbols]
        return payload

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        symbols: Sequence[SymbolRecordV1] | None = None,
    ) -> "SymbolParseResult":
        parsed_symbols = (
            tuple(symbols)
            if symbols is not None
            else tuple(
                SymbolRecordV1.from_dict(row)
                for row in payload.get("symbols", [])
                if isinstance(row, dict)
            )
        )
        return cls(
            schema_version=str(
                payload.get("schema_version")
                or SYMBOL_PARSE_RESULT_SCHEMA_VERSION
            ),
            repo=str(payload.get("repo") or ""),
            base_commit=str(payload.get("base_commit") or ""),
            file_path=str(payload.get("file_path") or ""),
            language=str(payload.get("language") or ""),
            status=str(payload.get("status") or ""),
            symbols=parsed_symbols,
            error_type=str(payload.get("error_type") or ""),
            message=str(payload.get("message") or ""),
            error_line=(
                int(payload["error_line"])
                if payload.get("error_line") is not None
                else None
            ),
            error_column=(
                int(payload["error_column"])
                if payload.get("error_column") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class _RawSymbol:
    symbol_kind: str
    qualified_name: str
    display_name: str
    parent_index: int | None
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    signature: str


def extract_symbols_from_repository_file(
    repo_root: str | Path,
    file_path: str,
    *,
    repo: str,
    base_commit: str,
    language: str = "python",
    source_file_rank: int = 0,
    source_file_score: float = 0.0,
    ast_input_source: str = "stage2_retrieval_baseline",
) -> SymbolParseResult:
    """Safely read and extract one repository-relative source file.

    Path failures are returned as structured diagnostics. Invalid input is not
    copied into ``file_path``, preventing absolute paths or parent traversal
    from entering portable indexes.
    """

    normalized_language = str(language or "").strip().casefold() or "unknown"
    common = {
        "repo": repo,
        "base_commit": base_commit,
        "language": normalized_language,
    }
    try:
        normalized_path = normalize_repository_path(file_path)
    except ValueError as exc:
        return SymbolParseResult(
            **common,
            file_path="",
            status="invalid_path",
            error_type="InvalidRepositoryPath",
            message=str(exc),
        )

    root = Path(repo_root).resolve()
    candidate = root.joinpath(*PurePosixPath(normalized_path).parts)
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except ValueError:
        return SymbolParseResult(
            **common,
            file_path=normalized_path,
            status="symlink_escape",
            error_type="RepositoryBoundaryError",
            message="Resolved path escapes the repository root.",
        )
    except OSError as exc:
        return SymbolParseResult(
            **common,
            file_path=normalized_path,
            status="invalid_path",
            error_type=type(exc).__name__,
            message=str(exc),
        )

    if not resolved.exists():
        return SymbolParseResult(
            **common,
            file_path=normalized_path,
            status="missing_file",
            error_type="FileNotFoundError",
            message="Repository file does not exist.",
        )
    if not resolved.is_file():
        return SymbolParseResult(
            **common,
            file_path="",
            status="invalid_path",
            error_type="NotAFileError",
            message="Repository path is not a regular file.",
        )

    try:
        source = resolved.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return SymbolParseResult(
            **common,
            file_path=normalized_path,
            status="missing_file",
            error_type="FileNotFoundError",
            message="Repository file disappeared before it could be read.",
        )
    except OSError as exc:
        return SymbolParseResult(
            **common,
            file_path=normalized_path,
            status="parser_error",
            error_type=type(exc).__name__,
            message=str(exc),
        )

    return extract_symbols_from_source(
        source,
        repo=repo,
        base_commit=base_commit,
        file_path=normalized_path,
        language=normalized_language,
        source_file_rank=source_file_rank,
        source_file_score=source_file_score,
        ast_input_source=ast_input_source,
    )


def extract_symbols_from_source(
    source: str,
    *,
    repo: str,
    base_commit: str,
    file_path: str,
    language: str = "python",
    source_file_rank: int = 0,
    source_file_score: float = 0.0,
    ast_input_source: str = "stage2_retrieval_baseline",
) -> SymbolParseResult:
    """Extract one source file into SymbolRecordV1 records.

    v1 formally supports Python. Other languages receive an explicit
    ``unsupported_language`` result instead of an empty symbol list.
    """

    normalized_path = normalize_repository_path(file_path)
    normalized_language = str(language or "").strip().casefold()
    common = {
        "repo": repo,
        "base_commit": base_commit,
        "file_path": normalized_path,
        "language": normalized_language,
    }
    if normalized_language != "python":
        return SymbolParseResult(
            **common,
            status="unsupported_language",
            message=f"No v1 parser is registered for language {normalized_language!r}.",
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source)
    except SyntaxError as exc:
        return SymbolParseResult(
            **common,
            status="syntax_error",
            error_type=type(exc).__name__,
            message=str(exc.msg or exc),
            error_line=int(exc.lineno) if exc.lineno is not None else None,
            error_column=(max(0, int(exc.offset) - 1) if exc.offset is not None else None),
        )
    except RecursionError as exc:
        return SymbolParseResult(
            **common,
            status="parser_error",
            error_type=type(exc).__name__,
            message=str(exc) or "Python AST recursion limit was exceeded.",
        )
    except Exception as exc:  # defensive boundary around the standard-library parser
        return SymbolParseResult(
            **common,
            status="parser_error",
            error_type=type(exc).__name__,
            message=str(exc),
        )

    source_lines = source.splitlines()
    raw_symbols: list[_RawSymbol] = []
    scope_stack: list[int] = []

    class Visitor(ast.NodeVisitor):
        def _parent_index(self) -> int | None:
            return scope_stack[-1] if scope_stack else None

        def _qualified_name(self, name: str) -> str:
            if not scope_stack:
                return name
            parent = raw_symbols[scope_stack[-1]]
            separator = ".<locals>." if parent.symbol_kind in CALLABLE_SYMBOL_KINDS else "."
            return f"{parent.qualified_name}{separator}{name}"

        def _append(self, node: ast.AST, *, symbol_kind: str, name: str) -> int:
            start_line, start_column = _decorator_aware_start(node)
            raw_symbols.append(
                _RawSymbol(
                    symbol_kind=symbol_kind,
                    qualified_name=self._qualified_name(name),
                    display_name=name,
                    parent_index=self._parent_index(),
                    start_line=start_line,
                    start_column=start_column,
                    end_line=int(getattr(node, "end_lineno", getattr(node, "lineno", 1))),
                    end_column=int(
                        getattr(node, "end_col_offset", getattr(node, "col_offset", 0))
                    ),
                    signature=_signature_line(source_lines, node),
                )
            )
            return len(raw_symbols) - 1

        def visit_ClassDef(self, node: ast.ClassDef) -> Any:
            index = self._append(node, symbol_kind="class", name=node.name)
            scope_stack.append(index)
            self.generic_visit(node)
            scope_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
            self._visit_function(node, async_kind=False)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
            self._visit_function(node, async_kind=True)

        def _visit_function(
            self,
            node: ast.FunctionDef | ast.AsyncFunctionDef,
            *,
            async_kind: bool,
        ) -> None:
            parent_kind = raw_symbols[scope_stack[-1]].symbol_kind if scope_stack else ""
            is_method = parent_kind == "class"
            if is_method:
                kind = "async_method" if async_kind else "method"
            else:
                kind = "async_function" if async_kind else "function"
            index = self._append(node, symbol_kind=kind, name=node.name)
            scope_stack.append(index)
            self.generic_visit(node)
            scope_stack.pop()

    try:
        Visitor().visit(tree)
    except RecursionError as exc:
        return SymbolParseResult(
            **common,
            status="parser_error",
            error_type=type(exc).__name__,
            message=str(exc) or "Python AST traversal recursion limit was exceeded.",
        )

    if not raw_symbols:
        return SymbolParseResult(**common, status="no_symbols")

    records: list[SymbolRecordV1] = []
    for raw in raw_symbols:
        parent_symbol_id = records[raw.parent_index].symbol_id if raw.parent_index is not None else ""
        records.append(
            SymbolRecordV1.create(
                repo=repo,
                base_commit=base_commit,
                file_path=normalized_path,
                language=normalized_language,
                symbol_kind=raw.symbol_kind,
                qualified_name=raw.qualified_name,
                display_name=raw.display_name,
                parent_symbol_id=parent_symbol_id,
                start_line=raw.start_line,
                start_column=raw.start_column,
                end_line=raw.end_line,
                end_column=raw.end_column,
                signature=raw.signature,
                source_file_rank=source_file_rank,
                source_file_score=source_file_score,
                ast_input_source=ast_input_source,
            )
        )
    return SymbolParseResult(**common, status="ok", symbols=tuple(records))


def extract_python_symbols(
    source: str,
    *,
    repo: str,
    base_commit: str,
    file_path: str,
    source_file_rank: int = 0,
    source_file_score: float = 0.0,
    ast_input_source: str = "stage2_retrieval_baseline",
) -> SymbolParseResult:
    """Convenience wrapper for the formal v1 Python adapter."""

    return extract_symbols_from_source(
        source,
        repo=repo,
        base_commit=base_commit,
        file_path=file_path,
        language="python",
        source_file_rank=source_file_rank,
        source_file_score=source_file_score,
        ast_input_source=ast_input_source,
    )


def _decorator_aware_start(node: ast.AST) -> tuple[int, int]:
    line = int(getattr(node, "lineno", 1))
    column = int(getattr(node, "col_offset", 0))
    decorators: Sequence[ast.expr] = getattr(node, "decorator_list", ())
    if decorators:
        first = min(decorators, key=lambda decorator: (decorator.lineno, decorator.col_offset))
        if int(first.lineno) < line:
            return int(first.lineno), max(0, int(first.col_offset) - 1)
    return line, column


def _signature_line(source_lines: Sequence[str], node: ast.AST) -> str:
    line_number = int(getattr(node, "lineno", 0))
    if line_number <= 0 or line_number > len(source_lines):
        return ""
    source = "\n".join(source_lines[line_number - 1 :])
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        bracket_depth = 0
        saw_definition = False
        for token in tokens:
            if token.type == tokenize.NAME and token.string in {"def", "class"}:
                saw_definition = True
            elif token.type == tokenize.OP:
                if token.string in {"(", "[", "{"}:
                    bracket_depth += 1
                elif token.string in {")",
                    "]",
                    "}",
                }:
                    bracket_depth = max(0, bracket_depth - 1)
                elif token.string == ":" and saw_definition and bracket_depth == 0:
                    end_line, end_column = token.end
                    header_lines = source.splitlines(keepends=True)
                    return (
                        "".join(header_lines[: end_line - 1])
                        + header_lines[end_line - 1][:end_column]
                    ).strip()
    except (IndentationError, tokenize.TokenError):
        pass
    return source_lines[line_number - 1].strip()
