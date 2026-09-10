# Academic CFP Tracker

每週追蹤 JCR Q1 / top 20% 期刊的 Special Issue 與 Call for Papers，聚焦急診醫學、急救復甦、重症醫學、醫學資訊、遠距照護、醫療 AI 與醫療假訊息。

## 最新週報

[2026-W37 最新自動週報](reports/2026-W37.md)  
本期收錄：42 筆候選。

GitHub Pages 入口會由 `docs/` 自動產生；啟用 Pages 後可直接用網頁瀏覽歷史週報。

## 歷史週報

- [2026-W37 週報](reports/2026-W37.md)

## 快速開始

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python scripts\fetch_cfps.py
```

本機每週自動跑：

```powershell
.\scripts\install_windows_task.ps1 -RepositoryPath "/home/runner/work/specialIssueSurvey/specialIssueSurvey"
```

若要提高搜尋覆蓋率，可在本機環境或 GitHub Secrets 加入 `BRAVE_SEARCH_API_KEY` 或 `SERPAPI_API_KEY`。

## Q1 驗證原則

`config/target_journals.json` 是人工維護的 JCR Q1 / top-20% 白名單。每年 JCR 更新後，請用 Clarivate JCR 重新核對期刊名稱、category 與 JIF rank；SCImago 可作為公開替代參考，但不能取代 JCR。
