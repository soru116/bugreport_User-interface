# Evaluation and validation

## 成功層級不能混淆

| 層級 | 能支持的結論 | 不能支持的結論 |
|---|---|---|
| Unit／contract tests | schema、gates、分支、fake client 行為 | 真實模型品質 |
| Synthetic I/O smoke | 指定 fixture 的模組接合／before-after | 真實 dataset 修復率 |
| Static／apply checks | 有效 diff、scope、可套用 | bug 已修好 |
| Supplied tests plausible | 指定 before/after/regression 通過 | 官方 benchmark resolved、完整 correctness |
| Official evaluator | 指定 instance/patch/env 的 resolved verdict | 未評估案件、其他模型或其他 cohort 成績 |

MAIN `completed_verified` 要 reproduction 與 regression 皆 passed；`tests_passed` 是部分測試通過；`patch_applied_unverified` 未功能驗證。這些是程式輸出條件，不是本次所有環境已通過的宣告。

## 現有 evaluator 地圖

| 任務 | 實際 evaluator | 判讀重點 |
|---|---|---|
| Ticket JSON | [ToJson evaluate_results.py](../ToJson/scripts/To_Json/evaluate_results.py) | field accuracy／exact match；matched records、fields、backup、observable-only、missing-gold 選項影響比較集合 |
| Duplicate | [DUP metrics.py](../bug-duplicate-detection/bug-duplicate-detection/src/duplicate_ticket_detection/metrics.py)、`tfidf_detector.py`／`sbert_detector.py`／`decision.py` | MAP、Top-k、MRR、ranking recall 與 decision confusion matrix 分開；AP 使用 relevant set，不能任意改 denominator |
| Priority | [PRI training/eval](../bug-priority-drone/bug-priority-drone/scripts/train_recall_balanced_priority_model.py)、`show_model_metrics.py` | accuracy／macro-F1／per-class recall／off-by-one／MAE；validation objective 與 natural holdout 分開 |
| Assignee | [MAIN evaluate_assignee_triage.py](../bug_tracking_llm_system/bug_tracking_llm_system/experiments/evaluate_assignee_triage.py)、`assignee_triage_accuracy/scripts/` | Top-k、coverage、unseen errors、校準／時間分窗與 live shadow；不能以一個 accuracy 取代部署 gate |
| FL | [FL evaluate_fault_localization.py](../fault_localization_feature_files/scripts/evaluate_fault_localization.py)、`src/utils/symbol_evaluation.py` | file Candidate Hit/Recall@20、Top-k/MRR、symbol exact/overlap 各有契約；目前 merged policy 與 legacy 不混用 |
| Patch | [REPAIR evaluation.py](../patch/integrated-fixed-patch/src/fim_patch/evaluation.py)、[CLI](../patch/integrated-fixed-patch/src/fim_patch/cli.py) | reference similarity 是描述性；official harness 另給 verdict，error 不等於 unresolved |

MAIN `scripts/evaluate_integrated_baselines.py` 及 `experiments/evaluate_*` 是較輕量的輸出評分。特別是 `evaluate_patch_success.py` 的 **patch_success_rate 只計 generated／provided 且非 manual 的比例**，不是測試修復成功率。`evaluate_test_generation.py` 以 `fib_passed_tests` 是否非空計 FiB，但目前 TestGenerator 留空、實際 reproduction 結果在 regression output；不可將此數字当完整 FiB 執行結果。

## Test／commit generation 的現況

[TestGenerator](../bug_tracking_llm_system/bug_tracking_llm_system/src/modules/test_generator.py) 接受 ticket tests／commands／test diff，或保留文字 reproduction plan。它不呼叫 LLM，也沒有完整 LIBRO 自主測試生成。`RegressionTester` 透過 [git_utils](../bug_tracking_llm_system/bug_tracking_llm_system/src/utils/git_utils.py) 在副本 apply，測試預設不執行、ticket commands 預設不信任。

Regression output 的 `changed_code_coverage=0.0` 是常數；`ripr_analysis` 四欄由同一 success boolean 填入。尚未實作獨立 coverage instrumentation／RIPR 階段量測，不能寫成完成研究驗證。[CommitMessageGenerator](../bug_tracking_llm_system/bug_tracking_llm_system/src/modules/commit_message_generator.py) 只以 deterministic 規則產文字；不會 commit/push。工作台的手動補丁路徑另在 [`src/integration/commit_message.py`](../bug_tracking_llm_system/src/integration/commit_message.py) 使用 Code Llama-Instruct 13B 根據選中 diff 產生一行草稿，同樣不會 commit/push，且模型輸出仍需人工審查。

## 局部驗證指令

以下是 PowerShell。先切到表列 cwd，`$py` 指向可用 Python；本次使用 Python 3.12.14：

```powershell
$py = 'C:/Users/User/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = (Resolve-Path src).Path
& $py -m unittest discover -s tests -p test_main_stage3_integration.py -v
```

上例 cwd 是 MAIN；在其他机器請換成實際 interpreter。各入口都應在自己的程序／cwd 執行，以免 `src/utils`／`fim_patch` 被另一份程式遮蔽。按修改範圍選下列 test pattern，**不要把整表當每次都要跑的 SOP**。

| cwd（工作區相對路徑） | `-p` pattern／用途 |
|---|---|
| `bug_tracking_llm_system/bug_tracking_llm_system` | `test_pipeline.py`：前段、routing、short-circuit、validation；`test_main_stage3_integration.py`、`test_stage3_patch_handoff.py`：設定／context contracts |
| `bug-duplicate-detection/bug-duplicate-detection` | `test_metrics.py`、`test_stable_rerank.py` 或相關 dataset/decision tests；需該專案依賴 |
| `fault_localization_feature_files` | `test_fault_localization.py`／`test_symbol_*.py`／`test_merged_ground_truth.py`，按實際變更選用 |
| `patch` | `test_fim.py`：獨立 FIM gates／validator |
| `patch/integrated-fixed-patch` | `test_repair_pipeline.py`、`test_patch_cli_and_evaluation.py`、`test_run_stage4_batch.py`、`test_fim_target_selection.py` |

PRI 與 JSON 未發現獨立 `tests/` suite；未來該模組變更需選小型 I/O／既有 evaluator 驗證，不虛構測試指令。完整 research rerun、模型下載、官方 Docker/Modal 或訓練須另有任務範圍，不是 documentation validation 的預設步驟。

本次實跑結果、兩個 MAIN runtime 失敗及確認範圍見 [restructure_review](restructure_review.md)。MAIN 測試內有硬編碼 `python3` subprocess；啟動 suite 使用 `$py` 不會自動修正子程序 interpreter。本次未為使測試變綠修改程式。
