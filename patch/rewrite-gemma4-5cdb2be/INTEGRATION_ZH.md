# 整合版使用與結果

## 2026-09-13 續跑驗收

統一 CLI、exit code、Ground Truth 後載入與官方 SWE-bench evaluator adapter 完成後，
測試總數為 229，全部通過。統一入口的真實模型回歸輸出位於
`runs/unified-smoke-20260913`：`patch_status=generated`、
`validation_status=plausible`，修改前 reproducer 失敗，修改後 reproducer 與 regression
全數通過。此電腦未安裝 Docker；`swebench` 僅存在於被 Git ignore 的專案虛擬環境。
Modal 實測均在執行測試前發生上游 infrastructure error，因此沒有偽造 resolved 結果，
`official_resolved` 維持 `null`。

219 項 tests 全部通過（包含上游 FL、本機 FIM 與新增 pipeline tests）。通用 CLI 再次使用真實 `codellama:7b-instruct` 跑完 FL → FIM → diff → supplied unittest validation。第 2 候選獲選，`patch_status=generated`、`validation_status=plausible`。修改前 reproducer 失敗，修改後 1 項 reproducer 與 2 項 regression 通過。

證據：`runs/resumed-smoke-20260913/result.json`、`evaluation.json`、`localization.json`、`selected.diff`。補丁在 `normalize_username()` 增加 `if value is None: return ""`。這是合成案例的功能驗證，尚未執行官方 SWE-bench 或真實資料集 Ground Truth 評分。

目前本機 shell 未提供 `python` 別名，可使用已確認可執行的完整路徑重跑（output 改成新的名稱）：

```powershell
Set-Location D:\bug-llm-project\patch\integrated-fixed-patch
& 'C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts/run_patch_pipeline.py --ticket runs/smoke/ticket.json --repo runs/smoke/repo --test-spec runs/smoke/test_spec.json --output runs/my-next-smoke
```

`runs/` 已加入 Git ignore，避免意外提交測試 repository。示範腳本的 `localization.json` 已修正為儲存實際 FL 結果；舊 `runs/smoke/localization.json` 是歷史人工 fixture，舊 run 的真實 FL 證據請讀 `runs/smoke/pipeline_output/localization.json`。最新續跑一律讀 `runs/resumed-smoke-20260913/localization.json`。

本專案以 fixed-patch main `2a9524601d5afbc5e307ceff8868bdc9582f32e3` 為基底，保留 Git 歷史與上游 FL，加入本機 `src/fim_patch`。原本 workspace 程式仍保留。此次尚未 push 到 GitHub。

## 執行

需求：Python 3.10+、Git、Ollama，已安裝 `codellama:7b-instruct`。預設 TF-IDF FL 與 FIM 核心使用標準函式庫。

在此專案目錄執行一次完整示範（真實 FL、真實模型、人工合成 bug 與測試）：

```powershell
python scripts/smoke_repair_pipeline.py
```

示範自行建立 base Git repository 與 ticket/test spec，輸出至 `runs/smoke/pipeline_output`。如果 `runs/smoke/repo` 已存在，會拒絕覆寫；可使用下列通用 CLI 指向既有輸入與新的 output。

```powershell
python scripts/run_patch_pipeline.py --ticket runs/smoke/ticket.json --repo runs/smoke/repo --test-spec runs/smoke/test_spec.json --output runs/another-run
```

目前只有一個正式 CLI 實作：`fim_patch.cli`。下列啟動方式都會走同一個
`RepairPipeline`，不再接受可繞過 adapter 的歷史 `--localization` / `--system-src` 路徑：

```powershell
python scripts/run_patch_pipeline.py --help
# pip install -e . 後，另可使用：
python -m fim_patch --help
patch-pipeline --help
```

`--output` 必須是 repository 外尚不存在的資料夾。省略 `--test-spec` 表示只檢查 syntax/scope/apply，結果不能視為修復成功。測試會執行受信任 repository 程式；目前為本機 unittest runner。

可選參數：`--model`、`--url`、`--candidates`、`--num-ctx`、`--max-new-tokens`、`--research-mode`、`--local-context`。研究模式保留原本低信心與人工審查旗標。

CLI exit code：`0` 僅代表 supplied tests 為 `plausible`，或官方 SWE-bench 判為
resolved；`2` 代表已產生 patch、但沒有功能驗證；`1` 代表阻擋、失敗或官方 unresolved。

## Ground Truth 與隔離評估

`--ground-truth <json/jsonl>` 只在 patch 生成完成後才讀取 reference row，避免 developer
patch 洩漏進 FIM prompt。資料列接受 `instance_id` 或 `ticket_id`、`base_commit`、`patch`，
以及可選的 `verified` / `merged_commit`。輸出 `ground_truth.json` 記錄 patch SHA256、完全
相同與修改檔案交集；這些是描述性指標，不能代替功能測試。

合成 smoke 可加上 `--ground-truth examples/smoke_ground_truth.json` 驗證後載入流程；該
fixture 明確標示為 synthetic，不是 SWE-bench 或實際合併 commit。

正式成功判讀使用 SWE-bench 官方 Docker harness：

```powershell
python scripts/run_patch_pipeline.py --ticket CASE.json --repo REPO --output runs/CASE-RUN `
  --official-eval --dataset-name SWE-bench/SWE-bench_Lite --split test `
  --run-id CASE-RUN-001 --max-workers 1 --swebench-python C:\path\to\python.exe
```

執行環境須先安裝 `swebench` 與 Docker；也可加 `--modal` 使用官方 Modal 路徑。每個不同
patch 必須使用新的 `--run-id`，因為官方 harness 會以 run ID 快取結果。CLI 讀取
`official_harness/logs/run_evaluation/<run-id>/results.json`，輸出
`official_evaluation.json`，並回寫 `evaluation.json.official_resolved`。

本機 `UnittestValidator` 只允許受信任的合成／自有 repository；它不是安全 sandbox。
第三方資料集 repository 一律使用 `--official-eval`。官方說明：
https://github.com/SWE-bench/SWE-bench/blob/main/docs/reference/harness.md

SWE-bench 5.x 的 Modal 路徑使用 `SWE-bench/...` dataset（包含 `image`、`eval_script` 等
task schema）。歷史 `princeton-nlp/...` dataset 缺少這些欄位，不可直接交給 5.x Modal evaluator。

### 不安裝本機 Linux / Docker 的替代方式

已實測 Modal 兩條官方版本路徑：5.0.2 因 `TestSpec` 缺少
`setup_env_script` 而失敗；4.1.0 雖建立完整 Flask 雲端映像，仍因 Modal 已停用 legacy
Sandbox filesystem API 而在測試前失敗。兩者都屬 `error`，不可判為 `unresolved`。

專案因此提供 `.github/workflows/swebench-official-evaluation.yml`。它使用 GitHub-hosted
`ubuntu-24.04` 與 Docker 執行固定版 `swebench==5.0.2`，Linux 映像不會下載到本機。
在真正的 GitHub repository 開啟 Actions，手動執行 **SWE-bench official cloud evaluation**，
填入 dataset、split、prediction path（或先用 `gold`）、instance ID 與唯一 run ID；完成或
失敗都會上傳 `logs/run_evaluation` 與 JSON report。現在 `origin` 是本機路徑，workflow 已建立
但尚未 push，所以本次不能直接觸發 GitHub-hosted runner。

## 整合方式

`scripts/run_patch_pipeline.py` → `RepairPipeline.run()` → clean base snapshot → `build_code_index()` → `localize_ticket(symbol_localization=True, b1-structured, coverage-aware-v1)` → `localization_for_patch()` → `FIMPatchGenerator.generate()` → optional `UnittestValidator` → `write_run()`。

Adapter 位於 `src/fim_patch/pipeline.py`，輸出 `schema_version=LocalizationForPatchV1`，帶 index 的 repo/base_commit/source SHA256 與原始 confidence。它只適用於此次剛建立的 index/FL；不為任意歷史 JSON 捏造 confidence/hash。上游 BugLocalizer 原有預設不變，整合入口明確啟用 Stage 3。

`src/fim_patch` 保留原 package 名稱，避免破壞既有 imports/tests；它就是專案的 Patch Generation 模組。原有大型 data/reports 是上游追蹤內容，保留 provenance；LFS 大型索引沒有下載，整合入口會從目標 source 建索引。原有 data 裡的 index 不應直接假定可用。

## 輸出與判讀

| 檔案 | 內容 |
|---|---|
| ticket.json / localization.json | 輸入與本次實際 Stage-3 結果、hash、confidence |
| candidates.json / result.json | 候選、prompt、raw response、拒絕原因、選擇結果 |
| selected.diff | 可套用的選定 patch；無候選為空 |
| evaluation.json | supplied tests 的 before/after/regression 與解釋 |
| predictions.jsonl | instance_id、model_name_or_path、model_patch |
| ground_truth.json | 生成後才載入的 reference patch provenance 與描述性比較 |
| official_evaluation.json | 官方隔離 harness 的 resolved / unresolved 判讀與原始報告 |

`generated + plausible`：通過指定測試，仍需審查。`generated + not_run`：只通過靜態/apply gates。`blocked_low_confidence`：FL gate 擋下。`no_valid_candidate`：候選未通過所需 gates。只有官方 harness 的 resolved 結果會把 `official_resolved` 設成 `true`；supplied unittest 不會。

## 驗證

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
python -m unittest discover -s tests
```

請在此專案根目錄跑測試；在外層 patch 目錄執行會讓上游測試的 scripts import 指到其他目錄。

新增測試 `tests/test_repair_pipeline.py` 使用真實 FL 與可控 backend，驗證修補前後測試、source 保持乾淨、輸出一致性、拒絕覆寫結果、拒絕 dirty source，以及 adapter 不製造 confidence。

下一階段：固定真實 dataset cases，加入官方 SWE-bench evaluator 與 reference provenance。此次完成的是單一整合入口與 synthetic end-to-end，沒有把這次 smoke 當成真實資料集修復率。
