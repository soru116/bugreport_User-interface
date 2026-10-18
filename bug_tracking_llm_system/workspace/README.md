# Ticket 工作台（整合版）

把組內**已經做好的五個功能**串成一條可以「匯入 Ticket → 逐步處理」的流程，並提供網頁介面和命令列兩種用法。

整合層不重寫任何演算法，每一步都直接 import／呼叫子專案原本的程式：

| 步驟 | 實際呼叫的程式 | 需要的訓練產物 | 缺少產物時 |
|---|---|---|---|
| 1. 轉換 JSON | `ToJson/scripts/To_Json/extract_to_json.py` 的 `build_prompt` → `call_ollama` → `extract_json` → `normalize_prediction` | 本機 Ollama 的 `codellama:7b-instruct` | 標示「備援」，改用主系統 `json_schema` 規則正規化 |
| 2. 重複偵測 | `bug-duplicate-detection/src/duplicate_ticket_detection`：SBERT title/content 分數 → `combine_similarity_scores(weighted:0.6,0.4)` → `apply_metadata_rerank_scores(component:0.02)` → `apply_stable_rerank_scores` | `SbertDuplicateDetector.save()` 輸出的 `title_model/`、`content_model/`，以及歷史 Ticket 資料 | 標示「備援」，改用同一個套件的 `TfidfDuplicateDetector` |
| 3. 優先級 | `bug-priority-drone/scripts/build_features.py --mode transform`（DRONE／REP- 特徵）＋ `analyze_priority_errors.predict_model_or_bundle` | `recall_balanced_priority_model.joblib`、`features_bm25_p2_error_keywords_train/`（fitted encoders）、清洗後的歷史 CSV | 標示「備援」，改用主系統固定權重公式 |
| 4. 分派負責人 | `bug_tracking_llm_system/src/modules/assignee_triager.py`（原封不動） | 負責人歷史 JSONL（預設找 paper_grade 的 bmo 資料） | 改用工作台匯入或確認過、含負責人的紀錄 |
| 5. 錯誤定位 | `bug_tracking_llm_system/src/modules/bug_localizer.py` → `utils/fault_localization_stage3.py`（與 `fault_localization_feature_files/src/utils/fault_localization.py` 內容完全相同） | 要定位的 Repository（可預先建立 code index） | 沒有設定專案時略過 |

「備援」會顯示在畫面、CLI 輸出和結果 JSON（`status: "fallback"` 與 `fallback_reason`），不會把備援結果當成訓練模型的結果。

## 流程與人工確認點

```text
匯入 → 1 轉換 JSON → 2 重複偵測 ──(工程師確認)──┬─ 是重複：記錄 duplicate_of，流程結束
                                                └─ 不是重複 → 3 優先級 → 4 負責人建議 → 5 錯誤定位
                                                                         └─ 工程師可隨時確認／改派負責人
```

- 重複偵測依 DUP README 的建議採「半自動」：預設 Top-k 候選一律交給工程師確認。若要自動判定，在設定檔填 `duplicate.auto_duplicate_threshold` 或 `review_threshold`（分數是 SBERT／TF-IDF 的原始相似度）。
- 負責人確認不會擋住錯誤定位，這和主系統 orchestrator 的行為一致。確認結果會用主系統的 `build_assignee_feedback_record` 寫進 `data/workspace/feedback/assignee_feedback.jsonl`。
- 處理完的 Ticket 會加入工作台歷史資料（`data/workspace/history/workspace_history.jsonl`），之後匯入的 Ticket 可以和它比對重複。
- 重複偵測只比對同一個專案、而且建立時間比這張 Ticket 早的歷史資料（真實系統裡，歷史一定比新回報早）；略過了幾張會寫在重複偵測的說明裡。
- SBERT 會把歷史資料的向量存在 `data/workspace/cache/sbert/`，每份歷史只需要編碼一次，之後每張 Ticket 只算自己。換了 SBERT 模型會自動另存一份，不會混用。

### 處理順序與排隊

伺服器用兩條工作線依序處理：

1. 「轉換 JSON＋重複偵測」一次一張，所以一批 Ticket 匯入後很快就可以開始確認重複。
2. 「優先級＋負責人＋錯誤定位」：準備好的 Ticket 會**一起**計算優先級（PRI 每次都要重算整份歷史的特徵，一次算 6 張和算 1 張的時間差不多）。同一批匯入的 Ticket 還在做重複偵測時，最多會等 90 秒再一起算。

卡片和 Ticket 頁面會顯示「排隊中，前面還有幾張，預計約幾分鐘」，畫面左下角會顯示目前處理中和排隊的數量。預估時間是依最近實際花的時間計算。伺服器關掉時還沒處理完的 Ticket，下次啟動會自動接著處理。

## 安裝

```bash
cd bug_tracking_llm_system
pip install -r requirements.txt          # numpy、pandas、scipy、scikit-learn、joblib、requests
pip install sentence-transformers datasets accelerate   # 只有要用 SBERT 重複偵測模型時才需要
```

PRI 的模型檔是用 scikit-learn 1.8.0 存的；1.9 版載入會出現 `No module named '_loss'`，請安裝相同版本：`pip install scikit-learn==1.8.0`。

## 取得訓練好的模型

模型與資料都被各子專案的 `.gitignore` 排除，不在 GitHub 上，需要另外準備：

- **重複偵測（SBERT）**：在 `bug-duplicate-detection` 用組內的 Firefox 資料訓練，CPU 約 15–20 分鐘：

  ```bash
  cd bug-duplicate-detection
  PYTHONPATH=src python -m duplicate_ticket_detection.cli train-sbert --tickets data/mozilla_firefox_duplicates.csv \
    --epochs 1 --max-triplets 2000 --negative-strategy hard --negatives-per-anchor 3 \
    --combine weighted:0.6,0.4 --output-dir models/duplicate_sbert_firefox
  ```

  這是 `train-sbert` 的版本，沒有 README 最佳成績用到的 top-1 mined hard negatives 那一步（那支實驗程式不會存模型）。
- **優先級（PRI）**：向負責的組員拿 `models/recall_balanced_priority_model.joblib`、`data/processed/features_bm25_p2_error_keywords_train/`、`data/processed/eclipse_bug_reports_clean_with_dupe.csv`，路徑填進設定檔的 `priority` 區塊。每張 Ticket 要重算整份歷史（約 1 萬筆）的特徵，約 1–2 分鐘。

伺服器啟動時會列出每一步是用訓練模型還是備援；改設定檔不用重開，伺服器會自動重新載入。

## 設定

```bash
cp integration_config.example.json integration_config.json
```

把各自電腦上的模型與資料路徑填進去，相對路徑以 `bug_tracking_llm_system/` 為基準：

- `duplicate.sbert_model_dir`：SBERT 微調模型資料夾
- `historical_tickets_paths`：重複偵測用的歷史 Ticket（CSV／JSONL，欄位名稱同 DUP 的 `load_tickets`）
- `priority.model_path`、`priority.reference_feature_dir`、`priority.history_csv`
- `assignee.assignee_dataset_path`：負責人歷史資料
- `projects.<名稱>.repo_path`：要做錯誤定位的 Repository；建議同時設定 `code_index_path`，在狀態頁預先建立索引

設定好之後先檢查：

```bash
python3 scripts/run_integrated_pipeline.py --config integration_config.json check
```

## 網頁介面

```bash
python3 workspace/server.py --config integration_config.json --port 8780
```

開啟 <http://127.0.0.1:8780>：

1. 右上「模型與資料狀態」：檢查每一步會用到的程式和產物在這台電腦上找不找得到，也可以建立 Repository 的程式碼索引。
2. 「匯入 Ticket」：
   - 勾選「這是歷史 Ticket」可以把舊資料加入比對資料庫，例如 `workspace/examples/history_tickets.jsonl`。
   - 一般匯入會立刻開始處理，例如 `workspace/examples/new_tickets.jsonl`。
   - 也可以手動填寫一張。
3. 首頁依專案分成資料夾。專案就是設定檔 `projects` 裡的 Repository，匯入 Ticket 時選的專案決定它放在哪個資料夾，也決定錯誤定位要在哪個程式碼庫裡找。匯入時沒選專案的 Ticket 放在「未指定專案」資料夾。
   - 資料夾上會顯示 Ticket 數、等你確認和處理中的數量；露出來的紙張顏色是最近幾張 Ticket 的狀態。
   - 點開資料夾才看到一張張 Ticket 卡片：上方是「釘選的 Ticket」區，其餘放在下方。點卡片右上角的圖釘可以釘選；釘選狀態存在伺服器的 Ticket 紀錄裡，重開伺服器、重新執行流程都會保留。
   - 卡片封面的顏色代表狀態：橘色是等你確認，紫色是處理中，綠色是已完成，紅色是失敗。左下五顆小圓點是五個步驟的進度，右下圓形貼紙是優先級。
   - 資料夾裡的「匯入到這個專案」會預先選好這個專案。
   - 最上方的搜尋欄可以找 Ticket 編號、標題、專案、產品、元件、負責人或優先級；輸入多個字詞時要全部符合。在首頁搜尋會找全部專案，在資料夾裡搜尋只找這個專案。按 `/` 可以直接開始輸入，按 Enter 會開啟第一張結果，按 Esc 清除。
   - 搜尋欄下方的「等你確認」等標籤，會篩出需要判斷是否重複或確認負責人的 Ticket。
   - 要新增專案資料夾，在設定檔的 `projects` 加一個 Repository 即可，不用重開伺服器。
4. 點一張卡片會進入 Ticket 頁面，左上角的返回鍵回到它所在的專案資料夾，也可以用瀏覽器的上一頁；每個資料夾和每張 Ticket 都有自己的網址（`#/p/<專案>`、`#/t/<編號>`）。標題列上的「‹ 3 / 6 ›」可以直接切換到同一個專案的上一張或下一張 Ticket，鍵盤是 `[`、`]`；順序和你剛才在資料夾裡看到的一樣，從「等你確認」篩選點進來時，就只在那幾張之間切換。進入後，上方的進度列會一直固定在畫面上，顯示五個步驟各自的狀態。每個步驟是一個分頁：點進度列上的步驟就能切換，也可以用鍵盤左右鍵。畫面會自動停在需要你處理的那一步；你自己點過分頁後，就不會再自動跳走。
5. 每個步驟的分頁底部都會寫出使用的模組、程式路徑、耗時；用了備援時也會寫出原因。優先級分頁的每個因子都可以把滑鼠移上去看簡短說明，點一下開啟詳細解釋。
6. 右上「更多」裡可以下載完整結果 JSON、從頭重新執行或刪除 Ticket。

範例資料是**示範用的虛構 Ticket**，內容描述 `bug-duplicate-detection` 原始碼中可能出現的錯誤，搭配設定檔中的 `duplicate-detection` 專案示範錯誤定位。它們不是評估資料，不能拿來算準確率。

## 用組內的真實資料示範

`scripts/build_real_dataset_demo.py` 會把子專案用過的真實 Bugzilla 資料，轉成工作台可以匯入的檔案。它只讀資料，不會改動原本的資料集：

| 專案資料夾 | 來源 | 歷史資料 | 新 Ticket |
|---|---|---|---|
| Mozilla Firefox | `bug-duplicate-detection/data/mozilla_firefox_*.csv` | 約 1,760 筆，含重複標記與負責人 | 4 張真的是重複、2 張不是 |
| Eclipse | `bug-priority-drone/data/raw/eclipse_bugzilla_raw_with_dupe.csv` | 隨機 1,500 筆（含正確答案） | PRI `natural_test` 中 P1–P5 各 1 張，加 1 張重複 |

```bash
python3 scripts/build_real_dataset_demo.py          # 自動尋找兩個子專案的 data 資料夾
```

輸出在 `data/real_datasets/`（不會上傳 GitHub）：

1. 先用「匯入 Ticket」勾選「這是歷史 Ticket」匯入 `history.jsonl`。
2. 再匯入 `new_tickets_firefox.jsonl`、`new_tickets_eclipse.jsonl`。每一筆都帶 `project`，會自動放進對應的專案資料夾。新 Ticket 已移除重複對象、優先級、負責人和處理結果。
3. 處理完後對照 `answer_key.md`，裡面是被移除的真正答案。

要注意：

- 每張 Ticket 都要用 SBERT 比對約 1,500–1,800 筆歷史，再用 PRI 重算整份 Eclipse 歷史的特徵，一張大約需要 2–3 分鐘，Ticket 會一張一張依序處理。
- 這兩個專案沒有 Repository，所以錯誤定位會顯示「略過」。
- SBERT 是用 Firefox 重複資料訓練的，Firefox 的重複偵測結果會偏好看；PRI 只用 Eclipse 訓練，Firefox 的優先級是跨資料集推測。這是示範，不能拿來算準確率。
- Eclipse 資料沒有負責人欄位，Eclipse Ticket 會建議人工分派。

重複偵測和負責人推薦只會使用同一個專案（以及沒有標專案）的歷史資料，所以 Firefox 和 Eclipse 不會互相比對。優先級計算時，如果新 Ticket 本身就在 PRI 的歷史 CSV 裡，會先把那一筆排除，避免模型讀到自己的答案。

### SWE-bench：錯誤定位也用真實資料

錯誤定位是在 SWE-bench 上開發和評估的。SWE-bench 的每一題都是真實的 GitHub issue，而且記錄了 issue 當時程式碼的版本（`base_commit`），所以五個步驟都能用真實資料跑。

如果錯誤定位組員的資料夾已經下載過完整的 SWE-bench（`fault-localization-feature-files/data/fault_localization/swebench_full/raw/test-00000-of-00001.parquet`），可以直接用它，並加上 `--only-ids .../swebench_full/protocol_v1/development_tickets.jsonl` 只取開發集，避開保留測試集：

```bash
python3 scripts/build_swebench_demo.py --input <swebench_full>/raw/test-00000-of-00001.parquet \
  --only-ids <swebench_full>/protocol_v1/development_tickets.jsonl --limit-per-repo 6 --config integration_config.json
```

沒有的話：

1. 用瀏覽器下載 SWE-bench Lite：<https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/resolve/main/data/test-00000-of-00001.parquet>，放到 `data/real_datasets/`。
2. 執行（需要 git 和網路；預設只取兩個小專案 `psf/requests`、`pallets/flask`，共 9 題）：

   ```bash
   pip install pyarrow
   python3 scripts/build_swebench_demo.py --input data/real_datasets/test-00000-of-00001.parquet --config integration_config.json
   ```

   它會 clone 這兩個 repository，把每一題需要的版本匯出成 `data/swebench/snapshots/<repo>/<commit>/`，並把兩個專案加進設定檔（原檔備份為 `.bak`）。
3. 匯入 `data/real_datasets/new_tickets_swebench.jsonl`。每張 Ticket 會在**自己那個版本**的程式碼裡做錯誤定位；第一次用到某個版本時會先建立程式碼索引，之後重用。
4. 對照 `answer_key_swebench.md`：裡面是真正的修補改了哪些檔案。新 Ticket 只有 issue 本文，沒有 patch、測試或提示。

注意：錯誤定位團隊在 SWE-bench 上調過方法，這不是沒看過的測試資料；這些專案沒有重複標記和負責人歷史；PRI 是用 Eclipse 訓練的。這是示範，不是評估。

## 命令列（批次）

```bash
# 加入歷史 Ticket
python3 scripts/run_integrated_pipeline.py --config integration_config.json import-history --input workspace/examples/history_tickets.jsonl
# 預先建立程式碼索引
python3 scripts/run_integrated_pipeline.py --config integration_config.json build-index --project duplicate-detection
# 匯入並處理；遇到重複候選會停在「等你確認」
python3 scripts/run_integrated_pipeline.py --config integration_config.json run --input workspace/examples/new_tickets.jsonl --output result.json
# 批次測試時略過人工確認（決定會被記錄為 cli:--assume-not-duplicate）
python3 scripts/run_integrated_pipeline.py --config integration_config.json run --input tickets.jsonl --assume-not-duplicate
```

CLI 會把多張 Ticket 的優先級合併成一次 `build_features.py` 呼叫，比逐張呼叫快。

## 匯入格式

JSON、JSONL、CSV 都可以。每筆可以是：

- 一般欄位：`ticket_id`、`title`、`description`、`steps_to_reproduce`、`expected_behavior`、`actual_behavior`、`logs`、`environment`、`reporter`、`product`、`component`、`severity`、`created_at`
- ToJson 的 `unlabeled.jsonl`：`{"id", "bug_report", "meta": {"title", "direct_fields"}}`
- ToJson 的 `predicted.jsonl`：`{"id", "predicted_json"}`；直接沿用，不會重新呼叫模型
- GitHub issue 形式：`title` 與 `body`

回報者明確填寫的欄位優先，其餘欄位由 Code Llama 抽取；畫面上會用「模型抽取」標出哪些欄位來自模型。

## 已驗證與未驗證

已驗證（測試：`python3 -m unittest tests.test_integration_workflow`，18 項）：

- ToJson 的 prompt、JSON 解析與正規化程式能正確接上。測試中只把 Ollama 的 HTTP 呼叫換成假回應；Ollama 連不上時會正確顯示備援。
- DUP 的 TF-IDF 路徑，以及 SBERT 路徑上的 metadata rerank 與 stable rerank。SBERT 路徑用假的 encoder 測試，沒有載入真的 SBERT 模型。
- PRI：用 PRI 自己的 `clean_bug_reports.py` → `build_experiment_splits.py` → `build_features.py` → `train_recall_balanced_priority_model.py` 在小型合成資料上訓練模型，再透過整合層預測，P1 和 P5 都預測正確。
- 完整流程：確認重複的關卡、主系統 AssigneeTriager 的候選、Stage-3 定位到正確檔案、負責人確認後寫回歷史資料。

**Not verified**（這台開發環境沒有組內的模型與資料）：

- 真正的 `codellama:7b-instruct` 抽取品質與速度
- 組內訓練好的 SBERT 模型與 PRI 模型
- 實際 Eclipse 歷史 CSV 的 `build_features.py` 執行時間；它會重算整份歷史的特徵，資料大時可以設定 `priority.history_max_rows` 只取最近的 N 筆，但這會縮小相關報告因子的比對範圍
- paper_grade 的 bmo 負責人歷史資料

這幾項請在有產物的電腦上執行 `check`，並各跑一次範例。
