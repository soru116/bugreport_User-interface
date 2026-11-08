# 目前固定評估對象：36 個 Astropy 案例

這 36 題暫定為目前的 development 評估對象。它們已用於調整方法，不能宣稱為未見過的 holdout。
案例 ID 與 2026-09-21、2026-09-29 的兩批 36 案完全一致；採用 9/29 凍結的**預測定位**。

最新共用 FIM 入口、manifest 與离線驗證見 [UNIFIED_REPAIR_ZH.md](UNIFIED_REPAIR_ZH.md)。預設只規劃；domain adapters 關閉；每題所有洞與重試共用最多三次 infill。此入口尚未真實生成／官方評估。

泛化實驗的離線 parser 配對、跨 repo 候選清單、seed 43 對照與待辦，見 [泛化實驗工作台](../generalization_v1/README_ZH.md)。24 題的預測 Stage-3、source snapshots 與離線規劃已補齊，見 [定位結果](../generalization_v1/LOCALIZATION24_ZH.md)；尚未進行新模型／官方評估。

## 資料

- `ticket_ids.txt`：固定的 36 題及順序。
- `tickets.jsonl`：公開 issue、repository、base commit。已移除 reference patch、test patch、測試答案與本機絕對路徑。
- `localizations.jsonl`：相同案例的既有 FL 預測與 source hash；未重新定位，也未換成 gold。
- `manifest.json`：來源、模型 digest、參數與輸入 SHA-256。
- `baselines/`：既有設定、逐題結果與摘要；不是 v7 的結果。

## 執行 v7

目前程式已修正輸出範圍契約與 docstring 驗證，版本標記含 `scope-contract-v1`。
修正後 36 題尚未執行；Windows 的完整操作與續跑方式見 [HOW_TO_RUN_FIXED_ZH.md](HOW_TO_RUN_FIXED_ZH.md)。
新預設輸出是 `runs/eval36-v7-scope-contract-v1`，以下歷史結果仍為修正前 v7。

分段空間量測可使用 `measure_prompt_parts.py --repo-cache-dir <repositories>`，預設讀取補跑完成的 `runs/eval36-v7-scope-contract-v1-retry16`。
此工具只離線重建與驗證保存的 FIM prompt，不呼叫模型；輸出 bytes／字元／行數，不宣稱是分段實際 token 數。

在專案根目錄執行（Python 3.10+、Git、可用的 Ollama）：

```sh
python experiments/eval36_v7/run.py --check-only
python experiments/eval36_v7/run.py --clone-missing --fetch-missing-commits
```

已有 Astropy Git 快取時可指定路徑，避免重新下載：

```sh
python experiments/eval36_v7/run.py --repo-cache-dir /path/to/repositories
```

快取應包含 `astropy__astropy` Git repository。每題建立 base commit 的獨立 snapshot，不修改原始快取。
中斷後使用相同指令加上 `--resume`；不要將不同 layout 寫進同一個結果目錄。

設定固定為 Qwen 14B **base**、temperature 0.2、seed 42、1 candidate、context 16384、輸出 4096 tokens，對齊 9/29 基線。
此批次只改為目前 v7 prompt 套件；不增加官方測試提示、不增加修補輪數。
一次完整執行約 30–120 分鐘，依 GPU、候選與逾時而異；300 秒是每次模型呼叫的逾時，不是整批上限。

同版本 layout 對照可另跑：

```sh
python experiments/eval36_v7/run.py --prompt-layout legacy --output runs/eval36-v7-legacy
```

注意：目前程式的 legacy 不是歷史 prompt 的逐字還原。歷史 vs v7 的比較包含清理文字、docstring 與指令等多項改動，不能只歸因於指令位置。

## 官方測試交接

生成完成後，將 `runs/eval36-v7-task-last/predictions.jsonl` 交給具備 Docker 的 SWE-bench 官方 harness 環境：

```sh
python -m swebench.harness.run_evaluation --dataset_name SWE-bench/SWE-bench --split test --predictions_path /absolute/path/to/predictions.jsonl --max_workers 1 --run_id eval36-v7-task-last
```

建議直接使用資料夾內的入口，會限制官方報告範圍為固定 36 題並檢查 prediction ID：

```sh
python experiments/eval36_v7/evaluate_official.py --predictions runs/eval36-v7-scope-contract-v1/predictions.jsonl --run-id eval36-v7-scope-contract-v1
```

使用 SWE-bench **Full**，不要改成 Lite；無補丁的案例仍計入固定 36 題分母。
保存 harness 版本、每題 report、test output、失敗／環境錯誤及 resolved ID。
官方新增 tests 保留給驗證器，不放入這次生成 prompt。
指令介面參考 [SWE-bench 官方評估指南](https://www.swebench.com/SWE-bench/guides/evaluation/)。

## 評估與回傳

| 批次 | Generated / 36 | 官方修復 |
|---|---:|---|
| 9/21 Qwen | 1 | 批次摘要未提供 |
| 9/29 Qwen（主要歷史基線） | 3 | 批次摘要未提供 |
| 9/29 CodeLlama 13B | 0 | 批次摘要未提供 |
| v7（2026-10-03 實跑） | **2** | 待官方 harness 驗證 |

`generated` 只代表通過目前語法／範圍／套用閘門。`validation_plausible=0` 且測試未執行，不能解讀為官方修復失敗。
比較同案例新成功／新失敗、generated/36、official resolved/36、逾時、context budget、拒絕原因、token 與耗時。
請回傳完整 run 目錄及官方 reports；結果可壓縮共享，不需修改此固定輸入。

完整批次結束後產生逐題配對比較（未跑完會拒絕計算）：

```sh
python experiments/eval36_v7/compare.py --run runs/eval36-v7-scope-contract-v1 --output runs/eval36-v7-scope-contract-v1/comparison.json
```

## 已完成的本機 v7 結果

完整紀錄保存在 [results/v7-task-last-20261003](results/v7-task-last-20261003/)，分析見 [RESULTS_ZH.md](RESULTS_ZH.md)。
組員可直接測試此預測檔，不必先重跑模型：

```sh
python experiments/eval36_v7/evaluate_official.py --predictions experiments/eval36_v7/results/v7-task-last-20261003/predictions.jsonl
```
# 本機最新 Issue 整理

自動補末尾換行已接入；最多 3 輪 FIM 修復及官方測試回饋可明確啟用，詳見 [AUTOMATIC_REPAIR_ZH.md](AUTOMATIC_REPAIR_ZH.md)。未重跑模型，完整 Docker 迴圈尚未在本機實跑。

Token 預算量測已確認舊 bytes 門檻誤攔截 7 題。新版採明確啟用的 `--tokenizer-json`，35 題有 prompt 的案例全部符合 token 容量；詳見 [TOKEN_BUDGET_V1_ZH.md](TOKEN_BUDGET_V1_ZH.md)。尚未重跑模型，生成率仍未驗證。

最新 task_last Issue renderer 加入 `issue-compact-v1` 版本標記，保留 FIM；預設生成輸出目錄是 `runs/eval36-v7-issue-compact-v1`。本次只做離線量測與測試，沒有模型重跑結果。說明與手動指令見 [ISSUE_COMPACT_V1_ZH.md](ISSUE_COMPACT_V1_ZH.md)。不要將舊結果當成新版結果，也不要以新程式 resume 舊目錄。


GitHub Actions 雲端官方測試已新增 opt-in adapter（`--repair-cloud-repo`／`--repair-cloud-ref`），保留本機 FIM 與最多三次總生成限制；部署與保存候選測試指令見 [CLOUD_OFFICIAL_ZH.md](CLOUD_OFFICIAL_ZH.md)。只通過離線驗證，尚未部署或完成真實官方測試。


有效候選生成與測試接受已分開記錄（static-candidate-accounting-v1）；未通過測試的靜態有效候選保存於 generated_candidates，詳見 [GENERATION_ACCOUNTING_ZH.md](GENERATION_ACCOUNTING_ZH.md)。此為紀錄修正，不代表模型能力提升；後續使用新輸出目錄。

局部 FIM 已新增 opt-in `--local-fim --prompt-layout compact_fim`，固定簽名，只填有公開線索的完整 AST 小區塊，歧義時自動回退。固定 36 題離線量測與三題生成指令見 [LOCAL_FIM_ZH.md](LOCAL_FIM_ZH.md)；尚未呼叫真實模型驗證本次生成率。

生成起點修正已新增 opt-in `--local-fim-expressions`（需搭配 local_fim／compact_fim）：保留 docstring／程式骨架，只填 AST 運算式或條件。新版本與 12962 單題指令見 [LOCAL_FIM_EXPRESSION_ZH.md](LOCAL_FIM_EXPRESSION_ZH.md)；未自行呼叫模型，定位限制與真實生成效果仍需另驗證。

需求／範圍相容性的離線規劃器與兩洞 FIM 設計見 [SCOPE_COMPATIBILITY_ZH.md](SCOPE_COMPATIBILITY_ZH.md)。36 題僅 12962 偵測到明確新增參數方案與固定簽名的衝突；其餘題目仍未證明語意相容。規劃器不生成補丁；簽名＋行為的兩洞執行器已以 opt-in 接入，見下方操作說明。

兩洞 FIM 執行器已接入（opt-in）：[範圍自動調整操作說明](SCOPE_EXECUTION_ZH.md)。只完成離線受控驗證，未證明模型生成率或修復率提升。

簽名洞起點 v2 已修正：固定 separators／keyword-only `*`，公開需求保留完整提議與呼叫，使用新輸出目錄隔離 v1；見 [SIGNATURE_ANCHOR_V2_ZH.md](SIGNATURE_ANCHOR_V2_ZH.md)。真實 v2 生成尚未驗證。

簽名邊界 v3 已接入自動擷取：v2 保存回應可通過 signature gate 並進入行為洞；維持完整原始輸出與三次生成上限。離線證據見 [SIGNATURE_BOUNDARY_V3_ZH.md](SIGNATURE_BOUNDARY_V3_ZH.md)。v3 真實試跑簽名成功、行為失敗，0／1 有效補丁。

行為回饋 v4 已接入：重試包含 no-op、未引用新增參數與公開預期結果；v3 三次保存回應已離線重播，不新增模型呼叫或補丁。操作及限制見 [BEHAVIOR_FEEDBACK_V4_ZH.md](BEHAVIOR_FEEDBACK_V4_ZH.md)。使用者完成 v4 單題試跑，回饋已送達但仍為 0／1 有效補丁，見 [v4 結果分析](../../runs/eval1-codellama13b-scope-adjustment-v4/ANALYSIS_ZH.md)。

單元素洞 v5 已接入兩洞分支：host 保留單元素容器括號，LLM 只填元素；首次行為就附公開預期，精簡摘要保留完整必要證據。離線實作見 [INNER_ELEMENT_V5_ZH.md](INNER_ELEMENT_V5_ZH.md)。使用者完成单題試跑：1／1 靜態候選，行為以 EOT 結束，但新增參數未使用、需求未落實，官方測試未執行；見 [v5 真實結果分析](../../runs/eval1-codellama13b-scope-adjustment-v5/ANALYSIS_ZH.md)。

靜態候選保留 v6 已接入：有公開預期與參數未直接使用的提示時，可使用剩餘一次行為重試；第一份靜態候選保留。入口及離線證據見 [STATIC_OPTION_RETRY_V6_ZH.md](STATIC_OPTION_RETRY_V6_ZH.md)。使用者已完成一次真實續跑，回饋有送達但補丁與 v5 完全相同，唯一靜態候選仍為 1；需求未落實，官方測試未執行。詳見 [v6 真實結果分析](../../runs/eval1-codellama13b-scope-adjustment-v6-saved-retry/ANALYSIS_ZH.md)。
