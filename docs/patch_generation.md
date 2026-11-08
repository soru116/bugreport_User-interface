# Patch generation, FIM and handoff

研究 invariant：**Current Fault Localization → predicted location/span → context → FIM → apply → validation → evaluation**。Ground truth/reference patch 僅供隔離評估；不可作 localization input。

## 三個真實入口

| 入口 | 實際行為 | 狀態 |
|---|---|---|
| [MAIN PatchGenerator](../bug_tracking_llm_system/bug_tracking_llm_system/src/modules/patch_generator.py) | supplied diff 或 configured LLM 產 unified diff；Stage-3 context、confidence gate；default `patch_generation_enabled=False` | Partially implemented（相對目標 native FIM pipeline）；handoff 已接合 |
| [FIM CLI](../patch/fim_patch/__main__.py) | `python -m fim_patch`，選 `--localization` 或 `--system-src`，native PSM；output 是 `.json` | Implemented，獨立第一版；非 MAIN default |
| [REPAIR CLI](../patch/integrated-fixed-patch/src/fim_patch/cli.py) | fresh FL + `RepairPipeline` + FIM + evaluation；output 是新目錄；不接受上述兩個歷史 localization flags | Implemented，repair 子流程統一入口；非完整 ticket orchestration |
| [工作台第 6 步](../bug_tracking_llm_system/src/integration/patch.py) | 工作台自己的 Stage-3 定位 → [`patch/rewrite-gemma4-5cdb2be`](../patch/rewrite-gemma4-5cdb2be/PROVENANCE_ZH.md)（fixed-patch `5cdb2be`，官方 6/36 那一版）的 `RepairPipeline` + 對話式改寫（gemma4:12b，3 候選）；方法設定凍結為該 run 的 `run_config.json`；官方判定只以 patch sha256 對應 | Implemented，網頁手動按鈕；假模型測試通過，真實 gemma4 在工作台內 Not verified |

工作台選出補丁後，會把該 unified diff 與可觀察 Ticket 脈絡交給 [`integration/commit_message.py`](../bug_tracking_llm_system/src/integration/commit_message.py)，以 `codellama:13b-instruct` 零樣本產生一行 Conventional Commit 草稿。此步驟不會接觸 gold patch／hints，也不會執行 `git commit`；失敗時保留補丁並記錄錯誤。

FIM／REPAIR 都叫 `fim_patch`，必須用正確 cwd 與 import path。新的統一 repair 任務優先定位 REPAIR；維護 FIM 第一版才使用外層 `patch/` 文件，不能混用 CLI 參數與 exit code。

## Input → Output

REPAIR：ticket 至少含 `base_commit`、公開 bug evidence 及可追蹤 ID；repo HEAD 必須對應 base，working tree 乾淨。當次 index／FL → `localization_for_patch()` 驗證 symbol 欄位與來源 fingerprint → `LocalizationForPatchV1` → generator。

Generator 驗證 commit、source bytes／hash、Python AST 與 symbol 座標，預設 strict Top-1 function/method body replacement。模型以 native PSM FIM 補 middle；host 重組 source／diff，檢查 AST、scope、no-op／duplicates、apply。可選 validator 執行 supplied unittest 的 before/after/regression；沒有 validator 時不能宣稱功能修復。

REPAIR 輸出 `ticket.json`、`localization.json`、`candidates.json`、`result.json`、`selected.diff`、`evaluation.json`、`predictions.jsonl`。可選 `ground_truth.json` 在生成完成後才載入／比較 reference；`official_evaluation.json` 保存官方 evaluator verdict。詳見 [INTEGRATION_ZH](../patch/integrated-fixed-patch/INTEGRATION_ZH.md)。

MAIN `utils/patch_context.py` 是另一個契約：由 Stage-3 Top-k source 建完整 context，驗 snapshot／行號；其 instruction client 直接要求 unified diff，**不是 native FIM**。MAIN 的非 benchmark ticket 可提供 patch，此路徑不是模型生成證據。`source_dataset` 或 `instance_id` ticket 的 developer patch 不可當 generated output。

## Optional integration 與 batch

[外層 attach_fim](../patch/fim_patch/integration.py) 可對已初始化的 MAIN orchestrator 顯式替換 generator，downstream `RegressionTester` 仍保留；default builder 沒呼叫它。此 adapter 不能證明所有前段研究模型已接好。

[REPAIR run_stage4_batch.py](../patch/integrated-fixed-patch/scripts/run_stage4_batch.py) 建 detached base snapshots、剝除 gold/test/hints 欄位、逐 ticket 呼叫相同 RepairPipeline、記錄 failures、resume 與彙總。`official_resolved` 保留 None，generation counts 不是 repair rate。其 test suite 本次通過；真實 batch 模型執行 Not verified。

Research mode、local context、target policy／span expansion 的支援在兩個 FIM copy 不同，以各自 `generator.py`／`cli.py` 為準。這些選項不改變 localization 來源必須是預測的要求；不得偷偷將研究 bypass 變預設。

## Validation 語意與限制

`generated` 只代表有通過生成 gates 的 patch；`not_run` 未功能測試；`plausible` 只代表指定測試通過；`official_resolved=true` 才是該官方評分結果。REPAIR CLI：0 為 supplied plausible 或 official resolved，2 為有 patch 未功能驗證，1 為 blocked/failed 或 official unresolved；exception／infrastructure error 不得算成「已驗證 unresolved」。外層 FIM CLI 的 0 僅要求有 patch，語意不同。

現存 synthetic smoke artifact 為 generated/plausible、official_resolved null。本次 2 項 REPAIR pipeline tests 使用真實 FL＋可控 backend，不重跑 Ollama；official adapter tests 使用 fake runner。官方 Docker／Modal 成功執行、真實修復率仍 Not verified。

目前不支援一般 multi-file/signature/import repair 或任意語言 FIM。自動 feedback repair loop、從文字自主生成驗證 tests 不屬這個 MVP 完成範圍。只修 documentation 時記錄缺口，不擴充演算法。評估細節見 [evaluation](evaluation.md)；資料邊界見 [datasets](datasets.md)。
