# v7：固定 36 案的本機執行結果

2026-10-03 已完成真實 Ollama 模型呼叫；36 題皆有紀錄，沒有執行錯誤。模型 digest 與輸入 SHA-256 已核對。
生成程式為 `0defb3633d6ff7200cbfbe0c2dcbb2b488f2e0c5`；未修改核心生成器或閘門。

| 指標 | 結果 |
|---|---:|
| 固定分母 | 36 |
| Generated / Applied | 2 / 2 |
| Generated / 36 | 5.56% |
| No valid candidate | 33 |
| Blocked invalid context | 1 |
| 執行錯誤 | 0 |
| 各題耗時加總 | 752.312 秒（約 12 分 32 秒） |
| 保存實際 prompt token 計數的模型呼叫 | 28 |
| 實際 prompt tokens 合計／最大 | 66,813 / 3,525 |
| 實際 output tokens 合計 | 15,745 |
| 官方修復成功 | **尚未驗證** |

## 與歷史基線比較

9/21 為 1/36、9/29 Qwen 為 3/36。本次 v7 為 2/36，因此相較較新的 9/29 基線，生成數沒有提高。
9/29 與本次使用相同的 36 IDs、凍結預測定位、model tag、temperature 0.2、seed 42、1 candidate、context 16384、輸出 4096 tokens。
v7 與歷史程式的差異不只指令位置，這是整套新版 prompt 流程的歷史比較，不是單一因素因果實驗。

- 保留的生成案例：`astropy__astropy-7606`。
- 新增的生成案例：`astropy__astropy-13462`。
- 9/29 曾生成、本次未生成：`astropy__astropy-13572`、`astropy__astropy-13465`。

## 13462 的生成統計需特別解讀

13462 通過現行生成閘門，故忠實保留在 Generated=2 中。
額外比對補丁套用前後的整檔 AST，遞迴移除 module/class/function docstring 且忽略位置信息後，**AST 完全相同**。
這表示它只有 docstring／註解變化，沒有修改可執行邏輯。此事後檢查不覆寫原始候選，也不變更本次閘門或統計。
7606 的 AST 則有執行邏輯變化，但仍須官方測試；靜態差異不能證明正確修復。
證據見 `results/v7-task-last-20261003/executable_ast_audit.json` 與兩題的 `selected.diff`。

## 主要失敗訊號

候選紀錄包含 context budget 拒絕 7 次、輸出不完整 6 次、Middle/Suffix 行界線不合 5 次、無執行邏輯變化 5 次、no-op 5 次。
這些是候選拒絕次數，不一定互斥，也不能相加作為失敗案例數。
實際 token 計數只來自成功取得 response metadata 的呼叫；不能把事前 budget 拒絕的案例當作已有 token 測量。

## 組員接續官方測試

在具備 Docker、SWE-bench 官方 harness 的環境，於專案根目錄執行：

```sh
python experiments/eval36_v7/evaluate_official.py --predictions experiments/eval36_v7/results/v7-task-last-20261003/predictions.jsonl
```

本次提供 2 筆預測；仍以固定 36 題為修復率分母。請保存 resolved ID、各題 test output、環境錯誤與 harness 版本。
尚未執行官方測試，所以不能宣稱修復數為 0；`official_resolved=null` 保留其未驗證狀態。
