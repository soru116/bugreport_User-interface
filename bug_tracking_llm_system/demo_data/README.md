# 示範資料集

組內目前用來 demo 的資料，直接用工作台的「匯入 Ticket」匯入。這是 `data/real_datasets/`（腳本在本機產生的位置，不上傳）的一份快照，放在這裡是為了讓組員不用重跑腳本就能匯入。這些檔案都是組內資料或 SWE-bench **開發集**產生的小型 JSONL；新 Ticket 已經移除答案欄位，答案只在 `answer_key*` 裡，**answer_key 不要匯入**。

| 檔案 | 怎麼匯入 | 內容 | 能展示的步驟 | 產生方式 |
|---|---|---|---|---|
| `history.jsonl` | 勾「這是歷史 Ticket」 | Firefox 1764 筆、Eclipse 1500 筆歷史（各自帶 `project`） | 重複偵測、負責人、優先級的比對歷史 | `scripts/build_real_dataset_demo.py` |
| `new_tickets_firefox.jsonl` | 一般匯入 | Firefox 6 張 | 重複偵測、負責人（有答案） | 同上 |
| `new_tickets_eclipse.jsonl` | 一般匯入 | Eclipse 6 張（PRI natural_test，模型沒看過） | 優先級（有答案） | 同上 |
| `answer_key.md/json` | 不要匯入 | 上面 12 張的重複對象、優先級、負責人 | 對照 | 同上 |
| `swebench_with_history/history_swebench.jsonl` | 勾「這是歷史 Ticket」 | psf/requests 19 筆、pallets/flask 1 筆較早的 issue | 重複偵測的比對歷史 | `scripts/build_swebench_demo.py --only-ids development_tickets.jsonl --limit-per-repo 6` |
| `swebench_with_history/new_tickets_swebench.jsonl` | 一般匯入 | psf/requests、pallets/flask 各 6 張（最新的 6 題） | 全部六步（負責人會轉人工） | 同上 |
| `swebench_with_history/answer_key_swebench.md/json` | 不要匯入 | 真正修改的檔案 | 錯誤定位對照 | 同上 |
| `patch_demo/new_tickets_patch_demo.jsonl` | 一般匯入 | astropy 6 張：補丁生成 6/36 實驗中官方判定修好的題目 | 錯誤定位、補丁生成（有官方判定） | `scripts/build_patch_demo.py` |
| `patch_demo/answer_key_patch_demo.md/json` | 不要匯入 | 那一輪的補丁 sha256 與官方判定 | 補丁對照 | 同上 |

## SWE-bench 與 astropy 還需要程式碼

錯誤定位與補丁生成要讀 Ticket 那個版本的程式碼。clone 和版本快照很大，所以沒有放在 GitHub，要在自己電腦上產生一次（會順便把專案加進 `integration_config.json`）：

```powershell
# psf/requests、pallets/flask（需要 SWE-bench parquet 與開發集 id 清單，見 workspace/README.md）
python scripts\build_swebench_demo.py --input <SWE-bench test.parquet> --only-ids <protocol_v1\development_tickets.jsonl> --limit-per-repo 6 --config integration_config.json

# astropy（沒有本機 clone 時會從 GitHub 下載）
python scripts\build_patch_demo.py --config integration_config.json
```

腳本產生的新檔案會放在 `data/real_datasets/`，不會改到這裡。

## 限制

- SWE-bench 的題目只取錯誤定位組的**開發集**（protocol_v1 development），沒有用驗證集或保留測試集。
- Firefox 的重複答案在 SBERT 的訓練資料裡，結果會偏好看；PRI 只用 Eclipse 訓練，套在其他資料集上是跨資料集推測。
- 以上都是示範用，不是評估，不能拿來算準確率。
