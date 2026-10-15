# Documentation index

這是整個工作區的 coding-agent 文件入口。先從 [AGENTS.md](../AGENTS.md) 的 routing 選一份相關文件；不要求每次讀完本目錄。

| 文件 | 用途 |
|---|---|
| [project_overview.md](project_overview.md) | 七個實際程式根目錄、主要模組狀態、兩種 orchestration 與完整流程 |
| [extraction_normalization.md](extraction_normalization.md) | ToJson 與主系統 normalization 的契約及落差 |
| [ticket_classification.md](ticket_classification.md) | Duplicate／retrieval 與 priority 的獨立研究和整合 baseline |
| [assignee_routing.md](assignee_routing.md) | 負責人候選、人工確認、時間協定、部署限制 |
| [fault_localization.md](fault_localization.md) | FL 版本邊界、Stage 1–3、預測 handoff |
| [../bug_tracking_llm_system/workspace/README.md](../bug_tracking_llm_system/workspace/README.md) | 整合版 Ticket 工作台：五個已完成功能（ToJson、DUP、PRI、Assignee、FL）如何串接、設定檔、網頁與 CLI、備援規則與未驗證項目 |
| [patch_generation.md](patch_generation.md) | 主系統 diff、獨立 FIM、統一 repair pipeline |
| [datasets.md](datasets.md) | 資料位置、schema、gold／holdout 邊界 |
| [evaluation.md](evaluation.md) | 現有 evaluator 語意、測試指令與驗證層級 |
| [task_prompt_templates.md](task_prompt_templates.md) | 按任務選用的六欄 prompt templates |
| [done_criteria.md](done_criteria.md) | 共通及 domain 驗收條件 |
| [restructure_review.md](restructure_review.md) | 2026-09-16 盤點、自我 review、測試證據、差異與後續工作 |

本目錄負責目前架構與導覽；子專案 README、研究規格、protocol 與 frozen reports 保留原位，避免複製數值或改寫歷史結論。文件衝突時核對該入口的程式及設定，並記錄衝突；不可默默套用另一個 checkout 的模型／evaluation policy。
