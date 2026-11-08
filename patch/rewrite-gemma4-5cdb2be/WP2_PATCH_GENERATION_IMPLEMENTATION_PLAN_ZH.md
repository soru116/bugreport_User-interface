# WP2 補丁生成後續實作計畫

> 基準分支：`wp2-target-selection` @ `117fee8`  
> 實作分支：`codex/wp2-before-after`  
> 目標：先降低原函式照抄，再補強靜態把關與大型函式策略。

## Phase 0：文件與既有介面確認（完成）

允許沿用的現有介面：

- `src/fim_patch/generator.py::evidence()`：只放公開 issue 欄位與可選的原函式 body。
- `FIMPatchGenerator(include_original_body=True)`：控制是否把原實作放進 prompt。
- `scripts/run_stage4_batch.py --include-original-body`：固定 5 題批次入口。
- `scripts/compare_stage4_summaries.py`：比較 generated、停止原因與拒絕原因。

禁止事項：不得把 gold patch、hidden tests 或 hints 放進 prompt；不得更換模型、seed、候選數或 gate，否則無法把差異歸因於 prompt 樣式。

## Phase 1：BEFORE／AFTER prompt 實驗（完成，不採用）

### 實作

1. 原函式註解區標記 `# BEFORE (buggy):`。
2. 真正 FIM 填空位置前標記 `# AFTER (fixed):`。
3. prompt version 升為 `issue-plus-before-after-localization-psm-v5`。
4. 沿用既有 `instructfim-A-control`，在相同 5 題執行實驗組。

### 驗證

- 單元測試確認 BEFORE → 原函式 → AFTER → function signature 的順序。
- 確認 gold patch、hidden tests、hints 仍不會進入 prompt。
- 比較照抄次數、generated、`length`／`visible_eot` 與拒絕原因。

### 驗收門檻

- 主要門檻：照抄少於 control 的 12/16。
- 次要門檻：至少 1/5 generated，且人工檢視確實處理 issue。
- 若修改率上升但破壞性重寫增加，不採用 v5。

### 實驗結果

- 4 題實際進入模型，共 16 次呼叫；13073 因本機 snapshot commit 不一致未進模型。
- 照抄仍為 12/16（`no_op` 5、`no_executable_change` 7），與 control 相同。
- generated 仍為 1，且仍是 14365 的無關語句重排，沒有處理大小寫問題。
- `visible_eot` 8、`length` 8，與 control 相同。

結論：單純加入 BEFORE／AFTER 標籤沒有改善照抄，不升為正式預設。下一階段先補靜態／語意閘門，再設計不把完整原 body 放在生成點上方的表示法。

## Phase 2：未定義名稱閘門（完成）

### 實作

使用 Python `symtable` 與 AST 比較修改前後名稱，拒絕補丁新引入、且在 local／global／builtins／import 中無法解析的引用。

### 驗證

- 可攔截 `WriterClass`、`_report_hdu_count` 與刪除定義後仍使用的 `wrapper`。
- 不誤擋函式參數、import alias、closure、class attribute 與合法 builtins。

### 防呆

不得把所有 attribute name 當 global name；`obj.method` 的 `method` 不應獨立查表。

### 實作結果

- 使用標準庫 `symtable` 比較修改前後 target function 與其 nested scopes。
- 只拒絕修改後新出現的 unresolved bare names；既有動態 global 保持允許。
- module import／definition、function local／parameter、closure 與 builtins 均視為合法。
- module 使用 star import 時保守跳過，避免無法證明來源時誤擋。
- `obj.method` 不視為 bare name，避免誤擋繼承或動態 attribute。
- 新增 7 個閘門測試，並修正 2 個原本缺少跨檔 helper import 的 multi-file fixtures。
- 全套 361 項測試通過。

## Phase 3：無意義語句重排閘門（完成）

### 實作

比較正規化 AST statement multiset 與順序；只有重排、沒有內容變更的候選標記為 `statement_reorder_only`。

### 驗證

- 攔截 14365 類型的無關語句搬動。
- 保留確實需要調整控制流程順序的候選供人工審查，不直接宣稱修復成功。

### 實作結果

- 比較修改前後 target function 的 top-level executable statement AST。
- 忽略函式 docstring，避免文件文字掩蓋純重排。
- 函式 signature、decorator、return annotation 或其他契約改變時，不標為純重排。
- 只有 statement 順序不同、排序後 AST multiset 完全相同時，拒絕為 `statement_reorder_only`。
- 新增 5 個測試，涵蓋 14365 型重排、真正 statement 修改、新增 statement、signature 變更與 `build_patch` 整合。
- 全套 366 項測試通過。

## Phase 4：大型函式生成策略（完成）

### 實作

針對超過 context budget 的函式，評估局部 branch/span infill；輸出必須帶原函式座標與 reconstruction provenance。

### 驗證

- 13073 能進入模型呼叫，不再於呼叫前被 context budget 阻擋。
- reconstruction 後通過 syntax、scope、retention 與 `git apply --check`。

### 防呆

不得默默截斷 signature、decorator 或跨出選定 statement block。

### 實作結果

- 新增顯式 `--large-function-strategy issue_statement`；預設仍是 `full_body`。
- 僅對至少 80 行的函式啟用，使用公開 issue 文字與 AST identifiers 選一個最多 40 行的 top-level statement／branch。
- 完整函式仍由 host reconstruction 保留；模型只能替換被選 statement。
- statement prompt 的公開 issue 文字上限為 4,000 字元，避免長討論串耗盡 context。
- visible-EOT 輸出必須剛好一個 statement；run-on 輸出只保留第一個可解析 statement。
- 13073 從 100 行 `_get_reader` 精確縮到 2 行 `converters` branch，成功進入模型並輸出 visible EOT。
- 該 smoke 候選額外生成第二個 `header_end` branch，已由 `statement_scope_escape` 安全拒絕。
- 全套 371 項測試通過。

## Phase 5：固定集合正式驗證（5 題生成完成，官方評估待執行）

1. 先跑固定 5 題，至少取得 1 題官方 resolved。
2. 通過後才擴至固定 20 題。
3. 永遠分開報告 `generated`、`applied`、`resolved`。

完成定義：固定 20 題可重現、無 ground-truth leakage、至少一題新增 official resolved，且 PASS_TO_PASS 不回歸。

### 固定 5 題執行結果

- 使用 `codellama:7b-code`、`issue_statement`、固定 seed 42，每題 4 candidates，共 20 次候選生成。
- 5 題均成功嘗試，無 skipped／errored；1 題 generated，4 題 `no_valid_candidate`。
- generated 的 `astropy__astropy-13073` 已通過 syntax、scope、retention 與 `git apply --check`，因此 `applied = 1`。
- 新 gates 實際攔截 `statement_reorder_only` 3 次、`statement_scope_escape` 3 次；另有 `no_op` 8 次、`no_executable_change` 4 次。
- 官方 `resolved` 尚未取得：目前主機沒有 Docker、可用 WSL distro、`swebench` evaluator 或 GitHub CLI，不能在本機啟動官方隔離 harness；不得把此狀態記成 unresolved。
- 完整結果與後續正式評估指令記錄於 `reports/STAGE4_PHASE5_FIXED5_EVALUATION_ZH.md`。

### Phase 5 下一個 gate

先在具 Docker 的環境，或把 predictions 放進可由 GitHub Actions checkout 的分支後，對 `astropy__astropy-13073` 執行官方 harness。只有 `official_resolved = true` 才擴到固定 20 題；若為 false，先修正 statement target／prompt，不擴大樣本。
