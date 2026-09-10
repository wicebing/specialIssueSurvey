"""Rendering for the conference deadline page.

Organised by what a researcher has to decide this week, not by conference name:
what closes soon enough to need work now, what is announced but still distant,
and what has not opened yet but is worth having a draft ready for.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Sequence

from scripts.conferences import Conference, ConferenceSourceReport

__all__ = ["render_conference_markdown"]


def esc(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().replace("|", "\\|")


def _row(conference: Conference, today: date) -> str:
    deadline = conference.next_deadline
    days = conference.days_left(today)
    if deadline is None:
        cell = "`未公布`"
    elif days is not None and days <= 14:
        cell = f"`{deadline}` ⚠️ 剩 {days} 天"
    else:
        cell = f"`{deadline}` ({days} 天)"

    both = ""
    if conference.abstract_deadline and conference.submission_deadline:
        if conference.abstract_deadline != conference.submission_deadline:
            both = f"<br>全文 `{conference.submission_deadline}`"

    name = esc(conference.name)
    if conference.edition:
        name = f"{name} {esc(conference.edition)}"
    link = f"[{name}]({conference.url})" if conference.url else f"**{name}**"

    return "| {deadline}{both} | {name} | {city} | {dates} | {field} | {rank} |".format(
        deadline=cell,
        both=both,
        name=link,
        city=esc(conference.city) or "-",
        dates=esc(conference.dates) or "-",
        field=esc(conference.relevance or conference.field) or "-",
        rank=esc(conference.rank) or "-",
    )


HEADER = [
    "| 截稿 | 會議 | 舉辦城市 | 會議日期 | 相關領域 | 等級 |",
    "| :--- | :--- | :--- | :--- | :--- | :--- |",
]


def render_conference_markdown(
    week_id: str,
    generated_at: datetime,
    conferences: Sequence[Conference],
    sources: Sequence[ConferenceSourceReport],
    config: dict[str, Any],
) -> str:
    today = generated_at.date()
    urgent_days = int(config.get("policy", {}).get("urgent_days", 90))

    openish = [c for c in conferences if c.status == "open" and c.next_deadline]
    urgent = [c for c in openish if (c.days_left(today) or 0) <= urgent_days]
    later = [c for c in openish if (c.days_left(today) or 0) > urgent_days]
    pending = [c for c in conferences if c.status != "open"]

    lines: list[str] = [
        f"# Top Conference 投稿截止日追蹤：{week_id}",
        "",
        f"> 產生時間：{generated_at:%Y-%m-%d %H:%M} UTC  ",
        "> 每週更新。只列出截稿日還沒過的場次；已經截稿的會自動換成下一屆。  ",
        "> 涵蓋：急診醫學、重症與急救復甦、醫學資訊、醫學影像 AI、數位健康與人機互動、"
        "健康假訊息，以及頂尖 AI / ML / NLP 會議。",
        "",
        "## 本週行動摘要",
        "",
        f"- **{urgent_days} 天內要截稿：{len(urgent)} 場**",
        f"- 已公布但時間還早：{len(later)} 場",
        f"- 下一屆尚未公布：{len(pending)} 場",
    ]
    if urgent:
        first = min(urgent, key=lambda c: c.next_deadline or date.max)
        lines.append(
            f"- 最急：**{esc(first.name)} {esc(first.edition)}**，"
            f"`{first.next_deadline}`（剩 {first.days_left(today)} 天），"
            f"{esc(first.city) or '地點未定'}"
        )
    lines.append("")

    lines += [f"## ⏰ {urgent_days} 天內截稿（要動手了）", ""]
    if not urgent:
        lines += [f"_目前沒有 {urgent_days} 天內截稿的場次。_", ""]
    else:
        lines += HEADER
        lines += [_row(c, today) for c in sorted(urgent, key=lambda c: c.next_deadline or date.max)]
        lines.append("")

    lines += ["## 📅 已公布，還有時間準備", ""]
    if not later:
        lines += ["_沒有其他已公布截稿日的場次。_", ""]
    else:
        lines += HEADER
        lines += [_row(c, today) for c in sorted(later, key=lambda c: c.next_deadline or date.max)]
        lines.append("")

    lines += [
        "## 🕓 下一屆尚未公布（先把題目養好）",
        "",
        "這些會議還沒公布下一屆截稿日。表格列的是它**往年通常截稿的月份**，可以先排時程；"
        "已經確定的地點與日期也一併列出，方便先安排行程。",
        "",
        "| 會議 | 通常截稿月份 | 舉辦城市 | 會議日期 | 相關領域 | 官方連結 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for conference in sorted(pending, key=lambda c: (c.typical_month or "zz", c.name)):
        link = f"[開啟]({conference.url})" if conference.url else "-"
        name = esc(conference.name)
        if conference.edition:
            name = f"{name} {esc(conference.edition)}"
        lines.append(
            f"| **{name}** | {esc(conference.typical_month) or '不詳'} "
            f"| {esc(conference.city) or '-'} | {esc(conference.dates) or '-'} "
            f"| {esc(conference.relevance or conference.field) or '-'} | {link} |"
        )
    lines.append("")

    lines += [
        "## 📡 來源狀態",
        "",
        "| 來源 | 狀態 | 追蹤 | 已公布截稿 | 備註 |",
        "| :--- | :--- | ---: | ---: | :--- |",
    ]
    for source in sources:
        lines.append(
            f"| {esc(source.label)} | `{source.status}` | {source.found} | {source.accepted} "
            f"| {esc(source.notes)} |"
        )
    lines.append("")

    lines += [
        "## 方法與限制",
        "",
        "- 截稿日來自社群維護的 `ccf-deadlines` 資料集，以及人工核對過的學會官方頁面。",
        "- 已過期的場次會自動換成下一屆；若下一屆尚未公布，就列在「尚未公布」區並附上往年月份，"
        "不會假裝有確定日期。",
        "- 「舉辦城市」欄位已把場館名稱與城市分開，只顯示城市與國家。",
        "- 資料集偶爾會比官方網站慢一兩週。**投稿前務必點官方連結確認截稿時間與時區**，"
        "很多會議的截稿是 AoE（Anywhere on Earth）時區。",
        "- 學會型醫學會議（SAEM、SCCM、AMIA 等）多數不提供機器可讀的截稿資訊，"
        "由人工核對後寫入設定檔，每年新一屆公布時需要更新。",
        "",
    ]
    return "\n".join(lines)
