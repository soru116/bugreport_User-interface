# 本機官方測試（SWE-bench harness）

把 GitHub Actions 上的官方評估搬到自己的 Windows 電腦上跑。用的是同一版 `swebench==5.0.2`、同一個資料集 `SWE-bench/SWE-bench`（test split），以及同樣的 `--max_workers 1 --timeout 1800`。

## 為什麼要包一層 Linux 容器

swebench 5.0.2 用 `Path.write_text()` 寫 `eval.sh` 和 `patch.diff`。在 Windows 原生的 Python 上，這個函式會把換行寫成 CRLF，bash 會讀不懂 `eval.sh`，測試根本沒跑，結果卻被當成「沒修好」。所以 harness 改在 `Dockerfile.runner` 建出來的 Linux 容器裡執行，再透過 `/var/run/docker.sock` 指揮 Docker Desktop 起測試容器。你不需要在 Windows 上裝 swebench。

## 一次性準備

1. 安裝 Docker Desktop，使用預設的 WSL2 後端和 Linux containers。打開 Docker Desktop，等它顯示 Engine running。
2. 磁碟空間：每個 astropy 評估映像檔大約要好幾 GB。6 題建議預留 30 GB 以上；如果還要 `save` 備份，空間大約要再加一倍。
3. 在 PowerShell 進到 `bug_tracking_llm_system`：

```powershell
cd "C:\Users\owner\Desktop\專題\bug_llm_project\bug_llm_project\bug_tracking_llm_system"
.\scripts\official_eval\official_eval.ps1 setup    # 建 runner 映像檔
.\scripts\official_eval\official_eval.ps1 fetch    # 下載 6 題的官方測試到 data\official_eval
.\scripts\official_eval\official_eval.ps1 pull     # 下載 6 題的評估映像檔
```

如果 PowerShell 擋住腳本執行，先跑一次 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。

預設的 6 題就是之前官方判定修好的那 6 題：`6938`、`7336`、`7606`、`7671`、`13572`、`14995`。之後要加題目：

```powershell
.\scripts\official_eval\official_eval.ps1 fetch -Ids astropy__astropy-6938 astropy__astropy-7336 ... astropy__astropy-12907
```

`fetch` 每次都會依 `-Ids` 重寫 `instances.jsonl`，所以要把舊題目也一起列進去。

## 先驗證環境（建議每題做一次）

```powershell
.\scripts\official_eval\official_eval.ps1 gold -InstanceId astropy__astropy-7336
```

用官方的參考補丁跑一次，結果必須是 `resolved`。如果參考補丁都不過，代表環境有問題，這時候模型補丁的判定也不能信。gold 的結果只會記在 `local_verdicts.jsonl`，`kind` 欄位是 `gold_sanity_check`，不會寫進工作台。

## 評估工作台產生的補丁

先在工作台把 Ticket 跑完第 6 步（補丁生成），然後：

```powershell
.\scripts\official_eval\official_eval.ps1 run -Ticket astropy__astropy-7336   # 單題
.\scripts\official_eval\official_eval.ps1 run-all                             # 6 題全部
```

每次評估會產生：

- `data\official_eval\runs\<run_id>\verdict.json`：完整判定，包含每個測試的結果
- `data\official_eval\local_verdicts.jsonl`：總表，每次評估一行
- `data\workspace\artifacts\<ticket>\official_local\<run_id>.json`：給工作台看的摘要，只有通過和失敗的數量，沒有測試名稱

| status | 意思 |
|---|---|
| `resolved` | FAIL_TO_PASS 全部通過，而且 PASS_TO_PASS 沒有退步 |
| `unresolved` | 補丁套上了，但測試沒有全過 |
| `patch_apply_failed` | 補丁套不上 |
| `harness_error` | harness 本身出錯，沒有產生 report。**這不是「沒修好」**，要看 `harness_stdout.txt` |

## 離線備份與還原

```powershell
.\scripts\official_eval\official_eval.ps1 save   # 映像檔存成 data\official_eval\images\*.tar
.\scripts\official_eval\official_eval.ps1 load   # 換電腦或清過 Docker 後還原
```

## 報告時的寫法

- 這是**本機用官方 harness 跑出來的判定**，跟 GitHub Actions 的判定是同一套程式和同一個映像檔。但它不是 SWE-bench 官方排行榜的提交結果。
- `resolved` 的定義跟之前一樣：F2P 全過且 P2P 沒退步，**不等於「改得對」**。
- 這 6 題是從開發集裡挑出「之前修好過」的題目，**不能拿來當修復率或泛化的證據**。

## 疑難排解

- `docker run` 掛載路徑失敗（路徑裡有「專題」這類中文）：先建一個英文磁碟代號，再從那裡執行。
  ```powershell
  subst X: "C:\Users\owner\Desktop\專題\bug_llm_project\bug_llm_project"
  cd X:\bug_tracking_llm_system
  ```
- `pull` 很慢或失敗：Docker Hub 匿名下載有次數限制，執行 `docker login` 後再試。
- `harness_error`：打開 `runs\<run_id>\harness_stdout.txt`，以及 `runs\<run_id>\logs\run_evaluation\...\run_instance.log`。
