"""Weekly report rendering.

The report answers two questions the previous one could not:

* Which special issues can I actually submit to right now, and by when?
* What are the top journals publishing more of this year than last?

Every row in the open-calls tables carries a verified deadline or an explicit
publisher statement that submissions are open.  Anything we could not confirm
is placed in a separate section, and journals whose publishers block automated
access are listed by name so their absence is never read as "no open calls".
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from scripts.cfp_sources import CFPRecord, SourceReport

__all__ = ["render_markdown"]

STAGE_LABELS = {
    "emerging": "🚀 新興",
    "rising": "📈 上升",
    "steady": "➡️ 持平",
    "cooling": "📉 降溫",
    "too_sparse": "· 樣本不足",
}


def esc(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().replace("|", "\\|")


def _deadline_cell(record: CFPRecord) -> str:
    iso = record.deadline_date
    if not iso:
        return "`滾動徵稿 / 未標示`"
    days = record.days_left
    if days is None:
        return f"`{iso}`"
    if days <= 14:
        return f"`{iso}` ⚠️ 剩 {days} 天"
    return f"`{iso}` ({days} 天)"


def _topic_cell(record: CFPRecord) -> str:
    labels = [topic["label"].split(" / ")[0] for topic in record.topics]
    return esc("、".join(labels)) if labels else "-"


def render_markdown(
    week_id: str,
    generated_at: datetime,
    dated: Sequence[CFPRecord],
    undated: Sequence[CFPRecord],
    trends: dict[str, Any],
    sources: Sequence[SourceReport],
    manual_watchlist: Sequence[dict[str, Any]],
    config: dict[str, Any],
) -> str:
    today = generated_at.date()
    lines: list[str] = []
    total_open = len(dated) + len(undated)
    new_count = sum(1 for record in list(dated) + list(undated) if record.is_new)

    tracked = trends.get("tracked_terms", [])
    discovered = trends.get("discovered_terms", [])
    rising = [t for t in tracked if t["stage"] in {"emerging", "rising"}]
    cooling = [t for t in tracked if t["stage"] == "cooling"]

    lines += [
        f"# Top Journal Special Issue 追蹤週報：{week_id}",
        "",
        f"> 產生時間：{generated_at:%Y-%m-%d %H:%M} UTC  ",
        "> 只收錄「確認仍可投稿」的徵稿；截稿日皆取自該筆徵稿自身的頁面文字。  ",
        "> 追蹤領域：急診醫學、急救復甦、重症、醫學資訊、遠距照護、醫療 AI、醫療假訊息。",
        "",
        "## 本週行動摘要",
        "",
        f"- **確認開放投稿：{total_open} 筆**（{len(dated)} 筆有明確截稿日、{len(undated)} 筆為滾動徵稿）",
        f"- 其中核心 Q1 白名單期刊：{sum(1 for r in list(dated) + list(undated) if r.tier == 'core')} 筆",
        f"- 本週新出現：{new_count} 筆",
    ]

    soonest = [r for r in dated if r.days_left is not None]
    if soonest:
        first = min(soonest, key=lambda r: r.days_left or 0)
        lines.append(
            f"- 最快截稿：**{esc(first.journal)}** — {esc(first.title)}，"
            f"`{first.deadline_date}`（剩 {first.days_left} 天）"
        )
    if rising:
        top = rising[0]
        lines.append(
            f"- 最強上升主題：**{esc(top['label'])}**"
            f"（近半年 {top['recent_count']} 篇 vs 去年同期 {top['baseline_count']} 篇，"
            f"成長 {top['growth_ratio']:.2f} 倍）"
        )
    lines.append("")

    # --- open calls with a confirmed deadline -------------------------------
    def _deadline_table(records: Sequence[CFPRecord]) -> list[str]:
        rows = [
            "| 截稿 | 期刊 | 徵稿主題 | 領域 | 分數 | 新 |",
            "| :--- | :--- | :--- | :--- | ---: | :---: |",
        ]
        for record in sorted(records, key=lambda r: r.deadline_date or "9999-12-31"):
            rows.append(
                "| {deadline} | **{journal}** | [{title}]({url}) | {topics} | {score} | {new} |".format(
                    deadline=_deadline_cell(record),
                    journal=esc(record.journal or "未標示"),
                    title=esc(record.title),
                    url=record.url,
                    topics=_topic_cell(record),
                    score=record.score,
                    new="🆕" if record.is_new else "",
                )
            )
        return rows

    core_dated = [r for r in dated if r.tier == "core"]
    other_dated = [r for r in dated if r.tier != "core"]

    lines += [
        "## ✅ 核心 Q1 期刊：確認開放投稿",
        "",
        "依截稿日排序，最急的在最前面。這一區只放核心 top-20% 白名單期刊。",
        "",
    ]
    if not core_dated:
        lines += ["_本週核心期刊沒有可確認截稿日的開放徵稿。_", ""]
    else:
        lines += _deadline_table(core_dated)
        lines.append("")

    if other_dated:
        lines += [
            "## 📚 其他期刊：確認開放投稿",
            "",
            "這些期刊仍值得投，但不在核心 top-20% 白名單內，投稿前請自行確認分區。",
            "",
        ]
        lines += _deadline_table(other_dated)
        lines.append("")

    # --- open, no stated deadline -------------------------------------------
    if undated:
        lines += [
            "## 🔄 開放中但未標示截稿日（滾動徵稿）",
            "",
            "這些徵稿頁面明確寫著仍在收稿，但沒有給截止日期；投稿前請再確認一次。",
            "",
            "| 期刊 | 徵稿主題 | 領域 | 依據 |",
            "| :--- | :--- | :--- | :--- |",
        ]
        for record in undated:
            lines.append(
                "| **{journal}** | [{title}]({url}) | {topics} | {reason} |".format(
                    journal=esc(record.journal or "未標示"),
                    title=esc(record.title),
                    url=record.url,
                    topics=_topic_cell(record),
                    reason=esc(record.status.get("reason", "")),
                )
            )
        lines.append("")

    # --- trend radar ---------------------------------------------------------
    windows = trends.get("windows", {})
    corpus = trends.get("corpus", {})
    lines += [
        "## 🔥 Top Journal 主流趨勢",
        "",
        f"資料來源：PubMed，{len(trends.get('journals_tracked', []))} 本核心 Q1 期刊的實際發表量。  ",
        f"比較區間：近期 `{windows.get('recent_start', '')}` 至 `{windows.get('recent_end', '')}`"
        f" vs 去年同期 `{windows.get('baseline_start', '')}` 至 `{windows.get('baseline_end', '')}`。",
        "",
    ]

    if rising:
        lines += [
            "### 📈 正在升溫的題目（建議追這些）",
            "",
            "| 主題 | 近半年 | 去年同期 | 成長 | 階段 |",
            "| :--- | ---: | ---: | ---: | :--- |",
        ]
        for item in rising[:12]:
            lines.append(
                f"| {esc(item['label'])} | {item['recent_count']} | {item['baseline_count']} "
                f"| {item['growth_ratio']:.2f}x | {STAGE_LABELS.get(item['stage'], item['stage'])} |"
            )
        lines.append("")

    if discovered:
        lines += [
            "### 🚀 自動探勘出的新浮現題目",
            "",
            "直接從近半年的論文標題挖出來的詞組，不在預設關鍵字清單內。",
            "",
            "| 詞組 | 近半年 | 去年同期 | 成長 |",
            "| :--- | ---: | ---: | ---: |",
        ]
        for item in discovered[:12]:
            lines.append(
                f"| {esc(item['term'])} | {item['recent_count']} | {item['baseline_count']} "
                f"| {item['growth_ratio']:.2f}x |"
            )
        lines.append("")

    if cooling:
        lines += [
            "### 📉 正在降溫的題目（投入前請三思）",
            "",
            "| 主題 | 近半年 | 去年同期 | 變化 |",
            "| :--- | ---: | ---: | ---: |",
        ]
        for item in cooling[:8]:
            lines.append(
                f"| {esc(item['label'])} | {item['recent_count']} | {item['baseline_count']} "
                f"| {item['growth_ratio']:.2f}x |"
            )
        lines.append("")

    if corpus:
        lines.append(
            f"_趨勢取樣：近期 {corpus.get('recent_titles', 0)} 篇標題、"
            f"去年同期 {corpus.get('baseline_titles', 0)} 篇標題。_"
        )
        lines.append("")

    # --- where trend meets opportunity --------------------------------------
    matches = _match_trends_to_calls(rising + discovered, list(dated) + list(undated))
    lines += ["## 🎯 建議切入點：熱門主題 × 正在徵稿", ""]
    if not matches:
        lines += ["_本週上升主題與開放徵稿沒有明顯交集；可先從上面的上升主題準備稿件。_", ""]
    else:
        lines += [
            "以下徵稿的主題，正好落在目前升溫中的研究方向上。",
            "",
            "| 熱門主題 | 對應徵稿 | 期刊 | 截稿 |",
            "| :--- | :--- | :--- | :--- |",
        ]
        for term, record in matches[:12]:
            lines.append(
                f"| **{esc(term)}** | [{esc(record.title)}]({record.url}) "
                f"| {esc(record.journal)} | {_deadline_cell(record)} |"
            )
        lines.append("")

    # --- journals we cannot check automatically -----------------------------
    if manual_watchlist:
        lines += [
            "## 🔒 無法自動查詢的頂尖期刊（請手動點開）",
            "",
            "這些出版社用 Cloudflare / captcha 擋掉自動查詢。**它們沒有出現在上面的清單，"
            "不代表沒有徵稿**，請直接點連結確認。",
            "",
            "| 期刊 | 出版社 | 阻擋原因 | 直接連結 |",
            "| :--- | :--- | :--- | :--- |",
        ]
        for item in manual_watchlist:
            lines.append(
                f"| {esc(item.get('journal', ''))} | {esc(item.get('publisher', ''))} "
                f"| {esc(item.get('block', ''))} | [開啟]({item.get('url', '')}) |"
            )
        lines.append("")

    # --- source health -------------------------------------------------------
    lines += [
        "## 📡 來源健康狀態",
        "",
        "| 來源 | 狀態 | 抓到 | 收錄 | 傳輸 | 備註 |",
        "| :--- | :--- | ---: | ---: | :--- | :--- |",
    ]
    for source in sources:
        rejects = ", ".join(f"{k} ×{v}" for k, v in sorted(source.reject_reasons.items()))
        note = source.notes or rejects
        lines.append(
            f"| {esc(source.label)} | `{source.status}` | {source.found} | {source.accepted} "
            f"| {esc(source.transport)} | {esc(note)} |"
        )
    lines.append("")

    lines += [
        "## 方法與限制",
        "",
        "- 每筆徵稿的截稿日只從**該筆徵稿自身的區塊文字**擷取，且必須緊接在 "
        "`submission deadline` / `截稿` 之類的關鍵詞後面；找不到就標成滾動徵稿，不會用整頁的日期硬湊。",
        "- 已過期的徵稿會自動剔除。IEEE JBHI 的頁面不會清掉過期項目，這一層過濾特別重要。",
        "- 導覽列連結（登入、投稿須知、語言編修等）會被擋掉，不會再出現在清單裡。",
        "- 趨勢區塊統計的是**實際發表量**，不是徵稿數量；成長倍率用「近半年 vs 去年同期」對齊季節性。",
        "- Q1 判定以 `config/target_journals.json` 的人工白名單為準，每年 JCR 更新後仍須用 "
        "Clarivate JCR 重新核對。",
        "- 投稿前務必點進官方頁面再確認一次截稿日、客座編輯與投稿系統。",
        "",
    ]
    return "\n".join(lines)


def _match_trends_to_calls(
    trend_items: Sequence[dict[str, Any]],
    records: Sequence[CFPRecord],
) -> list[tuple[str, CFPRecord]]:
    """Pair open calls with the rising topics they cover.

    Grouped by call rather than by term: one special issue that matches four
    hot topics is a single strong opportunity, not four rows of the same link.
    """
    by_record: dict[str, tuple[CFPRecord, list[str]]] = {}
    for item in trend_items:
        term = item.get("term", "")
        if not term or len(term) < 5:
            continue
        needle = term.lower()
        label = item.get("label") or term
        for record in records:
            haystack = f"{record.title} {record.summary}".lower()
            if needle not in haystack:
                continue
            entry = by_record.setdefault(record.fingerprint, (record, []))
            if label not in entry[1]:
                entry[1].append(label)

    matches = [
        ("、".join(labels[:4]), record) for record, labels in by_record.values() if labels
    ]
    # Rank by how many hot topics a call covers, then by score and urgency.
    matches.sort(
        key=lambda pair: (
            -pair[0].count("、"),
            -pair[1].score,
            pair[1].deadline_date or "9999-12-31",
        )
    )
    return matches
