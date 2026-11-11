# Assignee routing and feedback

## 主系統已接合的能力

[AssigneeTriager](../bug_tracking_llm_system/bug_tracking_llm_system/src/modules/assignee_triager.py) 使用歷史 ticket、product/component、文字與 ownership 訊號，回傳 assignee、suggested_assignee、ranked_candidates、candidate_details、confidence／raw_confidence、routing_status、needs_manual_triage、fallback_reason 等。歷史來源、roster、inactive list、ownership、feedback、calibration／open-set artifact 由 [PipelineConfig](../bug_tracking_llm_system/bug_tracking_llm_system/src/config.py) 控制。

預設 history 優先使用存在的 `assignee_triage_accuracy/paper_grade/data/processed/bmo_paper_2024_3k_history_train.jsonl`，否則退至 `data/assignee_dataset.jsonl`；本次盤點後者不存在，不代表前者也不存在。缺乏有效訊號時使用 manual fallback。具 timestamp 的 history 依 ticket 時間過濾；不應假定所有來源都有真實 assignment event time。

校準／open-set／roster gate、mapping fallback 和候選建議需分開解讀。`assigned` 或 `assigned_by_fallback` 是本地 routing 結果；沒有因此證明外部 tracker 已更新。MAIN orchestrator 收到 manual routing 後仍會繼續 FL／patch，而非等待人員完成分派。

工作台的全自動模式不保留 assignee 人工關卡：先採模型的 `suggested_assignee`；模型 abstain 時，依序使用重複檢索中最相近的同專案歷史 Ticket owner、低信心 routing 候選、同 component／product 的歷史 owner，最後才用同專案最常處理者。所有歷史 fallback 都只使用比目前 Ticket 更早建立的紀錄。自動決定會記錄來源與理由，但不寫成 human feedback；半自動模式仍保留原有 manual triage 語意。

## 研究與部署子模組

| 路由 | 實際內容 | 狀態 |
|---|---|---|
| [assignee_triage_accuracy](../bug_tracking_llm_system/bug_tracking_llm_system/assignee_triage_accuracy/README.md) | baseline、profiles、accuracy、feedback、Top-5 selection scripts | Implemented；不是所有方法都為 MAIN default |
| [phase6_candidate_ltr](../bug_tracking_llm_system/bug_tracking_llm_system/assignee_triage_accuracy/phase6_candidate_ltr/README.md) | LTR ranker、calibration | Experimental（研究部署路徑） |
| [phase7_rolling_open_set](../bug_tracking_llm_system/bug_tracking_llm_system/assignee_triage_accuracy/phase7_rolling_open_set/README.md) | temporal replay、rolling/open-set | Experimental／部署尚未完成 |
| [phase8_operational_readiness](../bug_tracking_llm_system/bug_tracking_llm_system/assignee_triage_accuracy/phase8_operational_readiness/README.md) | roster、shadow、bundle、短效 operational guard | Partially implemented：程式有，真實部署驗收不足 |
| [phase9_v3_protocol](../bug_tracking_llm_system/bug_tracking_llm_system/assignee_triage_accuracy/phase9_v3_protocol/README.md) | v3 時間分窗、protected holdout、防洩漏 audit、policy gates | 協定工程 Implemented；新 holdout／正式 deployment Not verified |

LLM reranker／QLoRA 的研究入口在 `assignee_triage_accuracy/scripts/` 與 [llm_assignee](../bug_tracking_llm_system/bug_tracking_llm_system/assignee_triage_accuracy/llm_assignee/README.md)，不是當前 hybrid default 已被 instruction-tuned 模型取代的證據。

## 現有缺口

現存 phase8 `shadow/shadow_readiness_report.json` 為 0 live-shadow rows、deployment gate false；這是保存的 artifact，不是即時營運監測。Phase8 文件記錄 v2 holdout 嚴格 gate 不通過、Top-3 安全 fallback 停用；不得用另一個 synthetic／離線 replay 宣稱部署就緒。

尚需當期 roster 的真實組織 review、live-shadow provenance／latency、滿足協定的新 holdout 與操作人授權。不得重用 protected 2022 Q2 調 v3 模型或 policy。此任務只記錄限制，不建立 live events、不簽核 roster、不部署。

## 驗收

測試應涵蓋缺失／過期 artifacts、inactive owner、open-set manual fallback、as-of history、approved calibration、Top-k user selection 與 feedback provenance。學術 accuracy、可分派 coverage、unseen error 與操作 gate 分開。現有 tests 包括 `test_assignee_*` 以及 `test_pipeline.py`；本次已跑範圍見 [review](restructure_review.md)，通用驗收見 [done_criteria](done_criteria.md)。
