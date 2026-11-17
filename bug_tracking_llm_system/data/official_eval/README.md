# data/official_eval：官方測試（僅供評估）

這個資料夾存放 SWE-bench 官方的評估資料，以及本機跑官方 harness 的結果。

**這裡全部都是答案資料。** 不可以被 Ticket 匯入、重複偵測、錯誤定位、補丁生成或重試回饋讀取。只有 `scripts/official_eval/run_local.py` 會在補丁生成完成之後使用它。寫回工作台的 `artifacts/<ticket>/official_local/*.json` 只有通過／失敗數，不含測試名稱。

| 路徑 | 內容 |
|---|---|
| `instances.jsonl` | SWE-bench 資料列（含 `test_patch`、`eval_script`、`FAIL_TO_PASS`、`PASS_TO_PASS`、映像檔名稱），harness 直接讀這個檔 |
| `instances/<id>/spec.json` | 方便人看的摘要：base commit、版本、映像檔、測試清單 |
| `instances/<id>/test.patch` | 官方測試補丁 |
| `manifest.json` | 來源資料集、split、swebench 版本、下載時間、sha256 |
| `runs/<run_id>/` | 每次評估的 predictions、harness 輸出、`verdict.json`、官方 log |
| `local_verdicts.jsonl` | 每次評估一行的總表 |
| `images/*.tar` | `docker save` 的映像檔備份（很大，不進 git） |

使用方式見 `scripts/official_eval/README_ZH.md`。
