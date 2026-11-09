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
| **`astropy_full/history_astropy_github.jsonl`** | 勾「這是歷史 Ticket」 | astropy GitHub issue 5559 筆（2023-07 以前），2176 筆有負責人、149 筆標成重複 | 重複偵測、負責人、優先級的比對歷史 | `scripts/build_astropy_github_demo.py` |
| **`astropy_full/new_tickets_astropy_github.jsonl`** | 一般匯入 | 3 張被維護者標成重複的 issue ＋ 補丁 6/36 官方修好的 6 張 | **全部七步，同一個專案** | 同上 |
| `astropy_full/answer_key_astropy_github.md/json` | 不要匯入 | 重複對象、修好它的人、真正修改的檔案、官方判定 | 對照 | 同上 |
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

## 一個資料集跑完全部七步：astropy_full

SWE-bench 沒有負責人和重複標記，所以 `astropy_full` 把 SWE-bench 的 6 張補丁 Ticket 和 astropy 自己的 GitHub issue 合在一起：

- 負責人：issue 的 GitHub assignee，沒有的話用關閉這個 issue 的 PR 作者（和 Bugzilla 的 assigned to 意思相近）；`assignee_source` 標出是哪一種。
- 重複：3 張新 Ticket 是維護者在留言裡指出「duplicate of #N」的 issue，原本那張在歷史裡。
- 歷史裡移除了新 Ticket 自己的 issue，以及 SWE-bench 驗證集／保留測試集的 issue（依標題比對；44 題中 43 題找到同名 issue，`astropy__astropy-13132` 沒有同名 issue）。
- 程式碼：和 `patch_demo` 一樣，要先在本機跑一次 `scripts/build_patch_demo.py`（clone 與版本快照）。
- 負責人推薦只用比該 Ticket 早建立的歷史（和重複偵測同樣的規則）。

在工作台的做法：如果之前匯入過 `patch_demo` 和它的 45 筆歷史，先在 astropy/astropy 資料夾按「刪除專案」，只勾 Ticket 與比對歷史、**不要勾「從設定檔移除」**；再匯入 `history_astropy_github.jsonl`（勾歷史）與 `new_tickets_astropy_github.jsonl`。

## 限制

- SWE-bench 的題目只取錯誤定位組的**開發集**（protocol_v1 development），沒有用驗證集或保留測試集。
- Firefox 的重複答案在 SBERT 的訓練資料裡，結果會偏好看；PRI 只用 Eclipse 訓練，套在其他資料集上是跨資料集推測。
- astropy 沒有優先級標記，優先級在 astropy 上沒有答案。
- 以上都是示範用，不是評估，不能拿來算準確率。
