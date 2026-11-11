# astropy 完整示範：答案

這份只用來核對，**不要匯入**。

歷史：5559 筆 astropy GitHub issue（2023-07-01 以前建立），其中 2176 筆有負責人、149 筆被標成重複。已移除新 Ticket 自己的 issue，以及 SWE-bench 驗證集／保留測試集的 43 筆 issue。

## 重複偵測（第 2 步）

| Ticket | 標題 | 真正重複的對象 | 依據 |
|---|---|---|---|
| astropy#11571 | Frame attributes in sky coordinates do not obey Numpy broadc | astropy#8812：BUG: Frame attributes in sky coordinates do not obey Numpy b | pllim: "duplicate of #8812" |
| astropy#11104 | Cannot use negative distance in spherical representation des | astropy#8091：Constructing a SphericalRepresentation object with a negativ | adrn: "duplicate of #8091" |
| astropy#9412 | Cutout2D does not work correctly with NDData objects | astropy#9173：When an NDData is passed to Cutout2D, it should use the wcs | bsipocz: links issues/9173 |

## 負責人、錯誤定位、補丁（第 4–7 步）

這 6 張不是重複（重複偵測請按「不是這張」）。

| Ticket | 標題 | 修好它的人 | 真正修改的檔案 | 補丁實驗官方判定 |
|---|---|---|---|---|
| astropy__astropy-6938 | Possible bug in io.fits related to D exponents | saimn | astropy/io/fits/fitsrec.py | ✅ 修好（`0d3510616350`） |
| astropy__astropy-7336 | units.quantity_input decorator fails for construct | Cadair | astropy/units/decorators.py | ✅ 修好（`7efc3ee48a15`） |
| astropy__astropy-7606 | Unit equality comparison with None raises TypeErro | mhvk | astropy/units/core.py | ✅ 修好（`5bf2266a37d9`） |
| astropy__astropy-7671 | minversion failures | larrybradley | astropy/utils/introspection.py | ✅ 修好（`389a467ea22b`） |
| astropy__astropy-13572 | Problem in function nutation_matrix in earth_orien | ayshih | astropy/coordinates/earth_orientation.py | ✅ 修好（`c213b48ab368`） |
| astropy__astropy-14995 | In v5.3, NDDataRef mask propagation fails when one | bmorris3 | astropy/nddata/mixins/ndarithmetic.py | ✅ 修好（`f015aee8c76c`） |

`astropy__astropy-13572` 的重複候選可能會出現 astropy#10680（Invalid rotation_matrix creation in earth_orientation.py）：維護者把它標成和這張同一個 issue（#6583）重複，所以排在前面是合理的。這張是要修的那一張，請按「不是這張」繼續。

## 解讀時要注意

- 「負責人」：issue 有 GitHub assignee 就用它，否則用關閉這個 issue 的 PR 作者（`assignee_source` 有標）。工作台只用比這張 Ticket 早建立的歷史推薦。
- 優先級沒有標準答案：astropy 不標優先級，PRI 只用 Eclipse 訓練，這裡是跨資料集推測。
- 重複偵測的 SBERT 用 Firefox 訓練，套在 astropy 是跨資料集。
- 6 張補丁 Ticket 來自 SWE-bench 開發集，補丁方法在這 36 題上調過，不能當泛化證據；工作台重新產生的補丁只有 sha256 相同才引用官方判定。
- 以上是示範，不是評估。
