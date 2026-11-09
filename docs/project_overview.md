# Project overview

盤點日期：2026-09-16。範圍是完整 AI-assisted Bug Tracking and Automated Program Repair 工作區，不只是 FL／patch。

## 實際目錄與責任

下列代號僅是本文件群的路徑縮寫，不是新增的 package 或環境變數。

| 代號 | 實際根目錄 | 責任／主要入口 |
|---|---|---|
| MAIN | [bug_tracking_llm_system/bug_tracking_llm_system](../bug_tracking_llm_system/bug_tracking_llm_system/) | `src/main.py`、`src/pipeline/orchestrator.py`：完整 ticket workflow；另含 demo、assignee 研究、tests |
| JSON | [ToJson](../ToJson/) | `scripts/To_Json/`：資料蒐集、抽取、後處理、欄位評估；literature code benchmarks 是另一個實驗 |
| DUP | [bug-duplicate-detection/bug-duplicate-detection](../bug-duplicate-detection/bug-duplicate-detection/) | `src/duplicate_ticket_detection/cli.py`：TF-IDF／SBERT ranking、rerank、decision experiments |
| PRI | [bug-priority-drone/bug-priority-drone](../bug-priority-drone/bug-priority-drone/) | `scripts/`：Eclipse 特徵工程、priority 模型訓練與報告 |
| FL | [fault_localization_feature_files](../fault_localization_feature_files/) | `scripts/fault_localization.py`、實驗 runner、file／symbol evaluator、merged gold policy |
| FIM | [patch](../patch/) | `fim_patch/__main__.py`：獨立第一版 FIM；`attach_fim` adapter |
| REPAIR | [patch/integrated-fixed-patch](../patch/integrated-fixed-patch/) | `scripts/run_patch_pipeline.py`：統一 FL → FIM → evaluation；另有 batch runner |

根目錄不是 Git repository；DUP、PRI、FL、REPAIR 各有 `.git`。相同名稱的 `src/utils` 或 `fim_patch` 不保證內容相同，測試必須從正確 cwd 啟動。`tmp/` 是 staging／歷史副本；`runs/` 含實驗與上游測試 repositories；它們不是新的全系統主入口。

## Implementation status

`Implemented` 表示具體列出的能力有程式與契約；不表示所有環境、真實模型或正式研究驗收都通過。驗證狀態另列，不以 script 存在判定功能完成。

| 能力 | Implementation status | 實際狀態／驗證界線 |
|---|---|---|
| MAIN ticket normalization | Implemented | 接受 raw／`predicted_json`，本次 pipeline normalization 測試通過 |
| JSON LLM extraction | Implemented but not integrated | 獨立 Ollama 抽取及評估流程存在；MAIN extractor 可注入 client，但 default 不注入；本次真實推論 Not verified |
| MAIN duplicate／historical retrieval、priority | Implemented | 輕量 similarity 與 deterministic factors 已接 orchestration；不是 DUP／PRI 訓練模型 |
| DUP SBERT／rerank、PRI trained classifiers | Implemented but not integrated | 訓練／排名／評估程式、模型或報告存在；未接 MAIN default；重訓與模型結果重現 Not verified |
| MAIN assignee hybrid／manual fallback | Implemented | 歷史、ownership、roster、calibration gates 已接；本次相關 pipeline 測試通過 |
| Assignee LTR／rolling／open-set deployment | Partially implemented | 研究及 guard 程式存在；live shadow、有效當期 roster、正式 deployment acceptance 尚缺；LLM／QLoRA 分支 Experimental |
| FL retrieval／deterministic Stage 3 | Implemented | MAIN 預設 B1 structured + coverage-aware-v1；獨立 FL／REPAIR 有各自入口，不能混用設定與指標 |
| Symbol LLM reranking | Experimental | 獨立研究分支，未採為 MAIN Stage-3 default；正式研究 gate 未完整通過 |
| MAIN localization → diff generator | Partially implemented | handoff 已接、可配置 LLM；default 關閉，無 provided patch 時可能停於 manual；不是 native FIM |
| FIM body replacement／REPAIR single pipeline | Implemented | fresh predicted FL → adapter → FIM → static/apply／可選 supplied tests；本次可控 backend 的 I/O 測試通過，真實模型 Not verified |
| REPAIR batch／official harness adapter | Partially implemented | batch、post-generation reference comparison、official adapter 已實作；正式 resolved 成功結果本次 Not verified |
| Test generation | Partially implemented | 只 normalize supplied executable tests 或產生文字 test plan；自主 LLM 從 prose 產生可執行測試 Not implemented |
| Regression validation | Implemented | 暫存副本 apply、before/after/regression 有實作；MAIN 本機兩項 runtime 測試失敗，端到端完成狀態 Not verified |
| Commit message | Implemented | MAIN 為 deterministic 文字；工作台手動補丁成功後另以 Code Llama-Instruct 13B 產生單行草稿；都不會自動 Git commit／push |
| 全系統 production 自動修復部署 | Not verified | 沒有本次完整真實模型、外部 tracker assignment、official benchmark 通過的證據 |

## 完整系統實際流程

```text
Raw ticket（亦可含 ToJson predicted_json）
  → TicketExtractor：預設 normalization；LLM injection 可選
  → DuplicateDetector：歷史相似 ticket Top-k
      duplicate → 結束；需複核／exception → needs_duplicate_review
  → PriorityClassifier：existing priority 或 deterministic factors
  → AssigneeTriager：候選／assigned／manual；manual 結果不會讓整個 pipeline 暫停
  → BugLocalizer：目前 repo index → files → Stage-3 predicted symbols
  → PatchGenerator：confidence/context gate → provided／opt-in LLM diff
      無可用 patch → needs_manual_patch
  → TestGenerator：supplied tests normalization／文字計畫
  → RegressionTester：temp copy apply → 可選 reproduction／regression
  → CommitMessageGenerator → checkpoints／final_pipeline_result.json
```

研究目標是 Current FL → predicted location/span → FIM → apply → validation → evaluation。REPAIR 已把這段接成獨立 pipeline：clean base snapshot → fresh index／FL → `LocalizationForPatchV1` → native FIM → candidate selection／supplied tests → artifacts → 可選 post-generation reference comparison／official harness。**REPAIR 不包含 extraction、duplicate、priority、assignee 前段；MAIN default 不會自動轉入 REPAIR。**

FIM 的 `attach_fim(orchestrator, ...)` 可顯式替換 MAIN patch generator；存在 adapter 不等於 default 已接。所有 repair 路徑都只能使用目前 FL 預測；本文件沒有引入 gold／oracle localization pipeline。

## 其他支援模組

MAIN 的 `demo/demo_app.py`、pollable FL job、code-index／cache management、health check 是展示與操作支援。`experiments/` 是各模組輸出 evaluator，不能視為每個研究能力都已部署。[研究計畫](../bug_tracking_llm_system_code_flow_plan.md)、根目錄架構圖與 [文獻](../文獻/) 是背景，不是 implementation evidence。

後續依任務路由到 [docs index](README.md)；本次測試與已知落差集中於 [review](restructure_review.md)。
