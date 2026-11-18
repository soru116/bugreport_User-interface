"""Build DRONE/REP- feature matrices for Eclipse Bugzilla priority prediction.

這支程式是整個專題的特徵工程核心：把清洗後的 bug report 轉成
`X_features.npz`、`y.npy` 和 `feature_meta.csv`。特徵包含文獻中的
textual、temporal、author、related-report、severity、product/component，
並額外加入本專題的 error-driven keyword features。
"""

import os
import argparse
import json
import re
import numpy as np
import pandas as pd
from scipy.sparse import hstack, csr_matrix, save_npz
from sklearn.feature_extraction.text import CountVectorizer, ENGLISH_STOP_WORDS, TfidfVectorizer
from sklearn.preprocessing import OneHotEncoder, StandardScaler
import joblib

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_PATH = os.path.join(PROJECT_ROOT, "data", "processed", "eclipse_bug_reports_clean.csv")
FEATURE_DIR = os.path.join(PROJECT_ROOT, "data", "processed", "features")

TEXT_VECTORIZER_PATH = os.path.join(FEATURE_DIR, "tf_vectorizer.joblib")
TFIDF_NGRAM_VECTORIZER_PATH = os.path.join(FEATURE_DIR, "tfidf_ngram_vectorizer.joblib")
REP_BIGRAM_VECTORIZER_PATH = os.path.join(FEATURE_DIR, "rep_bigram_vectorizer.joblib")
META_ENCODER_PATH = os.path.join(FEATURE_DIR, "meta_encoder.joblib")
NUMERIC_SCALER_PATH = os.path.join(FEATURE_DIR, "numeric_scaler.joblib")
X_PATH = os.path.join(FEATURE_DIR, "X_features.npz")
Y_PATH = os.path.join(FEATURE_DIR, "y.npy")
META_PATH = os.path.join(FEATURE_DIR, "feature_meta.csv")
NUMERIC_COLS = [
    "author_past_count",
    "author_past_mean_priority",
    "author_past_median_priority",
    "author_past_30d_count",
    "past_1d_count",
    "past_3d_count",
    "past_7d_count",
    "past_30d_count",
    "past_1d_same_or_higher_severity_count",
    "past_3d_same_or_higher_severity_count",
    "past_7d_same_or_higher_severity_count",
    "past_30d_same_or_higher_severity_count",
    "product_past_7d_count",
    "product_past_30d_count",
    "product_past_90d_count",
    "product_past_30d_mean_priority",
    "product_past_30d_median_priority",
    "product_past_30d_p1_ratio",
    "product_past_30d_p2_ratio",
    "product_past_30d_p3_ratio",
    "product_past_30d_p4_ratio",
    "product_past_30d_p5_ratio",
    "component_past_30d_count",
    "component_past_30d_mean_priority",
    "component_past_30d_median_priority",
    "component_past_30d_p1_ratio",
    "component_past_30d_p2_ratio",
    "component_past_30d_p3_ratio",
    "component_past_30d_p4_ratio",
    "component_past_30d_p5_ratio",
    "related_top1_priority",
    "related_top3_avg_priority",
    "related_top3_max_priority",
    "related_top3_min_priority",
    "related_similarity_top1",
    "related_similarity_top3_mean",
    "related_top3_median_priority",
    "related_top5_avg_priority",
    "related_top5_median_priority",
    "related_top5_max_priority",
    "related_top5_min_priority",
    "related_similarity_top5_mean",
    "related_top10_avg_priority",
    "related_top10_median_priority",
    "related_top10_max_priority",
    "related_top10_min_priority",
    "related_similarity_top10_mean",
    "related_top20_avg_priority",
    "related_top20_median_priority",
    "related_top20_max_priority",
    "related_top20_min_priority",
    "related_similarity_top20_mean",
]

SEVERITY_RANK = {
    "blocker": 1,
    "critical": 2,
    "major": 3,
    "normal": 4,
    "minor": 5,
    "trivial": 6,
    "enhancement": 7,
    "unknown": 8,
}
RELATED_TOP_KS = [1, 3, 5, 10, 20]
TOKEN_PATTERN = r"(?u)\b[a-zA-Z][a-zA-Z0-9_]{1,}\b"
TOKEN_RE = re.compile(TOKEN_PATTERN)


def ensure_numeric_col(column: str) -> None:
    if column not in NUMERIC_COLS:
        NUMERIC_COLS.append(column)


def simple_stem(token: str) -> str:
    """
    Lightweight stemming to keep the project self-contained without adding NLTK.
    It is intentionally conservative: enough to merge common English bug-report variants.
    """
    for suffix in ("ization", "ational", "fulness", "ousness", "iveness", "tional", "ments", "ment", "ingly", "edly", "ing", "edly", "ed", "ies", "s"):
        if token.endswith(suffix) and len(token) > len(suffix) + 3:
            if suffix == "ies":
                return token[: -len(suffix)] + "y"
            return token[:-1] if suffix == "s" else token[: -len(suffix)]
    return token


def drone_text_analyzer(text: str) -> list[str]:
    tokens = TOKEN_RE.findall(text)
    return [
        simple_stem(token.lower())
        for token in tokens
        if token.lower() not in ENGLISH_STOP_WORDS
    ]


def drone_bigram_analyzer(text: str) -> list[str]:
    tokens = drone_text_analyzer(text)
    return [f"{tokens[i]}_{tokens[i + 1]}" for i in range(len(tokens) - 1)]


def weighted_tfidf(matrix, weight: float):
    return matrix.multiply(weight) if weight != 1.0 else matrix


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def build_past_window_priority_stats(prev_priorities: list[int]) -> dict[str, float]:
    if not prev_priorities:
        return {
            "count": 0.0,
            "mean": 0.0,
            "median": 0.0,
            "p1_ratio": 0.0,
            "p2_ratio": 0.0,
            "p3_ratio": 0.0,
            "p4_ratio": 0.0,
            "p5_ratio": 0.0,
        }

    arr = np.array(prev_priorities, dtype=float)
    total = float(len(arr))
    return {
        "count": total,
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        # priority_num follows the Ticket labels directly:
        # P1(Blocker)=1, P2=2, ..., P5(Trivial)=5.
        "p1_ratio": float(np.sum(arr == 1) / total),
        "p2_ratio": float(np.sum(arr == 2) / total),
        "p3_ratio": float(np.sum(arr == 3) / total),
        "p4_ratio": float(np.sum(arr == 4) / total),
        "p5_ratio": float(np.sum(arr == 5) / total),
    }


def empty_related_stats() -> dict[str, float]:
    row = {
        "related_top1_priority": 0.0,
        "related_similarity_top1": 0.0,
        "related_top1_same_product": 0.0,
        "related_top1_same_component": 0.0,
        "related_top1_same_severity": 0.0,
        "related_top1_is_p1": 0.0,
        "related_top1_is_p2": 0.0,
        "related_top1_is_p3": 0.0,
        "related_top1_is_p4": 0.0,
        "related_top1_is_p5": 0.0,
    }
    for column in row:
        ensure_numeric_col(column)
    for top_k in RELATED_TOP_KS:
        if top_k == 1:
            continue
        topk_values = {
            f"related_top{top_k}_avg_priority": 0.0,
            f"related_top{top_k}_median_priority": 0.0,
            f"related_top{top_k}_max_priority": 0.0,
            f"related_top{top_k}_min_priority": 0.0,
            f"related_similarity_top{top_k}_mean": 0.0,
            f"related_top{top_k}_same_product_rate": 0.0,
            f"related_top{top_k}_same_component_rate": 0.0,
            f"related_top{top_k}_same_severity_rate": 0.0,
            f"related_top{top_k}_p1_rate": 0.0,
            f"related_top{top_k}_p2_rate": 0.0,
            f"related_top{top_k}_p3_rate": 0.0,
            f"related_top{top_k}_p4_rate": 0.0,
            f"related_top{top_k}_p5_rate": 0.0,
            f"related_top{top_k}_high_priority_rate": 0.0,
            f"related_top{top_k}_low_priority_rate": 0.0,
        }
        row |= topk_values
        for column in topk_values:
            ensure_numeric_col(column)
    return row


def _eligible_history_mask(df: pd.DataFrame, exclude_targets_from_history: bool) -> pd.Series:
    """Rows whose labels may be used as historical features for another row."""

    if exclude_targets_from_history and "__is_target" in df.columns:
        return ~df["__is_target"].astype(bool)
    return pd.Series(True, index=df.index)


def add_same_severity_temporal_features(
    df: pd.DataFrame,
    exclude_targets_from_history: bool = False,
) -> pd.DataFrame:
    """Literature TMP2/TMP5/TMP8/TMP11: same-severity counts in 7/30/1/3 day windows."""
    df = df.sort_values("creation_time").copy()
    windows = [1, 3, 7, 30]
    values = {f"past_{days}d_same_severity_count": [] for days in windows}

    eligible_history = _eligible_history_mask(df, exclude_targets_from_history)
    for _, row in df.iterrows():
        current_time = row["creation_time"]
        severity = str(row["severity"]).lower()
        earlier_mask = (df["creation_time"] < current_time) & eligible_history
        same_severity_mask = earlier_mask & (df["severity"].astype(str).str.lower() == severity)
        for days in windows:
            window_mask = same_severity_mask & (df["creation_time"] >= current_time - pd.Timedelta(days=days))
            values[f"past_{days}d_same_severity_count"].append(int(window_mask.sum()))

    for col, col_values in values.items():
        df[col] = col_values
        if col not in NUMERIC_COLS:
            NUMERIC_COLS.append(col)
    return df


def add_literature_product_features(
    df: pd.DataFrame,
    exclude_targets_from_history: bool = False,
) -> pd.DataFrame:
    """
    Literature PRO2-11 / PRO13-22: product/component stats over all prior reports,
    not just a 30-day window.
    """
    df = df.sort_values("creation_time").copy()
    field_specs = [("product", "product_past_all"), ("component", "component_past_all")]
    new_cols: dict[str, list[float]] = {}

    for _, prefix in field_specs:
        for suffix in [
            "count",
            "same_severity_count",
            "same_or_higher_severity_count",
            "p1_ratio",
            "p2_ratio",
            "p3_ratio",
            "p4_ratio",
            "p5_ratio",
            "mean_priority",
            "median_priority",
        ]:
            new_cols[f"{prefix}_{suffix}"] = []

    severity_series = df["severity"].astype(str).str.lower().map(SEVERITY_RANK).fillna(SEVERITY_RANK["unknown"])

    eligible_history = _eligible_history_mask(df, exclude_targets_from_history)
    for _, row in df.iterrows():
        current_time = row["creation_time"]
        current_severity_rank = SEVERITY_RANK.get(str(row["severity"]).lower(), SEVERITY_RANK["unknown"])
        earlier_mask = (df["creation_time"] < current_time) & eligible_history

        for field, prefix in field_specs:
            field_mask = earlier_mask & (df[field] == row[field])
            same_severity_mask = field_mask & (df["severity"].astype(str).str.lower() == str(row["severity"]).lower())
            same_or_higher_mask = field_mask & (severity_series <= current_severity_rank)
            stats = build_past_window_priority_stats(df.loc[field_mask, "priority_num"].tolist())

            new_cols[f"{prefix}_count"].append(float(field_mask.sum()))
            new_cols[f"{prefix}_same_severity_count"].append(float(same_severity_mask.sum()))
            new_cols[f"{prefix}_same_or_higher_severity_count"].append(float(same_or_higher_mask.sum()))
            new_cols[f"{prefix}_p1_ratio"].append(stats["p1_ratio"])
            new_cols[f"{prefix}_p2_ratio"].append(stats["p2_ratio"])
            new_cols[f"{prefix}_p3_ratio"].append(stats["p3_ratio"])
            new_cols[f"{prefix}_p4_ratio"].append(stats["p4_ratio"])
            new_cols[f"{prefix}_p5_ratio"].append(stats["p5_ratio"])
            new_cols[f"{prefix}_mean_priority"].append(stats["mean"])
            new_cols[f"{prefix}_median_priority"].append(stats["median"])

    for col, values in new_cols.items():
        df[col] = values
        if col not in NUMERIC_COLS:
            NUMERIC_COLS.append(col)
    return df


def add_error_driven_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Small binary/count features from P1/P2/P3 boundary inspection.
    They are intentionally transparent so they can be discussed in the report.
    """
    df = df.copy()
    summary = df["summary"].fillna("").astype(str).str.lower()
    description = df["description"].fillna("").astype(str).str.lower()
    text = df["text"].fillna("").astype(str).str.lower()

    pattern_specs = {
        "has_crash_terms": r"\b(?:crash|crashes|crashed|segfault|segmentation fault|fatal|abort|aborted|core dump)\b",
        "has_regression_terms": r"\b(?:regression|regressed|worked before|no longer works|used to work|previously worked|since update)\b",
        "has_data_loss_terms": r"\b(?:data loss|lost data|loses data|corrupt|corrupted|delete[sd]? files?|overwrite[sd]?|cannot recover)\b",
        "has_blocking_terms": r"\b(?:blocker|blocking|blocks?|cannot proceed|unusable|showstopper|critical|must fix)\b",
        "has_api_break_terms": r"\b(?:api break|breaking api|binary incompat|source incompat|incompatible api|api change)\b",
        "has_stack_trace": r"\b(?:exception|traceback|stack trace|at org\.|java\.lang\.|nullpointerexception|caused by)\b",
        "has_exception_terms": r"\b(?:exception|npe|null pointer|assertion|assert failed|abortcompilation|outofmemory|oom)\b",
        "has_freeze_hang_terms": r"\b(?:freeze|freezes|hang|hangs|hung|deadlock|not responding|locks? up)\b",
        "has_performance_terms": r"\b(?:slow|performance|timeout|times out|takes too long|memory leak|cpu|high memory)\b",
        "has_install_update_terms": r"\b(?:install|installation|update|upgrade|migration|startup|launch|fails to start)\b",
        "has_security_terms": r"\b(?:security|vulnerability|exploit|permission|privilege|authentication|authorization)\b",
        "has_build_break_terms": r"\b(?:build break|breaks build|compile error|compilation error|cannot compile|build failed)\b",
        "has_test_failure_terms": r"\b(?:test failure|unit test|failing test|tests fail|junit)\b",
        "has_ui_block_terms": r"\b(?:dialog cannot close|button disabled|cannot save|cannot open|wizard fails)\b",
        "has_startup_launch_terms": r"\b(?:startup|launch|workspace startup|fails to start|cannot start|start up|initialization failed)\b",
        "has_internal_error_terms": r"\b(?:internal error|logged error|error log|\.log|workspace log|problems occurred when invoking code)\b",
        "has_widget_disposed_terms": r"\b(?:widget is disposed|disposed widget|swt\.swtexception|invalid thread access)\b",
        "has_breakpoint_terms": r"\b(?:breakpoint|breakpoints|watchpoint|suspend event|resume event|stack frame|invalidstackframe)\b",
        "has_preferences_terms": r"\b(?:preferences|preference page|installed jre|settings page|properties page)\b",
        "has_update_manager_terms": r"\b(?:update manager|feature update|installing feature|update site|feature\.xml|hand-rolled feature)\b",
        "has_repository_terms": r"\b(?:repository|cvs|svn|git|checkout|check out|commit|merge|load project from repository)\b",
        "has_save_cancel_terms": r"\b(?:save could not be completed|save problems|cannot save|cancel does not work|overwrite|copying files)\b",
        "has_search_refactor_terms": r"\b(?:search result|search view|refactor|refactoring|quick fix|content assist|open declaration)\b",
        "has_workspace_project_terms": r"\b(?:workspace|project import|import project|project metadata|classpath|build path|workspace corrupt)\b",
        "has_editor_terms": r"\b(?:editor|source editor|text editor|compare editor|content assist|quick fix|refactoring)\b",
        "has_debug_terms": r"\b(?:debug|debugger|breakpoint|watchpoint|step into|step over|launch configuration)\b",
        "has_compiler_terms": r"\b(?:compiler|incremental builder|compile|javac|syntax error|type resolution)\b",
        "has_pde_osgi_terms": r"\b(?:plugin|plug-in|osgi|bundle|manifest|feature.xml|extension point)\b",
        "has_git_team_terms": r"\b(?:git|egit|cvs|svn|repository|merge conflict|checkout|commit)\b",
        "has_repro_steps_terms": r"\b(?:steps to reproduce|reproduce|reproducible|expected result|actual result|how to reproduce)\b",
        "has_workaround_terms": r"\b(?:workaround|work around|temporary fix|can avoid|manual fix)\b",
        "has_enhancement_request_terms": r"\b(?:enhancement|feature request|nice to have|wish|would like|request for)\b",
        "has_minor_visual_terms": r"\b(?:typo|cosmetic|spelling|label|icon|color|formatting|alignment)\b",
        "has_file_line_stack_terms": r"\b[a-zA-Z0-9_.$/-]+\.(?:java|xml|class|properties):[0-9]+\b",
    }

    feature_values = {
        "summary_char_len": summary.str.len().astype(float),
        "description_char_len": description.str.len().astype(float),
        "summary_word_count": summary.str.split().str.len().fillna(0).astype(float),
        "description_word_count": description.str.split().str.len().fillna(0).astype(float),
        "text_word_count": text.str.split().str.len().fillna(0).astype(float),
        "severity_rank_numeric": df["severity"].astype(str).str.lower().map(SEVERITY_RANK).fillna(SEVERITY_RANK["unknown"]).astype(float),
    }

    for name, pattern in pattern_specs.items():
        feature_values[name] = text.str.contains(pattern, regex=True).astype(float)
        feature_values[f"summary_{name}"] = summary.str.contains(pattern, regex=True).astype(float)
        feature_values[f"{name}_count"] = text.str.count(pattern).astype(float)

    high_signal_names = [
        "has_crash_terms",
        "has_regression_terms",
        "has_data_loss_terms",
        "has_blocking_terms",
        "has_api_break_terms",
        "has_stack_trace",
        "has_exception_terms",
        "has_freeze_hang_terms",
        "has_security_terms",
        "has_build_break_terms",
        "has_startup_launch_terms",
    ]
    p2_boundary_names = [
        "has_regression_terms",
        "has_stack_trace",
        "has_exception_terms",
        "has_build_break_terms",
        "has_editor_terms",
        "has_debug_terms",
        "has_compiler_terms",
        "has_repro_steps_terms",
        "has_internal_error_terms",
        "has_widget_disposed_terms",
        "has_breakpoint_terms",
        "has_preferences_terms",
        "has_update_manager_terms",
        "has_repository_terms",
        "has_save_cancel_terms",
        "has_search_refactor_terms",
    ]
    low_signal_names = [
        "has_workaround_terms",
        "has_enhancement_request_terms",
        "has_minor_visual_terms",
        "has_test_failure_terms",
    ]

    feature_values["high_priority_signal_count"] = sum(feature_values[name] for name in high_signal_names)
    feature_values["summary_high_priority_signal_count"] = sum(feature_values[f"summary_{name}"] for name in high_signal_names)
    feature_values["p2_boundary_signal_count"] = sum(feature_values[name] for name in p2_boundary_names)
    feature_values["summary_p2_boundary_signal_count"] = sum(feature_values[f"summary_{name}"] for name in p2_boundary_names)
    feature_values["low_priority_signal_count"] = sum(feature_values[name] for name in low_signal_names)
    feature_values["priority_signal_balance"] = (
        feature_values["high_priority_signal_count"] - feature_values["low_priority_signal_count"]
    )
    feature_values["description_line_count"] = description.str.count(r"\n").astype(float) + 1.0
    feature_values["stack_trace_line_count"] = description.str.count(
        r"(?:\bat\s+[a-zA-Z0-9_.$]+\(|\.java:[0-9]+|caused by:|exception)"
    ).astype(float)
    feature_values["summary_exclamation_count"] = summary.str.count("!").astype(float)
    feature_values["contains_build_id"] = text.str.contains(r"\b(?:build id|buildid|i20[0-9]{6,})\b", regex=True).astype(float)
    feature_values["contains_version_number"] = text.str.contains(r"\b[0-9]+\.[0-9]+(?:\.[0-9]+)?\b", regex=True).astype(float)

    product = df["product"].fillna("").astype(str).str.lower()
    component = df["component"].fillna("").astype(str).str.lower()
    severity = df["severity"].fillna("").astype(str).str.lower()
    feature_values["product_is_platform"] = product.str.contains(r"\bplatform\b", regex=True).astype(float)
    feature_values["product_is_jdt"] = product.str.contains(r"\bjdt\b|java development tools", regex=True).astype(float)
    feature_values["product_is_pde"] = product.str.contains(r"\bpde\b|plug-in development", regex=True).astype(float)
    feature_values["product_is_equinox"] = product.str.contains(r"\bequinox\b", regex=True).astype(float)
    feature_values["component_is_ui"] = component.str.contains(r"\bui\b|user interface", regex=True).astype(float)
    feature_values["component_is_debug"] = component.str.contains(r"\bdebug\b", regex=True).astype(float)
    feature_values["component_is_core"] = component.str.contains(r"\bcore\b", regex=True).astype(float)
    feature_values["component_is_compiler"] = component.str.contains(r"\bcompiler\b", regex=True).astype(float)
    feature_values["component_is_text"] = component.str.contains(r"\btext\b|editor", regex=True).astype(float)
    feature_values["product_focus_platform_or_jdt"] = (
        feature_values["product_is_platform"] + feature_values["product_is_jdt"]
    ).clip(upper=1.0)
    feature_values["component_focus_ui_debug_core"] = (
        feature_values["component_is_ui"] + feature_values["component_is_debug"] + feature_values["component_is_core"]
    ).clip(upper=1.0)
    feature_values["severity_is_normal"] = (severity == "normal").astype(float)
    feature_values["severity_is_enhancement"] = (severity == "enhancement").astype(float)
    feature_values["severity_is_minor_or_enhancement"] = severity.isin(["minor", "enhancement"]).astype(float)
    feature_values["severity_is_major_or_higher"] = severity.isin(["blocker", "critical", "major"]).astype(float)

    feature_values["normal_with_stack_exception"] = (
        feature_values["severity_is_normal"] * feature_values["has_stack_trace"]
    )
    feature_values["normal_with_internal_error"] = (
        feature_values["severity_is_normal"] * feature_values["has_internal_error_terms"]
    )
    feature_values["normal_with_ui_debug_core"] = (
        feature_values["severity_is_normal"] * feature_values["component_focus_ui_debug_core"]
    )
    feature_values["jdt_debug_breakpoint_signal"] = (
        feature_values["product_is_jdt"] * feature_values["component_is_debug"] * feature_values["has_breakpoint_terms"]
    )
    feature_values["platform_ui_internal_error_signal"] = (
        feature_values["product_is_platform"] * feature_values["component_is_ui"] * feature_values["has_internal_error_terms"]
    )
    feature_values["update_install_normal_signal"] = (
        feature_values["severity_is_normal"] * feature_values["has_update_manager_terms"]
    )
    feature_values["stack_without_crash_data_loss"] = (
        feature_values["has_stack_trace"]
        * (1.0 - feature_values["has_crash_terms"].clip(upper=1.0))
        * (1.0 - feature_values["has_data_loss_terms"].clip(upper=1.0))
    )
    feature_values["p2_soft_boundary_signal_count"] = (
        feature_values["normal_with_stack_exception"]
        + feature_values["normal_with_internal_error"]
        + feature_values["normal_with_ui_debug_core"]
        + feature_values["jdt_debug_breakpoint_signal"]
        + feature_values["platform_ui_internal_error_signal"]
        + feature_values["update_install_normal_signal"]
        + feature_values["stack_without_crash_data_loss"]
    )

    feature_df = pd.DataFrame(feature_values, index=df.index)
    for col in feature_df.columns:
        ensure_numeric_col(col)
    return pd.concat([df, feature_df], axis=1)


def compute_topk_related_stats(
    prev_priorities: np.ndarray,
    prev_sims: np.ndarray,
    same_product: np.ndarray | None = None,
    same_component: np.ndarray | None = None,
    same_severity: np.ndarray | None = None,
) -> dict[str, float]:
    if len(prev_priorities) == 0 or len(prev_sims) == 0:
        return empty_related_stats()

    if same_product is None:
        same_product = np.zeros(len(prev_priorities), dtype=float)
    if same_component is None:
        same_component = np.zeros(len(prev_priorities), dtype=float)
    if same_severity is None:
        same_severity = np.zeros(len(prev_priorities), dtype=float)

    sorted_idx = np.argsort(prev_sims)[::-1]
    best_idx = sorted_idx[0]
    row = {
        "related_top1_priority": float(prev_priorities[best_idx]),
        "related_similarity_top1": float(prev_sims[best_idx]),
        "related_top1_same_product": float(same_product[best_idx]),
        "related_top1_same_component": float(same_component[best_idx]),
        "related_top1_same_severity": float(same_severity[best_idx]),
        "related_top1_is_p1": float(prev_priorities[best_idx] == 1),
        "related_top1_is_p2": float(prev_priorities[best_idx] == 2),
        "related_top1_is_p3": float(prev_priorities[best_idx] == 3),
        "related_top1_is_p4": float(prev_priorities[best_idx] == 4),
        "related_top1_is_p5": float(prev_priorities[best_idx] == 5),
    }
    for column in row:
        ensure_numeric_col(column)

    for top_k in RELATED_TOP_KS:
        if top_k == 1:
            continue
        k = min(top_k, len(prev_priorities))
        top_idx = sorted_idx[:k]
        top_priorities = prev_priorities[top_idx]
        top_sims = prev_sims[top_idx]
        topk_values = {
            f"related_top{top_k}_avg_priority": float(np.mean(top_priorities)),
            f"related_top{top_k}_median_priority": float(np.median(top_priorities)),
            f"related_top{top_k}_max_priority": float(np.max(top_priorities)),
            f"related_top{top_k}_min_priority": float(np.min(top_priorities)),
            f"related_similarity_top{top_k}_mean": float(np.mean(top_sims)),
            f"related_top{top_k}_same_product_rate": float(np.mean(same_product[top_idx])),
            f"related_top{top_k}_same_component_rate": float(np.mean(same_component[top_idx])),
            f"related_top{top_k}_same_severity_rate": float(np.mean(same_severity[top_idx])),
            f"related_top{top_k}_p1_rate": float(np.mean(top_priorities == 1)),
            f"related_top{top_k}_p2_rate": float(np.mean(top_priorities == 2)),
            f"related_top{top_k}_p3_rate": float(np.mean(top_priorities == 3)),
            f"related_top{top_k}_p4_rate": float(np.mean(top_priorities == 4)),
            f"related_top{top_k}_p5_rate": float(np.mean(top_priorities == 5)),
            f"related_top{top_k}_high_priority_rate": float(np.mean(np.isin(top_priorities, [1, 2]))),
            f"related_top{top_k}_low_priority_rate": float(np.mean(np.isin(top_priorities, [4, 5]))),
        }
        row |= topk_values
        for column in topk_values:
            ensure_numeric_col(column)

    return row


def bm25_document_matrix(X_counts, k1: float = 1.5, b: float = 0.75):
    """
    REP- in the DRONE paper is BM25F-like. Bugzilla data here mostly has one text
    field, so we use a BM25 document weighting as a transparent REP-like proxy.
    """
    X = X_counts.tocsr().astype(float)
    n_docs, _ = X.shape
    doc_lengths = np.asarray(X.sum(axis=1)).ravel()
    avg_doc_len = float(doc_lengths.mean()) if len(doc_lengths) else 1.0
    avg_doc_len = max(avg_doc_len, 1e-9)

    X_csc = X.tocsc()
    df = np.diff(X_csc.indptr)
    idf = np.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)

    for row_idx in range(n_docs):
        start, end = X.indptr[row_idx], X.indptr[row_idx + 1]
        if start == end:
            continue
        cols = X.indices[start:end]
        tf = X.data[start:end]
        norm = k1 * (1.0 - b + b * doc_lengths[row_idx] / avg_doc_len)
        X.data[start:end] = idf[cols] * (tf * (k1 + 1.0)) / (tf + norm)

    return X


def bm25f_ext_matrices(
    summary_counts,
    description_counts,
    summary_weight: float,
    description_weight: float,
    summary_b: float,
    description_b: float,
    k1: float,
    k3: float,
):
    """
    BM25Fext from REP: document-side BM25F field normalization plus query-side
    term-frequency weighting for long structured bug-report queries.
    """
    X_summary = summary_counts.tocsr().astype(float)
    X_description = description_counts.tocsr().astype(float)
    n_docs, _ = X_summary.shape

    summary_lengths = np.asarray(X_summary.sum(axis=1)).ravel()
    description_lengths = np.asarray(X_description.sum(axis=1)).ravel()
    avg_summary_len = max(float(summary_lengths.mean()) if n_docs else 0.0, 1e-9)
    avg_description_len = max(float(description_lengths.mean()) if n_docs else 0.0, 1e-9)

    summary_norm = 1.0 - summary_b + summary_b * summary_lengths / avg_summary_len
    description_norm = 1.0 - description_b + description_b * description_lengths / avg_description_len
    summary_norm = np.maximum(summary_norm, 1e-9)
    description_norm = np.maximum(description_norm, 1e-9)

    document_tfd = (
        X_summary.multiply(summary_weight / summary_norm[:, None])
        + X_description.multiply(description_weight / description_norm[:, None])
    ).tocsr()

    query_tfq = (summary_weight * X_summary + description_weight * X_description).tocsr()

    document_presence = ((X_summary + X_description) > 0).tocsc()
    df = np.diff(document_presence.indptr)
    idf = np.log((n_docs + 1.0) / (df + 1.0))

    document_weights = document_tfd.copy()
    if document_weights.nnz:
        document_weights.data = document_weights.data / (k1 + document_weights.data)
        document_weights = document_weights.multiply(idf).tocsr()

    query_weights = query_tfq.copy()
    if query_weights.nnz:
        query_weights.data = ((k3 + 1.0) * query_weights.data) / (k3 + query_weights.data)

    return document_weights.tocsr(), query_weights.tocsr()


def build_enhanced_tfidf_vectorizers() -> dict[str, TfidfVectorizer]:
    """建立 enhanced text mode 所需的 word/char TF-IDF vectorizers."""
    return {
        "combined_word": TfidfVectorizer(
            max_features=5000,
            ngram_range=(1, 2),
            token_pattern=TOKEN_PATTERN,
            stop_words="english",
            sublinear_tf=True,
            min_df=2,
        ),
        "summary_word": TfidfVectorizer(
            max_features=2500,
            ngram_range=(1, 2),
            token_pattern=TOKEN_PATTERN,
            stop_words="english",
            sublinear_tf=True,
            min_df=2,
        ),
        "description_word": TfidfVectorizer(
            max_features=4000,
            ngram_range=(1, 2),
            token_pattern=TOKEN_PATTERN,
            stop_words="english",
            sublinear_tf=True,
            min_df=2,
        ),
        "char": TfidfVectorizer(
            max_features=3000,
            analyzer="char_wb",
            ngram_range=(3, 5),
            sublinear_tf=True,
            min_df=2,
        ),
    }


def enhanced_text_matrix(
    X_tf,
    texts: list[str],
    summaries: list[str],
    descriptions: list[str],
    tfidf_ngram_vectorizer: dict[str, TfidfVectorizer] | TfidfVectorizer | None,
):
    """Fit/transform enhanced textual features and append them to paper-style TF."""
    if tfidf_ngram_vectorizer is None:
        tfidf_ngram_vectorizer = build_enhanced_tfidf_vectorizers()
        X_combined_word = tfidf_ngram_vectorizer["combined_word"].fit_transform(texts)
        X_summary_word = tfidf_ngram_vectorizer["summary_word"].fit_transform(summaries)
        X_description_word = tfidf_ngram_vectorizer["description_word"].fit_transform(descriptions)
        X_char = tfidf_ngram_vectorizer["char"].fit_transform(texts)
    elif isinstance(tfidf_ngram_vectorizer, dict):
        X_combined_word = tfidf_ngram_vectorizer["combined_word"].transform(texts)
        X_summary_word = tfidf_ngram_vectorizer["summary_word"].transform(summaries)
        X_description_word = tfidf_ngram_vectorizer["description_word"].transform(descriptions)
        X_char = tfidf_ngram_vectorizer["char"].transform(texts)
    else:
        # Backward compatibility for older enhanced feature directories.
        X_combined_word = tfidf_ngram_vectorizer.transform(texts)
        X_summary_word = csr_matrix((len(texts), 0))
        X_description_word = csr_matrix((len(texts), 0))
        X_char = csr_matrix((len(texts), 0))

    X_text = hstack([
        X_tf,
        X_combined_word,
        weighted_tfidf(X_summary_word, 1.5),
        weighted_tfidf(X_description_word, 0.8),
        weighted_tfidf(X_char, 0.5),
    ]).tocsr()
    return tfidf_ngram_vectorizer, X_text


def build_rep_minus_matrices(
    vectorizer: CountVectorizer,
    rep_bigram_vectorizer: CountVectorizer | None,
    summaries: list[str],
    descriptions: list[str],
    rep_summary_weight: float,
    rep_description_weight: float,
    rep_bigram_summary_weight: float,
    rep_bigram_description_weight: float,
    rep_k1_unigram: float,
    rep_k3_unigram: float,
    rep_summary_b_unigram: float,
    rep_description_b_unigram: float,
    rep_k1_bigram: float,
    rep_k3_bigram: float,
    rep_summary_b_bigram: float,
    rep_description_b_bigram: float,
):
    """Build REP- BM25Fext unigram/bigram matrices used for historical similarity."""
    if rep_bigram_vectorizer is None:
        rep_bigram_vectorizer = CountVectorizer(
            max_features=3000,
            analyzer=drone_bigram_analyzer,
        )
        rep_bigram_vectorizer.fit([f"{summary} {description}" for summary, description in zip(summaries, descriptions)])

    X_summary_uni = vectorizer.transform(summaries)
    X_description_uni = vectorizer.transform(descriptions)
    X_doc_uni, X_query_uni = bm25f_ext_matrices(
        X_summary_uni,
        X_description_uni,
        summary_weight=rep_summary_weight,
        description_weight=rep_description_weight,
        summary_b=rep_summary_b_unigram,
        description_b=rep_description_b_unigram,
        k1=rep_k1_unigram,
        k3=rep_k3_unigram,
    )

    X_summary_bi = rep_bigram_vectorizer.transform(summaries)
    X_description_bi = rep_bigram_vectorizer.transform(descriptions)
    X_doc_bi, X_query_bi = bm25f_ext_matrices(
        X_summary_bi,
        X_description_bi,
        summary_weight=rep_bigram_summary_weight,
        description_weight=rep_bigram_description_weight,
        summary_b=rep_summary_b_bigram,
        description_b=rep_description_b_bigram,
        k1=rep_k1_bigram,
        k3=rep_k3_bigram,
    )
    return rep_bigram_vectorizer, X_doc_uni, X_query_uni, X_doc_bi, X_query_bi


def build_related_feature_rows(
    df: pd.DataFrame,
    related_mode: str,
    products: np.ndarray,
    components: np.ndarray,
    severities: np.ndarray,
    rep_matrices: tuple | None,
    X_related,
    rep_unigram_feature_weight: float,
    rep_bigram_feature_weight: float,
    rep_product_weight: float,
    rep_component_weight: float,
    rep_severity_weight: float,
    exclude_targets_from_history: bool = False,
) -> list[dict[str, float]]:
    """Compute top-k historical related-report statistics for each target report."""
    related_feature_rows = []
    eligible_history = _eligible_history_mask(df, exclude_targets_from_history).to_numpy()
    for i in range(len(df)):
        previous_indices = np.arange(i)
        if exclude_targets_from_history:
            previous_indices = previous_indices[eligible_history[:i]]
        if len(previous_indices) == 0:
            related_feature_rows.append(empty_related_stats())
            continue

        if related_mode == "rep_minus":
            X_doc_uni, X_query_uni, X_doc_bi, X_query_bi = rep_matrices
            unigram_sims = (X_query_uni[i] @ X_doc_uni[previous_indices].T).toarray().ravel()
            bigram_sims = (X_query_bi[i] @ X_doc_bi[previous_indices].T).toarray().ravel()
            sims = (
                rep_unigram_feature_weight * unigram_sims
                + rep_bigram_feature_weight * bigram_sims
                + rep_product_weight * (products[previous_indices] == products[i])
                + rep_component_weight * (components[previous_indices] == components[i])
                + rep_severity_weight * (severities[previous_indices] == severities[i])
            )
        else:
            sims = (X_related[i] @ X_related[previous_indices].T).toarray().ravel()

        prev_priorities = df.iloc[previous_indices]["priority_num"].to_numpy()
        related_feature_rows.append(
            compute_topk_related_stats(
                prev_priorities,
                sims,
                same_product=(products[previous_indices] == products[i]).astype(float),
                same_component=(components[previous_indices] == components[i]).astype(float),
                same_severity=(severities[previous_indices] == severities[i]).astype(float),
            )
        )
    return related_feature_rows


def add_author_features(df: pd.DataFrame, exclude_targets_from_history: bool = False) -> pd.DataFrame:
    df = df.sort_values("creation_time").copy()

    past_count = []
    past_mean = []
    past_median = []

    history = {}

    eligible_history = _eligible_history_mask(df, exclude_targets_from_history)
    for index, row in df.iterrows():
        author = row["creator"]
        prev = history.get(author, [])

        if len(prev) == 0:
            past_count.append(0)
            past_mean.append(0.0)
            past_median.append(0.0)
        else:
            past_count.append(len(prev))
            past_mean.append(float(np.mean(prev)))
            past_median.append(float(np.median(prev)))

        if eligible_history.loc[index]:
            prev.append(row["priority_num"])
            history[author] = prev

    df["author_past_count"] = past_count
    df["author_past_mean_priority"] = past_mean
    df["author_past_median_priority"] = past_median
    return df


def add_temporal_features(df: pd.DataFrame, exclude_targets_from_history: bool = False) -> pd.DataFrame:
    df = df.sort_values("creation_time").copy()

    past_1d_count = []
    past_3d_count = []
    past_7d_count = []
    past_30d_count = []
    past_1d_same_or_higher_severity_count = []
    past_3d_same_or_higher_severity_count = []
    past_7d_same_or_higher_severity_count = []
    past_30d_same_or_higher_severity_count = []

    product_past_7d_count = []
    product_past_30d_count = []
    product_past_90d_count = []
    author_past_30d_count = []

    product_past_30d_mean_priority = []
    product_past_30d_median_priority = []
    product_past_30d_p1_ratio = []
    product_past_30d_p2_ratio = []
    product_past_30d_p3_ratio = []
    product_past_30d_p4_ratio = []
    product_past_30d_p5_ratio = []

    component_past_30d_count = []
    component_past_30d_mean_priority = []
    component_past_30d_median_priority = []
    component_past_30d_p1_ratio = []
    component_past_30d_p2_ratio = []
    component_past_30d_p3_ratio = []
    component_past_30d_p4_ratio = []
    component_past_30d_p5_ratio = []

    eligible_history = _eligible_history_mask(df, exclude_targets_from_history)
    for _, row in df.iterrows():
        current_time = row["creation_time"]
        product = row["product"]
        component = row["component"]
        author = row["creator"]
        current_severity_rank = SEVERITY_RANK.get(str(row["severity"]).lower(), SEVERITY_RANK["unknown"])

        earlier_mask = (df["creation_time"] < current_time) & eligible_history
        same_or_higher_severity_mask = earlier_mask & (
            df["severity"].astype(str).str.lower().map(SEVERITY_RANK).fillna(SEVERITY_RANK["unknown"]) <= current_severity_rank
        )

        global_1d_mask = earlier_mask & (df["creation_time"] >= current_time - pd.Timedelta(days=1))
        global_3d_mask = earlier_mask & (df["creation_time"] >= current_time - pd.Timedelta(days=3))
        global_7d_mask = earlier_mask & (df["creation_time"] >= current_time - pd.Timedelta(days=7))
        global_30d_mask = earlier_mask & (df["creation_time"] >= current_time - pd.Timedelta(days=30))

        past_1d_count.append(int(global_1d_mask.sum()))
        past_3d_count.append(int(global_3d_mask.sum()))
        past_7d_count.append(int(global_7d_mask.sum()))
        past_30d_count.append(int(global_30d_mask.sum()))
        past_1d_same_or_higher_severity_count.append(int((global_1d_mask & same_or_higher_severity_mask).sum()))
        past_3d_same_or_higher_severity_count.append(int((global_3d_mask & same_or_higher_severity_mask).sum()))
        past_7d_same_or_higher_severity_count.append(int((global_7d_mask & same_or_higher_severity_mask).sum()))
        past_30d_same_or_higher_severity_count.append(int((global_30d_mask & same_or_higher_severity_mask).sum()))

        product_mask = earlier_mask & (df["product"] == product)
        component_mask = earlier_mask & (df["component"] == component)
        author_mask = earlier_mask & (df["creator"] == author)

        product_7d_mask = product_mask & (df["creation_time"] >= current_time - pd.Timedelta(days=7))
        product_30d_mask = product_mask & (df["creation_time"] >= current_time - pd.Timedelta(days=30))
        product_90d_mask = product_mask & (df["creation_time"] >= current_time - pd.Timedelta(days=90))
        author_30d_mask = author_mask & (df["creation_time"] >= current_time - pd.Timedelta(days=30))
        component_30d_mask = component_mask & (df["creation_time"] >= current_time - pd.Timedelta(days=30))

        product_past_7d_count.append(int(product_7d_mask.sum()))
        product_past_30d_count.append(int(product_30d_mask.sum()))
        product_past_90d_count.append(int(product_90d_mask.sum()))
        author_past_30d_count.append(int(author_30d_mask.sum()))

        product_stats = build_past_window_priority_stats(df.loc[product_30d_mask, "priority_num"].tolist())
        product_past_30d_mean_priority.append(product_stats["mean"])
        product_past_30d_median_priority.append(product_stats["median"])
        product_past_30d_p1_ratio.append(product_stats["p1_ratio"])
        product_past_30d_p2_ratio.append(product_stats["p2_ratio"])
        product_past_30d_p3_ratio.append(product_stats["p3_ratio"])
        product_past_30d_p4_ratio.append(product_stats["p4_ratio"])
        product_past_30d_p5_ratio.append(product_stats["p5_ratio"])

        component_stats = build_past_window_priority_stats(df.loc[component_30d_mask, "priority_num"].tolist())
        component_past_30d_count.append(component_stats["count"])
        component_past_30d_mean_priority.append(component_stats["mean"])
        component_past_30d_median_priority.append(component_stats["median"])
        component_past_30d_p1_ratio.append(component_stats["p1_ratio"])
        component_past_30d_p2_ratio.append(component_stats["p2_ratio"])
        component_past_30d_p3_ratio.append(component_stats["p3_ratio"])
        component_past_30d_p4_ratio.append(component_stats["p4_ratio"])
        component_past_30d_p5_ratio.append(component_stats["p5_ratio"])

    df["past_1d_count"] = past_1d_count
    df["past_3d_count"] = past_3d_count
    df["past_7d_count"] = past_7d_count
    df["past_30d_count"] = past_30d_count
    df["past_1d_same_or_higher_severity_count"] = past_1d_same_or_higher_severity_count
    df["past_3d_same_or_higher_severity_count"] = past_3d_same_or_higher_severity_count
    df["past_7d_same_or_higher_severity_count"] = past_7d_same_or_higher_severity_count
    df["past_30d_same_or_higher_severity_count"] = past_30d_same_or_higher_severity_count

    df["product_past_7d_count"] = product_past_7d_count
    df["product_past_30d_count"] = product_past_30d_count
    df["product_past_90d_count"] = product_past_90d_count
    df["author_past_30d_count"] = author_past_30d_count

    df["product_past_30d_mean_priority"] = product_past_30d_mean_priority
    df["product_past_30d_median_priority"] = product_past_30d_median_priority
    df["product_past_30d_p1_ratio"] = product_past_30d_p1_ratio
    df["product_past_30d_p2_ratio"] = product_past_30d_p2_ratio
    df["product_past_30d_p3_ratio"] = product_past_30d_p3_ratio
    df["product_past_30d_p4_ratio"] = product_past_30d_p4_ratio
    df["product_past_30d_p5_ratio"] = product_past_30d_p5_ratio

    df["component_past_30d_count"] = component_past_30d_count
    df["component_past_30d_mean_priority"] = component_past_30d_mean_priority
    df["component_past_30d_median_priority"] = component_past_30d_median_priority
    df["component_past_30d_p1_ratio"] = component_past_30d_p1_ratio
    df["component_past_30d_p2_ratio"] = component_past_30d_p2_ratio
    df["component_past_30d_p3_ratio"] = component_past_30d_p3_ratio
    df["component_past_30d_p4_ratio"] = component_past_30d_p4_ratio
    df["component_past_30d_p5_ratio"] = component_past_30d_p5_ratio

    return df


def add_related_report_features(
    df: pd.DataFrame,
    vectorizer: CountVectorizer | None = None,
    rep_bigram_vectorizer: CountVectorizer | None = None,
    tfidf_ngram_vectorizer: TfidfVectorizer | None = None,
    text_feature_mode: str = "enhanced",
    related_mode: str = "rep_minus",
    rep_summary_weight: float = 2.0,
    rep_description_weight: float = 1.0,
    rep_bigram_summary_weight: float = 2.0,
    rep_bigram_description_weight: float = 1.0,
    rep_unigram_feature_weight: float = 0.9,
    rep_bigram_feature_weight: float = 0.2,
    rep_product_weight: float = 2.0,
    rep_component_weight: float = 0.0,
    rep_severity_weight: float = 0.0,
    rep_k1_unigram: float = 1.2,
    rep_k3_unigram: float = 100.0,
    rep_summary_b_unigram: float = 0.75,
    rep_description_b_unigram: float = 0.75,
    rep_k1_bigram: float = 1.2,
    rep_k3_bigram: float = 100.0,
    rep_summary_b_bigram: float = 0.75,
    rep_description_b_bigram: float = 0.75,
    exclude_targets_from_history: bool = False,
):
    """
    DRONE related-report factor：
    1. 保留文獻風格 TF（CountVectorizer）作為主要 textual factor。
    2. enhanced mode 額外加入 TF-IDF unigram/bigram，補強 P1/P2/P3 邊界片語訊號。
    3. 每一筆 ticket 只與「歷史 ticket」比較，避免把未來資料當成相似報告。
    4. rep_minus mode 使用 REP-：BM25Fext(unigram)、BM25Fext(bigram)、product match、
       component match、severity match 的線性組合，並移除 priority/type/version 欄位。
    """
    texts = df["text"].fillna("").astype(str).tolist()
    summaries = df["summary"].fillna("").astype(str).tolist()
    descriptions = df["description"].fillna("").astype(str).tolist()
    if vectorizer is None:
        vectorizer = CountVectorizer(max_features=3000, analyzer=drone_text_analyzer)
        X_tf = vectorizer.fit_transform(texts)
    else:
        X_tf = vectorizer.transform(texts)

    if text_feature_mode == "enhanced":
        tfidf_ngram_vectorizer, X_text = enhanced_text_matrix(
            X_tf,
            texts,
            summaries,
            descriptions,
            tfidf_ngram_vectorizer,
        )
    else:
        X_text = X_tf

    if related_mode == "rep_minus":
        rep_bigram_vectorizer, X_doc_uni, X_query_uni, X_doc_bi, X_query_bi = build_rep_minus_matrices(
            vectorizer,
            rep_bigram_vectorizer,
            summaries,
            descriptions,
            rep_summary_weight,
            rep_description_weight,
            rep_bigram_summary_weight,
            rep_bigram_description_weight,
            rep_k1_unigram,
            rep_k3_unigram,
            rep_summary_b_unigram,
            rep_description_b_unigram,
            rep_k1_bigram,
            rep_k3_bigram,
            rep_summary_b_bigram,
            rep_description_b_bigram,
        )
        rep_matrices = (X_doc_uni, X_query_uni, X_doc_bi, X_query_bi)
        X_related = None
    else:
        rep_matrices = None
        X_related = bm25_document_matrix(X_tf)

    products = df["product"].astype(str).to_numpy()
    components = df["component"].astype(str).to_numpy()
    severities = df["severity"].astype(str).str.lower().to_numpy()
    related_feature_rows = build_related_feature_rows(
        df,
        related_mode,
        products,
        components,
        severities,
        rep_matrices,
        X_related,
        rep_unigram_feature_weight,
        rep_bigram_feature_weight,
        rep_product_weight,
        rep_component_weight,
        rep_severity_weight,
        exclude_targets_from_history,
    )

    related_df = pd.DataFrame(related_feature_rows, index=df.index)
    df = pd.concat([df, related_df], axis=1)

    return df, vectorizer, rep_bigram_vectorizer, tfidf_ngram_vectorizer, X_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build priority prediction feature matrices.")
    parser.add_argument("--input", default=INPUT_PATH, help="Cleaned bug report CSV input path.")
    parser.add_argument("--feature-dir", default=FEATURE_DIR, help="Output directory for X/y/meta files.")
    parser.add_argument("--mode", choices=["fit", "transform"], default="fit", help="Fit encoders or reuse fitted encoders.")
    parser.add_argument("--reference-feature-dir", default=FEATURE_DIR, help="Feature directory containing fitted encoders for transform mode.")
    parser.add_argument(
        "--history-input",
        default=None,
        help="Optional cleaned CSV used as historical tickets when transforming an evaluation set.",
    )
    parser.add_argument(
        "--exclude-targets-from-history",
        action="store_true",
        help="Do not use other transform targets or their placeholder labels as historical features.",
    )
    parser.add_argument(
        "--text-feature-mode",
        choices=["paper_tf", "enhanced"],
        default="enhanced",
        help="paper_tf uses DRONE's TF-only textual factor; enhanced also adds TF-IDF unigrams/bigrams.",
    )
    parser.add_argument(
        "--related-mode",
        choices=["bm25", "rep_minus"],
        default="rep_minus",
        help="rep_minus uses REP-: BM25Fext unigrams/bigrams plus product/component feature weights.",
    )
    parser.add_argument("--rep-summary-weight", type=float, default=2.0, help="REP- unigram summary field weight.")
    parser.add_argument("--rep-description-weight", type=float, default=1.0, help="REP- unigram description field weight.")
    parser.add_argument("--rep-bigram-summary-weight", type=float, default=2.0, help="REP- bigram summary field weight.")
    parser.add_argument("--rep-bigram-description-weight", type=float, default=1.0, help="REP- bigram description field weight.")
    parser.add_argument("--rep-unigram-feature-weight", type=float, default=0.9, help="REP- feature weight for unigram BM25Fext.")
    parser.add_argument("--rep-bigram-feature-weight", type=float, default=0.2, help="REP- feature weight for bigram BM25Fext.")
    parser.add_argument(
        "--rep-product-weight",
        "--rep-product-boost",
        dest="rep_product_weight",
        type=float,
        default=2.0,
        help="REP- feature weight for same product.",
    )
    parser.add_argument(
        "--rep-component-weight",
        "--rep-component-boost",
        dest="rep_component_weight",
        type=float,
        default=0.0,
        help="REP- feature weight for same component.",
    )
    parser.add_argument("--rep-severity-weight", type=float, default=0.0, help="REP- feature weight for same severity.")
    parser.add_argument("--rep-k1-unigram", type=float, default=1.2, help="BM25Fext k1 for unigram REP-.")
    parser.add_argument("--rep-k3-unigram", type=float, default=100.0, help="BM25Fext k3 for unigram REP-.")
    parser.add_argument("--rep-summary-b-unigram", type=float, default=0.75, help="BM25Fext summary b for unigram REP-.")
    parser.add_argument("--rep-description-b-unigram", type=float, default=0.75, help="BM25Fext description b for unigram REP-.")
    parser.add_argument("--rep-k1-bigram", type=float, default=1.2, help="BM25Fext k1 for bigram REP-.")
    parser.add_argument("--rep-k3-bigram", type=float, default=100.0, help="BM25Fext k3 for bigram REP-.")
    parser.add_argument("--rep-summary-b-bigram", type=float, default=0.75, help="BM25Fext summary b for bigram REP-.")
    parser.add_argument("--rep-description-b-bigram", type=float, default=0.75, help="BM25Fext description b for bigram REP-.")
    parser.add_argument(
        "--rep-weights-json",
        default=None,
        help="Optional JSON produced by train_rep_minus_weights.py; values override REP- weight arguments.",
    )
    return parser


def apply_rep_weight_config(args: argparse.Namespace) -> argparse.Namespace:
    if not args.rep_weights_json:
        return args

    with open(args.rep_weights_json, "r", encoding="utf-8") as fh:
        config = json.load(fh)

    key_map = {
        "summary_weight": "rep_summary_weight",
        "description_weight": "rep_description_weight",
        "bigram_summary_weight": "rep_bigram_summary_weight",
        "bigram_description_weight": "rep_bigram_description_weight",
        "unigram_feature_weight": "rep_unigram_feature_weight",
        "bigram_feature_weight": "rep_bigram_feature_weight",
        "product_weight": "rep_product_weight",
        "component_weight": "rep_component_weight",
        "severity_weight": "rep_severity_weight",
        "k1_unigram": "rep_k1_unigram",
        "k3_unigram": "rep_k3_unigram",
        "summary_b_unigram": "rep_summary_b_unigram",
        "description_b_unigram": "rep_description_b_unigram",
        "k1_bigram": "rep_k1_bigram",
        "k3_bigram": "rep_k3_bigram",
        "summary_b_bigram": "rep_summary_b_bigram",
        "description_b_bigram": "rep_description_b_bigram",
    }
    for config_key, arg_key in key_map.items():
        if config_key in config:
            setattr(args, arg_key, float(config[config_key]))
    return args


def feature_artifact_paths(args: argparse.Namespace, feature_dir: str) -> dict[str, str]:
    """Resolve output paths and reference artifact paths for fit/transform modes."""
    artifact_dir = args.reference_feature_dir if args.mode == "transform" else feature_dir
    return {
        "text_vectorizer": os.path.join(artifact_dir, "tf_vectorizer.joblib"),
        "tfidf_ngram_vectorizer": os.path.join(artifact_dir, "tfidf_ngram_vectorizer.joblib"),
        "rep_bigram_vectorizer": os.path.join(artifact_dir, "rep_bigram_vectorizer.joblib"),
        "meta_encoder": os.path.join(artifact_dir, "meta_encoder.joblib"),
        "numeric_scaler": os.path.join(artifact_dir, "numeric_scaler.joblib"),
        "x": os.path.join(feature_dir, "X_features.npz"),
        "y": os.path.join(feature_dir, "y.npy"),
        "meta": os.path.join(feature_dir, "feature_meta.csv"),
    }


def load_feature_dataframe(args: argparse.Namespace) -> pd.DataFrame:
    """Load target rows and optional history rows, then sort by creation_time."""
    df = pd.read_csv(args.input)
    df["__is_target"] = True

    if args.history_input:
        history_df = pd.read_csv(args.history_input)
        history_df["__is_target"] = False
        if "id" in df.columns and "id" in history_df.columns:
            target_ids = set(df["id"].astype(str))
            history_df = history_df[~history_df["id"].astype(str).isin(target_ids)].copy()
        df = pd.concat([history_df, df], ignore_index=True)

    df["creation_time"] = pd.to_datetime(df["creation_time"], errors="coerce")
    return df.dropna(subset=["creation_time"]).sort_values("creation_time").reset_index(drop=True)


def load_transform_artifacts(args: argparse.Namespace, paths: dict[str, str]):
    """Load fitted vectorizers/encoders when transforming validation or holdout data."""
    if args.mode != "transform":
        return None, None, None

    text_vectorizer = joblib.load(paths["text_vectorizer"])
    rep_bigram_vectorizer = joblib.load(paths["rep_bigram_vectorizer"]) if args.related_mode == "rep_minus" else None
    tfidf_ngram_vectorizer = (
        joblib.load(paths["tfidf_ngram_vectorizer"]) if args.text_feature_mode == "enhanced" else None
    )
    return text_vectorizer, rep_bigram_vectorizer, tfidf_ngram_vectorizer


def main():
    args = apply_rep_weight_config(build_parser().parse_args())
    feature_dir = args.feature_dir
    ensure_dir(feature_dir)
    paths = feature_artifact_paths(args, feature_dir)
    df = load_feature_dataframe(args)

    # 1. Author factor
    df = add_author_features(df, args.exclude_targets_from_history)

    # 2. Temporal factor
    df = add_temporal_features(df, args.exclude_targets_from_history)
    df = add_same_severity_temporal_features(df, args.exclude_targets_from_history)
    df = add_literature_product_features(df, args.exclude_targets_from_history)
    df = add_error_driven_features(df)

    # 3. Related-report factor + Textual factor
    text_vectorizer, rep_bigram_vectorizer, tfidf_ngram_vectorizer = load_transform_artifacts(args, paths)
    df, text_vectorizer, rep_bigram_vectorizer, tfidf_ngram_vectorizer, X_text = add_related_report_features(
        df,
        vectorizer=text_vectorizer,
        rep_bigram_vectorizer=rep_bigram_vectorizer,
        tfidf_ngram_vectorizer=tfidf_ngram_vectorizer,
        text_feature_mode=args.text_feature_mode,
        related_mode=args.related_mode,
        rep_summary_weight=args.rep_summary_weight,
        rep_description_weight=args.rep_description_weight,
        rep_bigram_summary_weight=args.rep_bigram_summary_weight,
        rep_bigram_description_weight=args.rep_bigram_description_weight,
        rep_unigram_feature_weight=args.rep_unigram_feature_weight,
        rep_bigram_feature_weight=args.rep_bigram_feature_weight,
        rep_product_weight=args.rep_product_weight,
        rep_component_weight=args.rep_component_weight,
        rep_severity_weight=args.rep_severity_weight,
        rep_k1_unigram=args.rep_k1_unigram,
        rep_k3_unigram=args.rep_k3_unigram,
        rep_summary_b_unigram=args.rep_summary_b_unigram,
        rep_description_b_unigram=args.rep_description_b_unigram,
        rep_k1_bigram=args.rep_k1_bigram,
        rep_k3_bigram=args.rep_k3_bigram,
        rep_summary_b_bigram=args.rep_summary_b_bigram,
        rep_description_b_bigram=args.rep_description_b_bigram,
        exclude_targets_from_history=args.exclude_targets_from_history,
    )

    if "__is_target" in df.columns:
        target_mask = df["__is_target"].astype(bool).to_numpy()
        X_text = X_text[target_mask]
        df = df.loc[target_mask].copy().reset_index(drop=True)

    # 4. Severity/Product one-hot
    cat_cols = ["severity", "product", "component"]
    if args.mode == "fit":
        encoder = OneHotEncoder(handle_unknown="ignore", sparse_output=True)
        X_cat = encoder.fit_transform(df[cat_cols])
    else:
        encoder = joblib.load(paths["meta_encoder"])
        X_cat = encoder.transform(df[cat_cols])

    # 5. Numeric features
    numeric_values = df[NUMERIC_COLS].fillna(0.0).astype(float).values
    if args.mode == "fit":
        numeric_scaler = StandardScaler(with_mean=False)
        numeric_values = numeric_scaler.fit_transform(numeric_values)
    elif os.path.exists(paths["numeric_scaler"]):
        numeric_scaler = joblib.load(paths["numeric_scaler"])
        numeric_values = numeric_scaler.transform(numeric_values)
    else:
        numeric_scaler = None
        print("warning: numeric_scaler.joblib not found; numeric features are left unscaled.")

    X_num = csr_matrix(numeric_values)
    print(f"using {len(NUMERIC_COLS)} numeric features")

    # 6. Combine all features
    X_all = hstack([X_text, X_cat, X_num]).tocsr()
    y = df["priority_num"].values

    save_npz(paths["x"], X_all)
    np.save(paths["y"], y)

    if args.mode == "fit":
        joblib.dump(text_vectorizer, paths["text_vectorizer"])
        if args.related_mode == "rep_minus":
            joblib.dump(rep_bigram_vectorizer, paths["rep_bigram_vectorizer"])
        if args.text_feature_mode == "enhanced":
            joblib.dump(tfidf_ngram_vectorizer, paths["tfidf_ngram_vectorizer"])
        joblib.dump(encoder, paths["meta_encoder"])
        joblib.dump(numeric_scaler, paths["numeric_scaler"])

    df.to_csv(paths["meta"], index=False, encoding="utf-8-sig")

    print(f"X shape = {X_all.shape}")
    print(f"y shape = {y.shape}")
    print(f"saved features -> {paths['x']}")
    print(f"saved labels   -> {paths['y']}")
    print(f"saved meta     -> {paths['meta']}")


if __name__ == "__main__":
    main()
