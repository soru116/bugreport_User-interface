# 官方判定：rewrite + gemma4:12b 在固定 36 題修復 3 題

- 日期：2026-10-25
- run：`experiments/eval36_v7/results/rewrite-gemma4-12b-20261024/`
- 判定來源：`.github/workflows/swebench-official-evaluation.yml`（GitHub Actions，pinned `swebench==5.0.2`）
- 證據：本資料夾的 `official/<run_id>/verdict.json` 與 `report.json`

**這是本專案第一次取得 `official_resolved`。** 在此之前每一個 run 的該欄位都是 `null`。

---

## 一、結果

| 方法 | 模型 | generated | **official resolved** |
|---|---|---:|---:|
| FIM | qwen2.5-coder:14b-base（`results/v7-task-last-20261003`） | 2 | **0 / 36** |
| FIM | codellama:13b-code（`results/codellama13b-local-20261020`） | 0 | — 無候選可送 |
| **rewrite** | **gemma4:12b** | **17** | **3 / 36** |

**修復：`astropy__astropy-7336`、`astropy__astropy-7606`、`astropy__astropy-7671`。**

17 個補丁全數送出並取得判定，無 harness 錯誤。

**v7 那 2 個 FIM 候選（`13462`、`7606`）在 10/03 產生後從未送過官方測試，本次補送，兩題皆 failed。** 所以 FIM 那條線在 resolved 層級是確定的 0，不是未知。

`baselines/external-patch-36-20260929` 的 3 個 qwen FIM 生成沒有 `predictions.jsonl`，若要補齊需從 `stage4_batch_records.jsonl` 重建。

---

## 二、逐題判定

| 題目 | resolved | 事前人工判定 |
|---|---|---|
| 7336 | ✅ | 合理 |
| 7606 | ✅ | 很可能正確 |
| 7671 | ✅ | 很可能正確 |
| 6938 | ❌ | 很可能正確 ← 判斷錯誤 |
| 13668 | ❌ | 合理 |
| 14163 | ❌ | 方向對但粗糙 |
| 14590 | ❌ | 合理 |
| 13404 | ❌ | 有瑕疵（重複貼既有註解） |
| 13477 | ❌ | 修錯方向（`__eq__` 應回 `NotImplemented`） |
| 13638 | ❌ | 空改動（if/else 兩分支內容相同） |
| 14182 | ❌ | 修反了（把使用者的 `header_rows` 丟掉） |
| 14213 | ❌ | 可疑（histogram 特例化，等於忽略單位） |
| 14253 | ❌ | 幾乎空改動（`elif hasattr(obj,"data")` 永遠成立） |
| 14539 | ❌ | 很可能噴 AttributeError |
| 14598 | ❌ | 改錯檔案（vendored `astropy/extern/configobj/`） |
| 7008 | ❌ | 會讓 `astropy.constants` 一 import 就壞掉 |
| 7746 | ❌ | 不完整（只短路 `wcs_pix2world`） |

---

## 三、7606：方法之間最乾淨的對照

同一題、同一批 36 題、同一份凍結定位、同一個 harness：

| 生成方法 | 補丁大小 | sha256 前 12 碼 | 判定 |
|---|---:|---|---|
| FIM（qwen2.5-coder:14b-base，v7） | 498 bytes | `069173c0aa47` | ❌ failed |
| rewrite（gemma4:12b） | 329 bytes | `5bf2266a37d9` | ✅ **resolved** |

兩個不同的補丁，唯一的差別是生成方法。rewrite 版就是最小修法：

```python
 def __eq__(self, other):
+        if other is None:
+            return False
         other = Unit(other, parse_strict='silent')
         return isinstance(other, UnrecognizedUnit) and self.name == other.name
```

---

## 四、人工 diff 審查的準確度：一個明確的不對稱

送測前對 17 個補丁做過人工審查（`PATCH_REVIEW.md`），分三層：

| 事前判定 | 題數 | 實際 resolved |
|---|---:|---:|
| 很可能正確 | 3 | 2 |
| 合理 | 4 | 1 |
| 垃圾／修錯／會壞掉 | 10 | **0** |

**被否決的 10 題全部 failed（10/10）；看好的 7 題中 3 題。**

**人工 diff 審查可以可靠地剔除無效候選，但不能預測成功。** 實務上可用來排送測順序、節省 Actions 額度，不可用來宣稱修復數。

誤判最明顯的是 `6938`（把 `output_field.replace()` 的結果改為賦值並移進迴圈，事前認定「就是真正的修法」）。建議用 workflow 的 `diagnostic_traceback` 選項重送一次，釐清測試實際報什麼。

---

## 五、報告數字時必須一起講的限制

1. **分母是 36，不是 17。** 沒有產出補丁的 19 題同樣是這個方法沒修好的題目。
2. **這 36 題是開發集**，被反覆用於調整方法，**不能當泛化證據**。
3. **`official_resolved: true` 的定義**是 FAIL_TO_PASS 全部通過且 PASS_TO_PASS 無退步，**不等於「改得對」**。
4. **rewrite 與 FIM 之間不只生成方法不同**：`--target-policy` 由 `related_targets_top5` 改為 `first_supported_ast`（rewrite 僅支援單一目標策略）、`--max-new-tokens` 由 4096 改為 2048、採樣沿用 gemma4 自身的 `top_k` 預設。不是單變數對照。
5. 一個具體代價：`14598` 在 FIM 下的目標是 `card.py::_pad`，在 `first_supported_ast` 下變成 vendored 的 `configobj`。**目標策略的改變不是全面有利。**

---

## 六、與 devstral 線的交集（給組員）

`gh run list` 顯示同期有 12 個 `devstral36-*` 評估，同樣在這批 36 題上，且有 resolved。

**`devstral36-8b74112e74764e5492a020370d71665a` 的補丁 sha256 與本次 rewrite 的 `7606` 完全相同**（`5bf2266a37d9`）。兩個不同模型獨立產出同一個兩行修法，互為佐證。

兩條線各自有數字，**報告前請先對齊**，否則會出現兩個互相矛盾的「36 題修復數」。需要確認：生成方法、devstral 的確切 tag、分母與定位是否同一份。

---

## 七、重現步驟

送出（預設乾跑，需 `-Dispatch` 才會送）：

```powershell
.\scripts\Submit-OfficialEvaluation.ps1 -Predictions experiments/eval36_v7/results/rewrite-gemma4-12b-20261024/predictions.jsonl -Only astropy__astropy-7606
.\scripts\Submit-OfficialEvaluation.ps1 -Predictions experiments/eval36_v7/results/rewrite-gemma4-12b-20261024/predictions.jsonl -Limit 5 -Dispatch
```

收集：

```powershell
.\scripts\Collect-OfficialVerdicts.ps1 -Predictions experiments/eval36_v7/results/rewrite-gemma4-12b-20261024/predictions.jsonl
```

**收集腳本以 `patch_sha256` 比對 predictions 來認定哪些判定屬於本次實驗，不看 run 名稱。** 這個 repo 是共用的：其他人 dispatch 同一個 workflow，他們的 run 出現在同一份 `gh run list`、artifact 落在同一個資料夾。名稱是約定，雜湊是補丁本身。

早期版本以名稱前綴過濾，而 Windows PowerShell 的 `ConvertFrom-Json` 將 JSON 陣列當成單一物件送入管線，`-like` 對陣列執行的是篩選、只要一個元素符合即為真，於是整批原封不動通過。**過濾等於沒有執行，而且不會報錯**，一度把其他人的 run 併入統計而錯報為 8 題修復。這與 `13462`（通過閘門但只改 docstring）、`7008`（通過全部閘門但會讓模組無法 import）屬於同一類風險：沉默地給出看起來合理的錯誤答案。
