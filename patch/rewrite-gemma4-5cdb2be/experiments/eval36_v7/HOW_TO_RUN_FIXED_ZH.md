# 修正後的 36 題：手動執行

> 最新 Issue 整理版本請使用 [ISSUE_COMPACT_V1_ZH.md](ISSUE_COMPACT_V1_ZH.md) 的新輸出目錄。下方 scope-contract 指令是上一版紀錄，請勿使用最新程式續跑舊目錄。

兩項修正：Middle 包含 signature 時，prompt 要求完整函式；docstring 增加／刪除不能單獨算作修補邏輯。
新 prompt 版本含 `scope-contract-v1`。此版本尚未重跑 36 題，因此沒有新的生成率／修復率。
以前的 `results/v7-task-last-20261003/` 是修正前的歷史結果，保留不覆寫。

## 你的 Windows 電腦

開啟 PowerShell，貼上：

```powershell
$taskPython = 'C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
Set-Location -LiteralPath 'D:\bug-llm-project\patch\fixed-patch-latest'
& $taskPython experiments/eval36_v7/run.py --check-only
```

預檢只讀取固定案例、雜湊與 Ollama 模型清單，不生成補丁。
請先啟動 Ollama；需要原來的 `qwen2.5-coder:14b-base` 及相同 digest，腳本會檢查。

真正開始 36 題時，再貼上這一行：

```powershell
& $taskPython -u experiments/eval36_v7/run.py --repo-cache-dir 'D:\bug-llm-project\patch\fixed-patch\data\repositories' --snapshot-cache-dir 'D:\bug-llm-project\patch\fixed-patch\data\repositories\_stage4_snapshots' --output 'D:\bug-llm-project\patch\fixed-patch-latest\runs\eval36-v7-scope-contract-v1'
```

這支程式呼叫本機 Ollama；主要消耗本機 GPU／CPU 時間與電力。它沒有使用 OpenAI API。
上一批耗時約 12 分 32 秒，這次時間可能不同。每次模型呼叫逾時 300 秒，不是整批時限。
參數維持模型、temperature 0.2、seed 42、每題 1 candidate、context 16384、輸出上限 4096 與原定位。
實際每題輸出 budget 仍由函式大小計算。不要同時另跑其他模型批次。

## 中斷與續跑

要中斷時按 Ctrl+C。之後使用相同指令，加上 `--resume`：

```powershell
& $taskPython -u experiments/eval36_v7/run.py --repo-cache-dir 'D:\bug-llm-project\patch\fixed-patch\data\repositories' --snapshot-cache-dir 'D:\bug-llm-project\patch\fixed-patch\data\repositories\_stage4_snapshots' --output 'D:\bug-llm-project\patch\fixed-patch-latest\runs\eval36-v7-scope-contract-v1' --resume
```

續跑會略過已有紀錄的題目，不會自動重試已記錄的錯誤。不要變更參數，也不要用修正前結果目錄續跑。

## 看結果

完成後：

```powershell
Get-Content -LiteralPath 'runs\eval36-v7-scope-contract-v1\summary.json'
& $taskPython experiments/eval36_v7/compare.py --run runs/eval36-v7-scope-contract-v1 --output runs/eval36-v7-scope-contract-v1/comparison.json
```

重點是 generated／36、拒絕原因、新增與流失的案例。新驗證器比較嚴格，不能把計數變少直接當成模型退步。
官方測試尚未執行時，`official_resolved=null`，不是修復數為 0。

## 組員的其他電腦

取得最新 `codex/eval36-v7-20261003` 分支後，在專案根目錄：

```sh
python experiments/eval36_v7/run.py --check-only
python experiments/eval36_v7/run.py --clone-missing --fetch-missing-commits
```

已有快取可加 `--repo-cache-dir /path/to/repositories`；下載 Astropy repository 可能額外耗時。
官方測試在具備 Docker 與 SWE-bench harness 的環境執行：

```sh
python experiments/eval36_v7/evaluate_official.py --predictions runs/eval36-v7-scope-contract-v1/predictions.jsonl --run-id eval36-v7-scope-contract-v1
```

## Git 擁有者檢查

執行入口目前會針對固定 cohort 的快取與快照，設定僅限子程序的精確 `safe.directory`，不改全域 Git 設定。
先前手動批次的 16 題 snapshot 錯誤已另備續跑資料夾，保留原批次與已處理的 20 題，沒有重跑模型。
已驗證 16 份快照的 HEAD 正確、續跑設定相同。只補跑這 16 題時執行：

```powershell
& $taskPython -u experiments/eval36_v7/run.py --repo-cache-dir 'D:\bug-llm-project\patch\fixed-patch\data\repositories' --snapshot-cache-dir 'D:\bug-llm-project\patch\fixed-patch\data\repositories\_stage4_snapshots' --output 'D:\bug-llm-project\patch\fixed-patch-latest\runs\eval36-v7-scope-contract-v1-retry16' --resume
```

如果出現 `dubious ownership`，先用同一個 Windows 使用者執行 PowerShell，並確認快取是你自己的資料夾。
不要把 `safe.directory` 設成 `*`。若批次只顯示 base_commit unavailable，可直接診斷：

```powershell
git -C 'D:\bug-llm-project\patch\fixed-patch\data\repositories\astropy__astropy' rev-parse HEAD
```

保存錯誤後先處理環境；不要把全部 snapshot error 的批次當作模型生成結果。
