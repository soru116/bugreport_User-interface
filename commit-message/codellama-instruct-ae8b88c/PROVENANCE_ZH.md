# 提交訊息草稿生成（Code Llama-Instruct）

這個資料夾是組員的提交訊息生成程式快照，給 Ticket 工作台「補丁生成」分頁裡的「提交訊息草稿」使用。

| 項目 | 值 |
|---|---|
| 來源 repository | `Lyy12705/commit-message-generator` |
| commit | `ae8b88c`（Initial commit: commit message generator） |
| 模型 | `codellama:7b-instruct`（Meta；符合不用中國模型的守則），本機 Ollama |
| 方法 | 零樣本（zero-shot）指令；模型只回傳 `type` 與 `subject` 的 JSON，程式驗證後組成 Conventional Commit |

沒有修改任何複製來的程式碼（`src/`、`tests/`、`scripts/`、`README.md`、`pyproject.toml`）。

## 工作台怎麼呼叫

`bug_tracking_llm_system/src/integration/commit_message.py` 呼叫 `commit_message.generate_commit_message()`，傳入：

| 欄位 | 內容 |
|---|---|
| `patch` | 工作台產生的補丁，或 36 題實驗紀錄裡的補丁（unified diff） |
| `title`、`description` | Ticket 的標題與內文（內文最多 3000 字） |
| `test_result` | 只有補丁 sha256 和官方判定過的補丁完全相同時，才傳官方判定；其他情況傳「沒有執行測試」，讓模型不能宣稱已修好 |

## 解讀限制

- `success=true` 只代表訊息產生成功，不代表補丁可套用、測試通過或錯誤已修復。
- 這是草稿：工作台不會執行 `git commit`。
- 修改的檔案一律從 diff 解析，不信任其他欄位。
