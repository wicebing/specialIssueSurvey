# Top Journal Special Issue Tracker

每週追蹤頂尖期刊（JCR Q1 / top 20%）**目前仍可投稿**的 Special Issue 與 Call for Papers，
並用實際發表資料呈現主流研究趨勢。聚焦急診醫學、急救復甦、重症醫學、醫學資訊、遠距照護、
醫療 AI 與醫療假訊息。

## 最新週報

[2026-W37 週報](reports/2026-W37.md)

- 確認開放投稿：100 筆
- 其中有明確截稿日：48 筆
- 本週新出現：100 筆

## 這個追蹤器怎麼保證不亂報

- **截稿日只從該筆徵稿自己的區塊擷取**，而且必須緊接在 `submission deadline` / `截稿`
  等關鍵詞後面。找不到就標示為滾動徵稿，絕不用整頁的日期硬湊。
- **過期的徵稿自動剔除。** IEEE JBHI 這類不會清理過期項目的頁面特別需要這層過濾。
- **導覽列連結被擋掉**，「登入」「投稿須知」「語言編修」不會再被當成徵稿。
- **被 Cloudflare 擋住的期刊會明列在報告裡**，不會讓「抓不到」看起來像「沒有徵稿」。

## 歷史週報

- [2026-W37 週報](reports/2026-W37.md)

## 快速開始

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m scripts.fetch_cfps
```

只重新產生網站（不連網）：

```powershell
.\.venv\Scripts\python -m scripts.fetch_cfps --site-only
```

本機每週自動跑：

```powershell
.\scripts\install_windows_task.ps1 -RepositoryPath "/home/runner/work/specialIssueSurvey/specialIssueSurvey"
```

設定 `NCBI_API_KEY` 環境變數可提高 PubMed 趨勢查詢的速率上限（非必要）。

## Q1 驗證原則

`config/target_journals.json` 是人工維護的 JCR Q1 / top-20% 白名單。每年 JCR 更新後，
請用 Clarivate JCR 重新核對期刊名稱、category 與 JIF rank；SCImago 可作為公開替代參考，
但不能取代 JCR。
