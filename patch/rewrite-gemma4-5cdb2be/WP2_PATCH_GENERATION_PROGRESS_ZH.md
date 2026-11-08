# WP2 補丁生成：功能、改進與測試進度

> 更新日期：2026-09-20  
> 專案：`soru116/fixed-patch`  
> 分支：`wp2-target-selection`  
> 本次更新基準 commit：`5f9e83f`（20 題 prediction artifact）

## 1. 目前成果摘要

本次工作集中在改善 Patch Generation 的目標選擇、跨檔修改能力與真實模型生成品質。

| 功能 | 狀態 | 驗證證據 |
|---|---|---|
| Stage-3 跨檔 helper 候選擴充 | 完成 | 可定位 `report_diff_values` |
| Called-helper 排序 | 完成 | 可從 caller 關係提升真正 helper |
| Tolerance-aware repair prompt | 完成 | 具備 `rtol`／`atol` 語意要求與 gate |
| Multi-symbol patch | 完成 | 可同時修改 helper 與跨檔 caller |
| Qwen FIM backend | 完成 | `qwen2.5-coder:14b-base` 可生成候選 |
| Host-side caller 精準修改 | 完成 | 以 AST 定位 call 並傳遞 tolerance |
| 專案測試 | 通過 | 350 / 350 tests passed |
| 官方 SWE-bench | 通過 | `astropy__astropy-13465` resolved |
| 20 題 development batch | 已執行 | 8 / 20 產生可套用補丁 |
| 8 題官方 SWE-bench batch | 已執行 | 0 / 8 resolved；8 / 8 patch applied |
| Minimal-edit prompt v4 | 已實作並測試 | 124 / 124 相關 tests passed；真實模型 0 / 5 generated |

目前證明的是一個真實跨檔案例已由官方 harness 判定 resolved，尚未代表所有 repository 與 bug 類型都完成驗證。
後續跨案例測試也顯示，目前生成器可以穩定產生並套用部分補丁，但一般化的修復品質仍不足。

## 2. 原始功能與主要缺口

原始 Patch Generation 流程：

```text
Bug ticket
  → Fault Localization
  → Stage-3 ranked symbols
  → FIMPatchGenerator
  → host-generated unified diff
  → syntax／scope／apply gates
  → tests／official evaluation
```

原本已有：

1. 接收 Stage-3 localization 的目標 symbol。
2. 使用 FIM 模型生成 function body。
3. 由 host 產生 unified diff。
4. 驗證 source hash、base commit、Python syntax 與 `git apply --check`。
5. 保存 candidate、prompt、model response 與 rejection reason。

主要缺口：

1. 容易選到表面 caller，未穩定選到真正執行錯誤邏輯的 helper。
2. 一次只能修改一個 symbol，無法同步調整 helper signature 與 caller。
3. Prompt 對 tolerance-aware ndarray 比較的要求不足。
4. Backend 綁定 Code Llama FIM token，其他 FIM 模型無法使用。
5. 模型可能直接複製原 caller，沒有傳遞新增參數。

## 3. 本次功能改進

### 3.1 Stage-3 跨檔 helper 候選池

相關 commits：

- `618d297`：從 issue 函式名稱與跨檔呼叫圖加入被呼叫 helper。
- `1471cda`：提高與高分 caller 相連的 helper 分數。
- `ad905cc`：優先選擇名稱與 ticket 內容相關的 called helper。

改進前：

```text
模型容易修改呈現症狀的 caller
```

改進後：

```text
TableDataDiff._report
  → report_diff_values
```

Astropy 案例最終能定位：

```text
astropy/utils/diff.py::report_diff_values
```

並保留跨檔 caller：

```text
astropy/io/fits/diff.py::TableDataDiff._report
```

### 3.2 Tolerance-aware repair prompt

Commit：`ba89937`

Issue 涉及 `rtol`、`atol` 或 `tolerance`，且目標函式包含 ndarray 比較時，prompt 會要求：

- 浮點 ndarray 使用 tolerance-aware 比較。
- 使用既有 `where_not_allclose`。
- 傳入 `rtol` 與 `atol`。
- 非浮點資料保留 exact comparison。
- 保留 recursive reporting 行為。

新增 semantic gates：

- 候選必須包含 `where_not_allclose`、`np.isclose` 或 `np.allclose`。
- 候選必須使用 `rtol` 與 `atol`。
- 拒絕只修改註解、docstring 或重複原始函式的候選。

### 3.3 Multi-symbol patch

Commit：`3d313df`

新增 `called_helper_multisymbol` target policy：

1. 修改 helper 的完整 function，包括 signature。
2. 尋找 localization 提供的跨檔 caller。
3. 產生第二個 caller patch。
4. 合併成一份 multi-file unified diff。
5. 對完整 patch 執行 `git apply --check`。

支援的修補形式：

```python
def report_diff_values(..., rtol=0.0, atol=0.0):
    ...
```

以及：

```python
report_diff_values(..., rtol=self.rtol, atol=self.atol)
```

### 3.4 Qwen FIM backend

Commit：`86eaa76`

使用模型：

```text
qwen2.5-coder:14b-base
```

Backend 改進：

- 只有 legacy `raw_psm` transport 強制要求 Code Llama markers。
- 支援從不同模型 metadata 讀取 context length。
- 記錄模型的 `completion`／`insert` capabilities。
- 非 Code Llama suffix 模型可使用正常 model EOS。
- Code Llama 保留 `<EOT>` 嚴格檢查，避免安全 gate 回歸。

### 3.5 Host-side caller 精準修改

Qwen 能正確修改 helper，但第二次生成會原樣複製 caller，沒有加入：

```python
rtol=self.rtol, atol=self.atol
```

改善方式：

1. 模型負責需要推理的 helper 行為與 signature。
2. Host 解析 caller 的 Python AST。
3. 定位實際呼叫 selected helper 的 `ast.Call`。
4. 只加入缺少的 `rtol`、`atol` keyword arguments。
5. 再做 syntax、scope、body retention 與 executable-change 檢查。

這個方法不會重寫 caller 的其他 branches，也不是對整個檔案做任意字串取代。

## 4. 真實案例產生的補丁

案例：

```text
astropy__astropy-13465
rtol for FITSDiff not working as expected
```

輸出位置：

```text
runs/astropy-13465-qwen14b-multisymbol-v15-20260920/
```

### 4.1 `astropy/utils/diff.py`

修改內容：

- `report_diff_values` signature 新增 `rtol=0.0`、`atol=0.0`。
- ndarray 差異從 exact `a != b` 改為 `where_not_allclose(...)`。
- Recursive `report_diff_values` 繼續傳遞 tolerance。

### 4.2 `astropy/io/fits/diff.py`

修改內容：

- `TableDataDiff._report` 的兩個 `report_diff_values` call 傳入 `self.rtol`。
- 同時傳入 `self.atol`。
- 其他 reporting behavior 保持不變。

## 5. 測試與驗證

### 5.1 自動化測試

完整測試結果：

```text
Ran 350 tests in 32.381s
OK
```

新增或加強的測試涵蓋：

- 非 Code Llama insert model 的 backend metadata。
- Model EOS 與 Code Llama visible EOT 的差異。
- Multi-symbol helper signature 與 caller 修改。
- AST 精準加入 tolerance keywords。
- 原 caller 其他程式碼保持不變。

### 5.2 Static／host gates

生成候選必須通過：

1. Python AST syntax。
2. 修改範圍沒有離開 selected function。
3. 不是 no-op、註解或 docstring-only change。
4. Function body retention gate。
5. `git apply --check`。

### 5.3 官方 SWE-bench

GitHub Actions：

```text
https://github.com/soru116/fixed-patch/actions/runs/35503156176
```

官方結果：

| 分類 | 結果 |
|---|---|
| Patch applied | 成功 |
| FAIL_TO_PASS | 1 / 1 通過 |
| PASS_TO_PASS | 3 / 3 通過 |
| Infra failure | 0 |
| Resolved | `true` |

通過的 bug test：

```text
astropy/io/fits/tests/test_diff.py::test_rawdatadiff_diff_with_rtol
```

通過的 regression tests：

```text
astropy/io/fits/tests/test_diff.py::test_fitsdiff_hdu_name
astropy/io/fits/tests/test_diff.py::test_fitsdiff_no_hdu_name
astropy/io/fits/tests/test_diff.py::test_fitsdiff_with_names
```

官方結果檔：

```text
runs/astropy-13465-qwen14b-multisymbol-v15-20260920/official_evaluation.json
```

### 5.4 20 題 development batch

使用 `qwen2.5-coder:14b-base`、`num_ctx=8192`，在固定的前 20 題 development cases 執行批次生成：

| 指標 | 結果 |
|---|---|
| Attempted | 20 / 20 |
| Pipeline errors | 0 |
| Generated／apply-gate passed | 8 / 20（40%） |
| No valid candidate | 11 / 20 |
| Blocked invalid context | 1 / 20 |

8 個生成補丁全部送入官方 SWE-bench；workflow 均成功完成，且 8 / 8 補丁均可套用，但結果為：

```text
official resolved: 0 / 8
official unresolved: 8 / 8
```

其中部分補丁只未通過 `FAIL_TO_PASS`，另有補丁同時造成大量 `PASS_TO_PASS` regression。這證明 `generated` 與 `patch applied` 不等於實際修復成功。

批次輸入 artifact：

```text
runs/stage4-dev-20-ctx8192/predictions.jsonl
```

### 5.5 Original-body 與 minimal-edit prompt v4

為降低整函式重寫與 regression，加入：

- 將目前函式實作放入 prompt 作為 baseline。
- 要求先辨識 actual／required behavior 差異。
- 禁止原樣複製，要求最小 executable change。
- 要求保留無關 branches、calls、validation 與 numerical behavior。
- 僅輸出完整 replacement function body。
- prompt version 升級為 `issue-plus-current-body-localization-psm-v4`。

相關自動化測試：

```text
Ran 124 tests
OK
```

相同 5 題真實模型重跑結果：

| 指標 | 結果 |
|---|---|
| Attempted | 5 / 5 |
| Generated | 0 / 5 |
| no-op candidates | 10 |
| incomplete-body candidates | 8 |
| line-boundary failures | 2 |

因此 v4 prompt 已正確落實並通過測試，但 `qwen2.5-coder:14b-base` 在目前 FIM 路徑仍常複製原始內容或產生不完整 body。這一輪沒有 prediction 可送官方評估。

## 6. 目前完整度

### 已完成

- Helper target selection 與 called-helper 排序。
- Helper signature 與 implementation 修改。
- 跨檔 caller tolerance propagation。
- Qwen FIM transport 與 EOS 相容。
- Syntax、scope、semantic 與 apply gates。
- 一個真實跨檔 SWE-bench 案例官方 resolved。

### 尚未完整驗證

- 多 repository、多 bug 類型的成功率。
- 非 tolerance 類 multi-symbol repair。
- 大型函式、多 caller 或三個以上 symbols。
- Research policy 是否適合作為 production default。
- 批次執行時間、GPU／記憶體成本與 timeout 分布。
- 能在一般 development cases 通過官方 `FAIL_TO_PASS` 且不造成 `PASS_TO_PASS` regression 的生成策略。

目前合理結論：

> WP2 補丁生成流程、批次執行與官方評估路徑均已建立；一個特定跨檔 tolerance 案例可官方 resolved，但 20 題 development batch 的 8 個可套用補丁在官方評估為 0 / 8。現階段瓶頸是一般化的生成品質，而不是 diff 格式、套用或評估基礎設施。

## 7. 建議下一步

下一步先固定目前 5／20 題 development cases，不再增加案例數，依序：

1. 比較更適合遵循 edit instruction 的模型或非純 body-FIM 生成策略。
2. 改善完整 body 結束判定，降低 `incomplete_body`。
3. 對候選加入更嚴格的最小 diff／regression proxy 排序。
4. 先在同一批 5 題取得至少一個 generated candidate，再擴至 20 題。
5. 只對通過 static／apply gates 的候選執行官方 SWE-bench，持續分開記錄 generated、applied 與 resolved。
