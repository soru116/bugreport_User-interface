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
3. 左側清單的「等你確認」會列出需要判斷是否重複或確認負責人的 Ticket。
4. 點進一張 Ticket 後，上方的進度列會一直固定在畫面上，顯示五個步驟各自的狀態。每個步驟是一個分頁：點進度列上的步驟就能切換，也可以用鍵盤左右鍵。畫面會自動停在需要你處理的那一步；你自己點過分頁後，就不會再自動跳走。
5. 每個步驟的分頁底部都會寫出使用的模組、程式路徑、耗時；用了備援時也會寫出原因。
6. 右上「更多」裡可以下載完整結果 JSON、從頭重新執行或刪除 Ticket。

範例資料是**示範用的虛構 Ticket**，內容描述 `bug-duplicate-detection` 原始碼中可能出現的錯誤，搭配設定檔中的 `duplicate-detection` 專案示範錯誤定位。它們不是評估資料，不能拿來算準確率。

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

已驗證（測試：`python3 -m unittest tests.test_integration_workflow`，11 項）：

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
