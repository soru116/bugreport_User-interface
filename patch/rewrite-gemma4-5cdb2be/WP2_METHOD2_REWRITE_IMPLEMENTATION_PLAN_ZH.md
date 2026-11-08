# WP2 方法二：對話模式整函式改寫實作計畫

> 計畫狀態：Phase 1–7 完成，Phase 8 待執行；Phase 7 未達成功門檻  
> 基準分支：`codex/wp2-before-after`  
> 基準測試：371 tests passing  
> 目標：新增非 FIM 的 instruct rewrite 路徑，讓模型讀取公開 issue 與原函式後，回傳完整修正版函式，再由 host 鎖定 signature、抽取 body、重建 patch 並沿用既有 gates。

## 1. 範圍與完成定義

方法二不是調整 FIM prompt，而是一條獨立 generation mode：

```text
公開 issue + localization + 原完整函式
                  ↓
       instruct 模型完整函式回答
                  ↓
   NO_CHANGE 或完整 FunctionDef parser
                  ↓
 signature/decorator/name 契約檢查
                  ↓
      抽取 body、重建原始檔案
                  ↓
  現有 syntax/scope/retention/name/reorder/apply gates
```

完成條件：

1. `--generation-mode fim|rewrite` 在單題 CLI 與 batch CLI 都可用，預設維持 `fim`，不改變既有結果。
2. rewrite mode 不傳 Ollama `suffix`、不放 FIM markers，也不依賴 `<EOT>`。
3. 模型只可回傳精確的 `NO_CHANGE`，或恰好一個完整 Python function／async function。
4. 函式名稱、同步／非同步種類、decorators、參數與 return annotation 不得改變；方法二只採用 body。
5. rewrite body 通過目前全部 gates 與 `git apply --check` 才能計為 generated/applied。
6. 固定 5 題完成 R1/R2，各題 4 candidates，分開報告 generated、applied、official resolved。
7. 既有 371 tests 全數通過，並新增方法二契約、parser、CLI、batch 與真實 Ollama smoke tests。

## Phase 0：文件與既有 API 盤點（已完成）

### Allowed APIs

- `src/fim_patch/backend.py::OllamaFIM.post(endpoint, payload)`：現有 urllib JSON transport，可沿用請求與 timeout 行為。
- `src/utils/llm_client.py::OllamaClient._generate()`：已有不帶 suffix 的 `/api/generate` payload 範例，但 rewrite backend 不應依賴其強制 JSON 格式或 curl subprocess。
- `src/fim_patch/generator.py::function_span()`：取得完整 target function、prefix、original middle、suffix、newline 與 BOM provenance。
- `src/fim_patch/generator.py::build_patch()`：已包含 `ast.parse`、no-op、executable change、undefined name、statement reorder、函式外修改與 unified diff 檢查。
- `FIMPatchGenerator.generate()` 的 candidate loop：已有 seed、duplicate、retention、`git apply --check`、validator 與最小 patch 選擇流程。
- `scripts/run_stage4_batch.py::make_generator()`、`build_record()`、`summarize()`、`write_predictions()`：可沿用批次、斷點、統計與官方 predictions 輸出。

### 目前不存在、不得假設的 API

- 沒有 `generation_mode`、`rewrite()`、`NO_CHANGE` parser 或完整函式回答 parser。
- `OllamaFIM.infill()` 只能代表 FIM 契約；不得在 rewrite mode 假傳空 suffix。
- `codellama:7b-instruct` 不接受 FIM suffix；rewrite mode 不得呼叫 `_require_infilling_template()`。
- 現有 qwen 測試使用 `qwen2.5-coder:14b-base`；R2 所需 instruct tag 必須在實驗前以 `ollama show` 驗證，不能把 base 結果標成 instruct。

### Anti-pattern guards

- 不把 gold patch、hidden tests、hints 或 FAIL_TO_PASS 名稱放進 prompt。
- 不讓模型直接輸出 diff；host 必須從 body 生成 diff。
- 不用 regex 單獨解析 Python 函式。
- 不因模型回答 `NO_CHANGE` 而硬造修改。
- 不讓 rewrite mode 默默回退到 FIM。

## Phase 1：建立 rewrite backend 契約（完成）

### 實作內容

在 `src/fim_patch/backend.py` 新增獨立 `OllamaRewrite`：

- 參數沿用 model、url、timeout、num_ctx、max_new_tokens、temperature。
- 提供 `rewrite(prompt, *, seed, max_new_tokens=None)`。
- 呼叫既有 `/api/generate`，payload 只含 `model`、`prompt`、`stream=False` 與 options；不得含 `suffix`、`raw`、FIM markers 或 JSON schema。
- 接受 `done_reason=stop`；`done_reason=length` 記為 incomplete，不進 parser。
- 回傳 raw response、stop reason、seed、token budget、prompt 與 Ollama metrics，維持可稽核性。
- `describe()` 驗證模型 tag 存在與 context length，但不執行 FIM suffix probe。

### 文件參考

- Transport 與 auditable response：`src/fim_patch/backend.py:25-42`。
- Ollama request 與 model metadata：`src/fim_patch/backend.py:66-101`。
- options、budget guard：`src/fim_patch/backend.py:127-144`。
- 非 suffix generate payload：`src/utils/llm_client.py:19-31`。

### 測試

- payload 不含 `suffix`、`raw`、`format`。
- seed、temperature、num_ctx、num_predict 正確傳遞。
- `stop` 成功；`length`、`done=false`、空 response 與 HTTP error 可稽核地失敗。
- instruct tag 不會觸發 FIM template probe。

### 驗收門檻

- backend 單元測試全部通過。
- 用 request capture 證明 rewrite 與 FIM payload 不混用。

### 實作結果

- 在 `src/fim_patch/backend.py` 新增獨立 `OllamaRewrite`，預設 instruct tag 為 `codellama:7b-instruct`。
- `rewrite()` 使用 Ollama `/api/generate`；payload 僅含 `model`、`prompt`、`stream` 與 `options`，不含 `suffix`、`raw`、`format` 或 FIM markers。
- `done_reason=stop` 才接受；`length`、`done=false`、空回覆與非預期 stop reason 均以可稽核 `GenerationError` 拒絕。
- `describe()` 只檢查 model tag 與 context metadata，不執行 FIM suffix probe。
- `OllamaRewrite` 已由 `fim_patch` package export，但尚未接入 generator 或 CLI。
- 新增 7 個 request／停止原因／budget／describe tests；focused 17 tests 與全套 378 tests 通過。
- anti-pattern grep、`py_compile` 與 `git diff --check` 通過。

## Phase 2：完整函式 prompt 與資料防洩漏（完成）

### 實作內容

在 `src/fim_patch/generator.py` 新增 `rewrite_prompt()`，內容只包含：

1. 公開 issue title／description／bug report，沿用 `evidence()` allowlist 與長度限制。
2. Stage-3 前五個 localization symbols，只放既有公開 localization context。
3. target 的完整原函式文字。
4. 明確輸出契約：只可輸出一個完整函式，或精確輸出 `NO_CHANGE`；禁止 Markdown、解說、diff 與 sibling definitions。
5. 最小修改要求：保留 working behavior，只修 issue 所需 executable lines。

新增並持續版本化 rewrite prompt；目前為 `issue-plus-full-function-rewrite-v2`，並在 result 中記錄 `generation_strategy=rewrite_function`。

### 文件參考

- 公開欄位 allowlist：`src/fim_patch/generator.py::evidence()`。
- localization context 與 prompt provenance：`src/fim_patch/generator.py:1085-1103`。
- ground-truth sanitization 測試模式：`tests/test_run_stage4_batch.py::SanitizationTests`。

### 測試

- prompt 包含完整原函式與 issue requirements。
- prompt 不含 patch、test_patch、hints、gold symbols、FAIL_TO_PASS/PASS_TO_PASS。
- nested method、decorated function、async function 與 CRLF/BOM source 都能形成 prompt。
- prompt 中明確包含 `NO_CHANGE` 契約，但不把一般文字內的 `NO_CHANGE` 誤判成結果。

### Anti-pattern guards

- 不沿用 BEFORE/AFTER FIM markers。
- 不把整份 source file 傳給模型；只傳 target function 與 bounded context。
- 不以 prompt 指示取代 host-side signature 檢查。

### 實作結果

- 在 `src/fim_patch/generator.py` 新增 `rewrite_prompt()` 與 prompt version `issue-plus-full-function-rewrite-v1`。
- rewrite prompt 只接受 `function_span(..., include_signature=True)` 的完整函式 slice；body-only span 直接拒絕。
- 公開需求使用明確 allowlist；rewrite 額外允許公開 `bug_report`，但既有 FIM 的預設 allowlist 與 prompt 行為不變。
- localization 僅輸出前五名的 file path／symbol identity，不複製 code text、score、gold 或 hidden-test 欄位。
- prompt 要求只回傳一個完整函式或精確 `NO_CHANGE`，並禁止 Markdown、分析、diff、class、module statements 與 sibling functions。
- 原函式含 ASCII／SentencePiece FIM markers 時拒絕，避免 rewrite prompt 誤帶 FIM 控制字串。
- 新增 5 組測試，涵蓋完整函式與前五名 localization、ground-truth leakage、nested decorated async、CRLF/BOM、bounded issue 與 marker/body-only rejection。
- focused 13 tests 與全套 383 tests 通過；anti-pattern grep、`py_compile` 與 `git diff --check` 通過。
- 本階段尚未解析模型回覆，也尚未接入 generator candidate loop；這兩部分分別屬 Phase 3、Phase 4。

## Phase 3：完整函式回答 parser 與 signature lock（完成）

### 實作內容

新增 `parse_rewrite_response(response, original_span)`：

1. 去除首尾空白後，若內容精確等於 `NO_CHANGE`，回傳明確 no-change outcome。
2. 拒絕 Markdown fence、分析文字、diff header、空回答與多個 top-level definitions。
3. 以 `ast.parse` 驗證回答恰好包含一個 `FunctionDef` 或 `AsyncFunctionDef`。
4. 比較原函式與回答的：
   - function name；
   - `def`／`async def` 種類；
   - decorators；
   - type parameters（若目前 Python AST 支援）；
   - positional-only、positional、vararg、keyword-only、kwarg 與 defaults；
   - return annotation 與 type comment。
5. 從回答的 function suite 取出 body，依原始 `original_middle` indentation 重新縮排；最終仍由 `build_patch()` 重建原檔。
6. parser outcome 記錄 `rewrite_no_change`、`rewrite_contract_violation`、`signature_changed`、`multiple_functions` 等穩定 reason code。

### 文件參考

- 原始函式座標與 body：`src/fim_patch/generator.py::function_span()`。
- AST 與函式外 scope 比較：`src/fim_patch/generator.py:679-722`。
- newline/BOM reconstruction：`src/fim_patch/generator.py:679-690`。

### 測試矩陣

- 普通函式、async、decorator、多行 signature、nested method。
- 合法 body 修改可抽取並套回。
- 精確 `NO_CHANGE` 被記錄但不生成 patch。
- `NO_CHANGE` 加解說、code fence、兩個函式、class、module statement 均拒絕。
- 改 function name、參數、default、annotation、decorator、async kind 均拒絕。
- 回答只有 body 而沒有完整函式時拒絕。
- comments、docstring、blank lines 與 CRLF 不破壞 reconstruction。

### 驗收門檻

- parser fuzz/edge tests 不可讓任何 signature 變更進入 `build_patch()`。
- 原始檔除 target body 外 byte-equivalent。

### 實作結果

- 新增 `parse_rewrite_response()`；精確 `NO_CHANGE` 回傳 `rewrite_no_change`，附帶說明或其他文字時不視為 no-change。
- 使用 `ast.parse(..., type_comments=True)` 要求回答恰好一個完整 `FunctionDef`／`AsyncFunctionDef`，不使用 regex、`eval` 或 `exec` 解析 Python。
- 鎖定 function name、同步／非同步種類、decorators、完整 arguments/defaults、return annotation、type comment 與 Python 3.12 type parameters。
- 拒絕 Markdown fence、diff、body-only、class、module statement、多函式、前後分析文字、inline suite 與空 body。
- 以 tokenize 找出完整多行 function header 的 suite 起點，只抽取 body；comments、docstring 與 blank lines 可保留。
- nested method 會從原始 repository slice 恢復實際 body indentation，而不是使用 AST dedent 後的固定四格縮排。
- parser 回傳的 body 已能直接進入既有 `build_patch()`，測試證明 sibling function 與 target signature 保持不變。
- 新增 7 組 parser tests；focused 7 tests 與全套 390 tests 通過。
- parser anti-pattern check、`py_compile` 與 `git diff --check` 通過。
- 本階段尚未呼叫 `OllamaRewrite` 或接入 candidate loop；該 wiring 屬 Phase 4。

## Phase 4：將 rewrite mode 接入 generator（完成）

### 實作內容

在 generator 新增 `GENERATION_MODES = ("fim", "rewrite")`：

- `FIMPatchGenerator.__init__()` 新增 `generation_mode="fim"`；為降低改名風險，第一版保留既有 class name。
- `fim` 完整保持目前 code path，不改 prompt、stop handling 或結果格式。
- `rewrite` 使用完整函式 prompt、`backend.rewrite()` 與 Phase 3 parser。
- parser 成功後，把 body 送進既有：retention → `build_patch()` → duplicate → snapshot recheck → `git apply --check` → optional validator。
- `NO_CHANGE` 是 rejected candidate，reason=`rewrite_no_change`，不算 generated。
- 第一版 rewrite 僅允許單一 primary target；遇到 `related_targets`／caller multi-function policy 時明確 blocked，避免同一候選混用 rewrite 與 FIM。
- `large_function_strategy` 在 rewrite v1 固定 `full_body`；若指定 `issue_statement`，CLI 直接報參數衝突。

### 文件參考

- generator constructor 與 result contract：`src/fim_patch/generator.py:780-839`。
- candidate loop：`src/fim_patch/generator.py:1104-1251`。
- 全部既有 gates：`src/fim_patch/generator.py:679-722`。

### 測試

- 相同 fixture 下 `fim` 行為與既有結果完全一致。
- rewrite backend double 回傳完整函式後能生成 patch。
- `NO_CHANGE`、signature change、scope escape、low retention、undefined name、reorder-only、duplicate 與 apply failure 都被正確分類。
- backend type 與 generation mode 不匹配時 fail fast，不做模型呼叫。

### Anti-pattern guards

- 不在同一 candidate 中先 rewrite、失敗後再 FIM。
- 不為了讓候選通過而降低 retention 或 scope gates。
- 不把 `NO_CHANGE` 當 no-op exception 混在一起；兩者需分開統計。

### 完成結果（2026-09-27）

- `FIMPatchGenerator` 已支援 `generation_mode="fim" | "rewrite"`；預設仍為 `fim`，既有呼叫不需修改。
- rewrite mode 使用獨立完整函式 span 建立 prompt，僅呼叫 `backend.rewrite()`，再以 Phase 3 parser 抽取 body；失敗時不會回退 FIM。
- parser 輸出已接回原 candidate pipeline，繼續執行 retention、scope、可執行變更、未定義名稱、statement reorder、duplicate、source snapshot、`git apply --check` 與 optional validator gates。
- `NO_CHANGE` 會以 `rewrite_no_change` 單獨拒絕；signature 變更、contract violation 與既有 gate reason 維持可區分。
- rewrite v1 已限制為單一 primary target 與 `large_function_strategy=full_body`；不相容 policy／backend 在模型呼叫前 fail fast。
- artifacts 會記錄 `generation_mode=rewrite`、`generation_strategy=rewrite_function`、rewrite prompt version 與完整原函式可見性。
- 新增 7 組 generator integration tests；focused 7 tests 與全套 397 tests 通過，`git diff --check` 通過。
- 本階段尚未新增 CLI 與 batch 參數；該 wiring 屬 Phase 5。

## Phase 5：CLI、batch、artifact 與統計 wiring（完成）

### 實作內容

修改：

- `src/fim_patch/cli.py`
- `scripts/run_stage4_batch.py`
- `tests/test_patch_cli_and_evaluation.py`
- `tests/test_run_stage4_batch.py`

新增：

- `--generation-mode {fim,rewrite}`，預設 `fim`。
- mode-aware backend factory：FIM 建立 `OllamaFIM`；rewrite 建立 `OllamaRewrite`。
- run config、ticket result、batch record 寫入 generation mode、prompt version 與 backend kind。
- summary 新增 `generation_mode_counts`、`rewrite_no_change`、`rewrite_contract_violation`、`signature_changed`。
- predictions 格式保持 SWE-bench 所需三欄，不因 mode 改變。

### CLI 相容性規則

- rewrite mode 禁止顯式 `--fim-transport` 非預設值。
- rewrite mode 禁止 `--large-function-strategy issue_statement`。
- model 預設仍不自動切換：使用者選 rewrite 時必須明確指定 instruct model，避免無聲改變實驗條件。
- batch resume 必須核對 run config；不能把 fim 與 rewrite 結果寫進同一 run directory。

### 驗收門檻

- 舊指令不加 `--generation-mode` 時結果仍是 FIM。
- rewrite run 的 artifacts 可單獨重現，且不會被標成 `fim_psm`。
- generated、applied、validation plausible、official resolved 持續分開報告。

### 完成結果（2026-09-27）

- 單題 CLI 與 batch runner 均新增 `--generation-mode {fim,rewrite}`；省略時仍採 `fim` 與 `codellama:7b-code`。
- 共用 mode-aware backend factory：FIM 建立 `OllamaFIM`，rewrite 建立 `OllamaRewrite`，不會將 suffix／FIM transport 傳入 rewrite backend。
- rewrite mode 要求明確 `--model`，並拒絕 `raw_psm` 與 `large_function_strategy=issue_statement` 等不相容組合。
- run config、ticket result 與 batch record 均保存 generation mode、strategy、prompt version 與 backend kind。
- `--resume` 會比對完整既有 run config；generation mode、model 或其他實驗條件不同時拒絕追加，避免混合實驗。
- summary 新增 `generation_mode_counts`、`rewrite_no_change`、`rewrite_contract_violation`、`signature_changed`，並分開報告 generated、applied、validation plausible、official resolved。
- `apply_status=passed` 在 validator 後續拒絕補丁時仍保留，避免把「可套用」誤當成「已生成候選」或「已解題」。
- predictions 仍維持 SWE-bench 的 `instance_id`、`model_name_or_path`、`model_patch` 三欄。
- Phase 5 focused 59 tests 與全套 405 tests 通過；`py_compile`、anti-pattern grep 與 `git diff --check` 通過。
- 尚未呼叫真實 Ollama；模型 smoke 屬 Phase 6B。

## Phase 6：離線測試與真實 Ollama smoke（完成）

### 6A：離線測試

預計新增至少 20 個方法二測試，涵蓋：

- backend payload／stop behavior：5 個。
- prompt leakage／contract：4 個。
- parser／signature lock：8 個。
- generator integration／gates：5 個。
- CLI／batch／summary：4 個。

數量是下限，不把「剛好 21 個、總數 374」當驗收條件；驗收依行為覆蓋率，而非湊測試數。

### 6B：真實模型 smoke

先用合成的 5–10 行函式執行：

1. `codellama:7b-instruct` 能回傳完整函式或 `NO_CHANGE`。
2. request 中沒有 suffix。
3. response parser 能處理模型實際格式。
4. 至少一個候選能生成並通過 `git apply --check`。

若模型頻繁輸出 Markdown，先調整 prompt；不得預設靜默剝除任意 fence／解說，以免錯誤接受多餘內容。

### 完成結果（2026-09-27）

- 方法二已有 38 個專屬離線測試：backend 7、prompt 5、parser 8、generator 8、CLI 7、batch 3，超過 20 個下限且符合各層契約覆蓋目標。
- 新增 `scripts/smoke_rewrite_generation.py`，建立 7 行合成函式與暫存 Git repository，使用真實 `codellama:7b-instruct` 執行四個 rewrite candidates。
- 第一輪揭露 3 個 `multiple_functions` 與 1 個 `truncated_or_incomplete`；parser 全數拒絕，沒有放寬 contract。
- prompt 末尾新增明確的單函式輸出指令，避免模型先重複 buggy function；因 prompt 實質改變，版本升為 `issue-plus-full-function-rewrite-v2`。
- 第二輪可解析 2 個完整函式，但均為 no-op；另 2 個因附帶說明文字以 `rewrite_contract_violation: extra_text` 拒絕。
- 最終以明確的合成需求驗證 transport／parser／generator：4/4 回傳可解析完整函式，1 個 generated、3 個 duplicate，補丁新增 `None` guard 並通過 `git apply --check`。
- 四次真實 generate request 均不含 `suffix`；模型 digest、候選原始回覆、分類與 patch 保存於 `reports/PHASE6_REWRITE_SMOKE_CODELLAMA_7B_INSTRUCT.json`。
- 此 smoke 僅證明端到端路徑可運作，不代表 SWE-bench 真實題目 resolved；正式品質由 Phase 7 固定五題實驗判定。
- 全套 406 tests、`py_compile` 與 `git diff --check` 通過。

## Phase 7：固定 5 題 R1/R2 實驗（完成，未達成功門檻）

### 凍結條件

- 題目：13073、13236、13465、14365、14598。
- 每題 4 candidates、seed 42。
- 相同 target policy、context、timeout、max tokens、retention 與 gates。
- 唯一自變項為 model：
  - R1：`codellama:7b-instruct`
  - R2：`qwen2.5-coder:14b` 的 instruct tag；執行前保存 `ollama show` model metadata/digest。
- 不使用 gold patch、hidden tests 或 hints 調 prompt。

### 必報指標

每個模型分開報告：

- 20 calls 中 `NO_CHANGE`、完整函式、length、timeout、contract violation 次數。
- no-op、no executable change、signature changed、undefined name、reorder-only、retention、scope、apply failure 次數。
- generated tickets、applied tickets、官方 resolved tickets。
- 每題 selected candidate、patch 行數、target rank、耗時與人工 issue relevance review。

### 成功門檻

1. 至少一個模型在 5 題中產生 ≥1 個 issue-relevant、可套用 patch。
2. 至少一題通過官方 SWE-bench FAIL_TO_PASS 與 PASS_TO_PASS。
3. 不以 generated 取代 resolved。
4. 若 R1/R2 都是 0 resolved，不擴到 20 題；先做 failure taxonomy。

### 已知限制

- 13465 若真正需要 signature 修改，rewrite v1 會由 signature lock 拒絕；此結果應記為 scope limitation，不是模型失敗。
- v1 只支援一次修改一個函式；跨函式修復另立後續階段。
- 本機目前沒有 Docker/SWE-bench harness；正式 resolved 需在具 Docker 的環境或 GitHub Actions 執行。

### 完成結果（2026-09-27）

- R1 使用 `codellama:7b-instruct`，R2 使用新安裝並核對 digest 的 `qwen2.5-coder:14b` instruct tag；未使用既有 `14b-base`。
- 自動比較確認兩組 run config 除 model 外完全相同；各 5 題、20 calls，且沒有 timeout。
- R1：parsed complete function 5、contract violation 9、length 4、multiple-functions 2、no-op 2；generated/applied tickets 1，但人工判定為不可達 dead code，issue-relevant=0。
- R2：`NO_CHANGE` 13、contract violation 7；generated/applied=0。
- 兩組 official resolved 均維持 `null`；因沒有 issue-relevant patch且本機無 Docker／可用 WSL／`gh`，未執行官方 harness。
- 成功門檻 1、2 未達；依規則不擴至 20 題，已先完成 failure taxonomy。
- 完整結果見 `reports/STAGE4_METHOD2_REWRITE_EXPERIMENT_ZH.md` 與 `reports/METHOD2_PHASE7_RESULTS.json`。

## Phase 8：最終驗證與交付

### 驗證清單

1. 執行完整單元測試，確認既有 371 tests 與所有新增 tests 通過。
2. `git diff --check`。
3. grep 確認 rewrite payload 不含 `suffix`／FIM markers。
4. grep 確認 prompt 沒有 ground-truth 欄位。
5. 比較預設 FIM smoke 的 before/after artifacts，證明 default path 無回歸。
6. 保存 R1/R2 run config、model digest、records、summary、predictions 與報告。
7. 官方 harness 結果回填 `official_resolved`，不得手動推定。

### 交付物

- rewrite backend 與 parser。
- generation mode CLI/batch wiring。
- 離線測試與真實 Ollama smoke artifacts。
- `reports/STAGE4_METHOD2_REWRITE_EXPERIMENT_ZH.md`。
- R1/R2 各自可重現的 run directory。

## 建議執行順序與工時

| 階段 | 預估 | 可見成果 |
|---|---:|---|
| Phase 1 | 1–2 小時 | instruct backend payload 測試通過 |
| Phase 2 | 1–2 小時 | 完整函式 prompt 與 leakage tests |
| Phase 3 | 3–5 小時 | parser 與 signature lock 完成 |
| Phase 4 | 2–4 小時 | rewrite candidate 可走完既有 gates |
| Phase 5 | 1–2 小時 | CLI/batch/artifacts 可重現 |
| Phase 6 | 2–4 小時 | 全測試與真實 Ollama smoke |
| Phase 7 | 1–3 小時模型執行時間 | R1/R2 固定 5 題結果 |
| Phase 8 | 1–2 小時，不含 Docker queue | 報告與官方 verdict |

總預估：11–22 小時，加上官方 Docker evaluation 等待時間。

## 第一個實作切片

先只完成 Phase 1，不改 generator：新增 `OllamaRewrite` 及其 request-capture tests。驗收時必須能證明 `codellama:7b-instruct` 的 request 沒有 `suffix`，且既有 `OllamaFIM` 測試完全不變。完成後再進入 prompt/parser，避免 transport 與解析問題混在同一個除錯階段。
