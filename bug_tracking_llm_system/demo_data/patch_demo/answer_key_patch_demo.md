# 補丁生成示範：36 題實驗的紀錄

實驗：`rewrite-gemma4-c3-20261106`（對話式改寫 + gemma4:12b，3 個候選）。這份只用來核對，不要匯入。

| 專案 | Ticket | 標題 | 那一輪的補丁 sha256 | 官方判定 |
|---|---|---|---|---|
| astropy/astropy | astropy__astropy-6938 | Possible bug in io.fits related to D exponents | `0d3510616350` | ✅ 修好 |
| astropy/astropy | astropy__astropy-7336 | units.quantity_input decorator fails for constructors with t | `7efc3ee48a15` | ✅ 修好 |
| astropy/astropy | astropy__astropy-7606 | Unit equality comparison with None raises TypeError for Unre | `5bf2266a37d9` | ✅ 修好 |
| astropy/astropy | astropy__astropy-7671 | minversion failures | `389a467ea22b` | ✅ 修好 |
| astropy/astropy | astropy__astropy-13572 | Problem in function nutation_matrix in earth_orientation.py | `c213b48ab368` | ✅ 修好 |
| astropy/astropy | astropy__astropy-14995 | In v5.3, NDDataRef mask propagation fails when one of the op | `f015aee8c76c` | ✅ 修好 |

## 解讀時要注意

- 工作台重新產生的補丁，只有 sha256 和上表完全相同時，才能引用那個官方判定；工作台會自動比對。
- 同樣的設定重跑不一定得到同一個補丁（模型輸出不是逐位元可重現），錯誤定位也是工作台重新算的。
- 這 36 題是開發集，被反覆用來調方法，不能當泛化證據。
- `official_resolved: true` 的定義是 FAIL_TO_PASS 全部通過且 PASS_TO_PASS 沒有退步，不等於「改得對」。
