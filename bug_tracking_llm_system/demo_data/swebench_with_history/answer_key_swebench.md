# SWE-bench 示範：答案

這份只用來核對錯誤定位結果，不要匯入。真正修改的檔案來自 SWE-bench 的 gold patch；新 Ticket 裡沒有 patch、測試與提示。

| 專案 | Ticket | 標題 | 真正修改的檔案 |
|---|---|---|---|
| psf/requests | psf__requests-3362 | Uncertain about content/text vs iter_content(decode_unicode=True/False | requests/utils.py |
| psf/requests | psf__requests-3718 | AttributeError: 'NoneType' object has no attribute 'read' | requests/models.py |
| psf/requests | psf__requests-3738 | Requests v2.12.2 does not add parameters to URLs with schemes it does  | requests/models.py |
| psf/requests | psf__requests-4106 | AttributeError: module 'requests.packages' has no attribute 'urllib3' | requests/packages.py |
| psf/requests | psf__requests-4356 | Misleading exception with invalid protocol in proxy variable | requests/adapters.py<br>requests/exceptions.py |
| psf/requests | psf__requests-4718 | Should Authorization header be cleared in https -> http redirect? | requests/sessions.py |
| pallets/flask | pallets__flask-4169 | Exceptions are sometimes replaced with "TypeError: exceptions must der | src/flask/cli.py |
| pallets/flask | pallets__flask-4544 | `flask run` rejects `--key foo.pem --cert foo.cert` | src/flask/cli.py |
| pallets/flask | pallets__flask-4642 | FlaskGroup does not work when nested in a click.group | src/flask/app.py<br>src/flask/cli.py<br>src/flask/debughelpers.py |
| pallets/flask | pallets__flask-4992 | Add a file mode parameter to flask.Config.from_file() | src/flask/config.py |
| pallets/flask | pallets__flask-5014 | Require a non-empty name for Blueprints | src/flask/blueprints.py |
| pallets/flask | pallets__flask-5063 | Flask routes to return domain/sub-domains information | src/flask/cli.py |

## 解讀時要注意

- 錯誤定位團隊在 SWE-bench 上調過方法，這批資料不是沒看過的測試集，結果只能當示範。
- `history_swebench.jsonl` 是同一個 repository 的其他 issue（只有標題、內文、時間），可以當重複偵測的比對歷史；SWE-bench 沒有重複標記，所以候選都應該判為「不是重複」。
- SWE-bench 沒有負責人資料，負責人推薦會建議人工分派。
- PRI 優先級模型是用 Eclipse 訓練的，套在 GitHub issue 上屬於跨資料集推測。
