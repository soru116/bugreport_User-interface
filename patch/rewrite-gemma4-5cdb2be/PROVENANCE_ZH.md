# 補丁生成（對話式改寫 + gemma4:12b，官方修復 6/36 的版本）

這個資料夾是補丁生成組的程式碼快照，給 Ticket 工作台的「產生補丁」步驟使用。

| 項目 | 值 |
|---|---|
| 來源 repository | `soru116/fixed-patch` |
| 分支 | `codex/eval36-v7-timeout-flag-20261019` |
| commit | `5cdb2be79ca86e38459070d408e4c5e62e6ce5a4` |
| 對應實驗 | `experiments/eval36_v7/results/rewrite-gemma4-c3-20261106/` |
| 結果 | 36 題開發集，21 題產生補丁，**官方 SWE-bench harness 判定修復 6 題** |

修復的 6 題：`astropy__astropy-6938`、`7336`、`7606`、`7671`、`13572`、`14995`。

## 複製了什麼

| 路徑 | 內容 |
|---|---|
| `src/fim_patch/` | 補丁生成模組本體（原封不動） |
| `src/utils/`、`src/modules/` | 同一個 commit 的錯誤定位與快照工具（`utils/repo_snapshot.py` 用來建立 base_commit 的 git 快照） |
| `scripts/run_stage4_batch.py` | 36 題批次執行入口（產生 6/36 的那支） |
| `scripts/` 其他 | 單一入口、改寫冒煙測試、補丁審查與閘門稽核 |
| `tests/` | 原 repository 的測試 |
| `experiments/eval36_v7/` | 36 題 tickets、凍結的錯誤定位、`run_config.json`、紀錄與補丁 |
| `.../rewrite-gemma4-c3-20261106/official/` | 本次新增：官方判定 `verdict.json`（從組員電腦的 `runs/official/` 複製，原 commit 尚未提交） |
| `.../rewrite-gemma4-12b-20261024/` | 上一輪（1 候選，3/36）的紀錄與官方判定，`7606` 的判定在這裡 |
| `official_verdicts.jsonl` | 本次新增：上面兩輪共 37 個官方判定的索引，以 `patch_sha256` 查詢 |

沒有修改任何複製來的程式碼。原本 9 月的 `patch/integrated-fixed-patch/` 保留不動。

## 6/36 的設定

工作台照 `rewrite-gemma4-c3-20261106/run_config.json` 執行，只改了「題目來源」：錯誤定位改用工作台自己剛跑出的結果，而不是凍結的 `localizations.jsonl`。

| 參數 | 值 |
|---|---|
| generation_mode | `rewrite`（對話式改寫整個函式） |
| model | `gemma4:12b`（Google；符合不用中國模型的守則） |
| candidates | 3（seed 42／43／44，同一個 prompt） |
| temperature | 0.2 |
| num_ctx／max_new_tokens | 16384／2048 |
| target_policy | `first_supported_ast` |
| max_targets | 1 |
| min_body_retention | 0.5 |
| prompt_layout | `task_last` |
| research_mode | true（錯誤定位信心低也產生，但會標示） |

## 解讀限制（必須跟數字一起講）

1. **分母是 36，不是 21。** 沒產生補丁的 15 題也是沒修好的題目。
2. **這 36 題是開發集**，反覆用來調方法，不能當泛化證據。
3. **`official_resolved: true`** 的定義是 FAIL_TO_PASS 全部通過且 PASS_TO_PASS 沒有退步，不等於「改得對」。
4. **不是逐位元可重現。** 同 seed、同 prompt，換機器或換 Ollama 版本仍可能得到不同補丁。工作台重跑得到的補丁，只有 `patch_sha256` 和上表某個判定完全相同時，才能引用那個官方判定。
5. 工作台本機沒有 Docker，不會自己跑官方測試。
