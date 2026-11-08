# 真實資料示範：答案

產生時間：2026-10-18T17:32:24+00:00，亂數種子 20261019。這份檔案只用來核對工作台的結果，不要匯入。

| 專案 | Ticket | 標題 | 真正的重複對象 | 真正的優先級 | 真正的負責人 |
|---|---|---|---|---|---|
| Mozilla Firefox | FF-178016 | "Decrease Text Size" wraps around. | FF-53995（using "text zoom" to scale fonts very sm） | （資料沒有） | bugzilla@blakeross.com |
| Mozilla Firefox | FF-170844 | scroll bar button arrows magically move | FF-170545（Scrolling in any window enlarges the sco） | （資料沒有） | hyatt@mozilla.org |
| Mozilla Firefox | FF-178284 | "Hide the tab when only one tab is open" don't work | FF-177506（turning off "hide tab bar ..." has no ef） | （資料沒有） | bugzilla@blakeross.com |
| Mozilla Firefox | FF-175164 | Some sidebar bookmarks have the wrong icon (Oct 17 nightly b | FF-174265（Wrong favicons in bookmarks） | （資料沒有） | p_ch@verizon.net |
| Mozilla Firefox | FF-184741 | F3 doesn't work in View Page Source | 不是重複 | （資料沒有） | p_ch@verizon.net |
| Mozilla Firefox | FF-175893 | Make XUL <tab>'s focusable | 不是重複 | P2 | aaronlev@gmail.com |
| Eclipse | 25244 | JDIThread#getTopStackFrame() performance | 16036（UI not responsive when stack is deep） | P4 | （資料沒有） |
| Eclipse | 11903 | 'add javadoc comment' adds non-java doc comments | 不是重複 | P1 | （資料沒有） |
| Eclipse | 16247 | "Externalize Strings" dialog is huge | 不是重複 | P2 | （資料沒有） |
| Eclipse | 2905 | Hover-help in wizards for long messages (1GKKS6N) | 不是重複 | P3 | （資料沒有） |
| Eclipse | 42563 | [JFace] There is no way to set the DirectoryDialog's message | 不是重複 | P4 | （資料沒有） |
| Eclipse | 86233 | [KeyBindings] registry: Accelerator key cannot be translated | 不是重複 | P5 | （資料沒有） |

## 解讀時要注意

- 新 Ticket 已移除 duplicate_of、priority、assignee、status、resolution；這些答案只在這份檔案裡。
- 專案沒有對應的 Repository，所以錯誤定位會顯示「略過」，這是預期的結果。
- SBERT 重複偵測模型是用 mozilla_firefox_duplicates.csv 訓練的，Firefox 的重複 Ticket 都在訓練資料裡，所以重複偵測結果會偏好看，不能當成準確率。
- PRI 優先級模型只用 Eclipse 資料訓練；Firefox Ticket 的優先級是跨資料集的推測，而且 Firefox 原始資料大多沒有填優先級，無法核對。
- Firefox 的負責人有一半以上是 bugzilla@blakeross.com，負責人推薦會明顯偏向他，這是資料本身的分布。
- Eclipse 新 Ticket 來自 PRI 的 natural_test，PRI 模型訓練時沒看過；工作台計算優先級時會把同一張報告從歷史資料中排除。
- Eclipse 的重複偵測歷史只取 1500 筆（含正確答案），比完整的 1 萬筆容易；SBERT 是用 Firefox 訓練的，用在 Eclipse 屬於跨資料集。
- Eclipse 資料沒有負責人欄位，所以 Eclipse Ticket 的負責人推薦沒有可用的歷史，會建議人工分派。
