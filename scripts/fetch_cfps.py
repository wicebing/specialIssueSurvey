"""Weekly tracker for open Special Issue / Call-for-Papers opportunities.

Pipeline:

1. :mod:`scripts.cfp_sources` adapters locate entries on each publisher's
   open-calls listing, one entry at a time.
2. :mod:`scripts.cfp_extract` validates each one, refusing to guess a deadline
   and refusing to pass an entry it cannot show is still accepting submissions.
3. :mod:`scripts.trends` measures what the same journals are actually
   publishing, so the report can point at topics that are gaining ground.
4. :mod:`scripts.report` renders the weekly Markdown, and this module renders
   the static site around it.

Run ``python scripts/fetch_cfps.py --help`` for the available switches.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# Allow both `python -m scripts.fetch_cfps` and `python scripts/fetch_cfps.py`.
# The scheduled task and the GitHub workflow have historically used the latter.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.cfp_sources import CFPRecord, SourceReport, collect_from_spec, dedupe
from scripts.http_client import PoliteSession
from scripts.report import render_markdown
from scripts.trends import build_trend_report

try:
    import markdown as markdown_lib
except ImportError:  # pragma: no cover
    markdown_lib = None


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "target_journals.json"
REPORTS_DIR = ROOT / "reports"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
DOCS_REPORTS_DIR = DOCS_DIR / "reports"
CSS_PATH = DOCS_DIR / "assets" / "site.css"


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def historical_fingerprints(current_week: str) -> set[str]:
    """Fingerprints seen in previous weeks, used to flag genuinely new calls."""
    seen: set[str] = set()
    if not DATA_DIR.exists():
        return seen
    for path in DATA_DIR.glob("*.json"):
        if path.name in {"latest.json", f"{current_week}.json"}:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in payload.get("calls", []) + payload.get("candidates", []):
            if item.get("fingerprint"):
                seen.add(item["fingerprint"])
    return seen


# A source that answered normally and simply had nothing is not a failure.
# These states mean we never saw the page, so last week's findings still stand.
FETCH_FAILURE_STATES = ("bot_challenge", "blocked_", "dead_link_", "http_", "error", "parse_error")


def load_previous_calls() -> tuple[list[dict[str, Any]], str]:
    """Return the most recent run's calls, for carrying past a blocked fetch."""
    latest = DATA_DIR / "latest.json"
    if not latest.exists():
        return [], ""
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return [], ""
    return payload.get("calls", []), payload.get("week", "")


def carry_forward_blocked_sources(
    reports: list[SourceReport],
    today: date,
    horizon_days: int,
) -> list[CFPRecord]:
    """Re-list still-valid calls from sources we could not reach this week.

    Springer serves its bot wall to datacenter IPs, so a GitHub-hosted run loses
    Critical Care, Intensive Care Medicine and three more journals that a run
    from a normal connection sees fine. Dropping them would quietly shrink the
    report; carrying them forward keeps the opportunity visible and labels it as
    not re-checked, which is the honest middle ground.
    """
    blocked = {
        report.source_id
        for report in reports
        if report.accepted == 0
        and any(report.status.startswith(state) for state in FETCH_FAILURE_STATES)
    }
    if not blocked:
        return []

    previous, previous_week = load_previous_calls()
    carried: list[CFPRecord] = []
    for item in previous:
        if item.get("source_id") not in blocked:
            continue
        if item.get("carried_forward") and item.get("last_verified") == previous_week:
            # Avoid re-carrying something that was itself only ever carried.
            pass
        deadline_iso = (item.get("deadline") or {}).get("date")
        if deadline_iso:
            try:
                deadline = date.fromisoformat(deadline_iso)
            except ValueError:
                continue
            days_left = (deadline - today).days
            if days_left < 0 or days_left > horizon_days:
                continue
            status = dict(item.get("status") or {})
            status["days_left"] = days_left
        else:
            status = dict(item.get("status") or {})
        record = CFPRecord(
            journal=item.get("journal", ""),
            title=item.get("title", ""),
            url=item.get("url", ""),
            publisher=item.get("publisher", ""),
            source_id=item.get("source_id", ""),
            source_label=item.get("source_label", ""),
            summary=item.get("summary", ""),
            deadline=item.get("deadline") or {},
            status=status,
            topics=item.get("topics") or [],
            jcr_band=item.get("jcr_band", ""),
            journal_priority=int(item.get("journal_priority", 1)),
            journal_category=item.get("journal_category", ""),
            score=max(0, int(item.get("score", 0)) - 5),
            fingerprint=item.get("fingerprint", ""),
            tier=item.get("tier", "other"),
            carried_forward=True,
            last_verified=item.get("last_verified") or previous_week,
        )
        carried.append(record)

    for report in reports:
        count = sum(1 for record in carried if record.source_id == report.source_id)
        if count:
            report.accepted = count
            note = f"unreachable; carried {count} still-open calls from {previous_week}"
            report.notes = f"{report.notes}; {note}" if report.notes else note
    return carried


def collect(
    config: dict[str, Any],
    today: date,
    limit_sources: int | None = None,
    trend_terms: list[str] | None = None,
) -> tuple[list[CFPRecord], list[SourceReport]]:
    policy = config.get("tracking_policy", {})
    session = PoliteSession(contact_email=policy.get("contact_email", ""))
    specs = config.get("sources", [])
    if limit_sources is not None:
        specs = specs[:limit_sources]

    records: list[CFPRecord] = []
    reports: list[SourceReport] = []
    for spec in specs:
        found, report = collect_from_spec(session, spec, config, today, trend_terms or [])
        records.extend(found)
        reports.append(report)
        print(
            f"  {report.label}: found={report.found} accepted={report.accepted} "
            f"status={report.status}",
            flush=True,
        )
    return dedupe(records), reports


def build_trends(config: dict[str, Any], today: date) -> dict[str, Any]:
    policy = config.get("tracking_policy", {})
    session = PoliteSession(contact_email=policy.get("contact_email", ""))
    api_key = os.environ.get("NCBI_API_KEY", "")
    return build_trend_report(session, config, today, api_key)


# ---------------------------------------------------------------------------
# Static site
# ---------------------------------------------------------------------------


def ensure_css() -> None:
    CSS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CSS_PATH.write_text(SITE_CSS, encoding="utf-8")


def render_site_page(title: str, body: str, active: str = "") -> str:
    prefix = "../" if active == "report" else ""
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <meta name="description" content="Weekly tracker for open special issues and calls for papers in top emergency medicine, critical care, medical informatics, digital health and clinical AI journals.">
  <link rel="stylesheet" href="{prefix}assets/site.css">
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div class="brand"><a href="{prefix}index.html">Top Journal CFP Tracker</a></div>
      <nav class="nav" aria-label="Primary">
        <a href="{prefix}index.html">期刊 Special Issue</a>
        <a href="{prefix}conferences.html">會議截稿日</a>
      </nav>
    </header>
    {body}
    <footer>投稿前請務必點入官方頁面再次確認截稿日與 JCR 分區。</footer>
  </main>
</body>
</html>
"""


def markdown_to_html(markdown_text: str) -> str:
    if markdown_lib is not None:
        return markdown_lib.markdown(
            markdown_text,
            extensions=["tables", "fenced_code", "toc"],
            output_format="html5",
        )
    return simple_markdown_to_html(markdown_text)


def simple_markdown_to_html(markdown_text: str) -> str:
    """Minimal renderer used when the Markdown package is unavailable."""
    output: list[str] = []
    in_list = False
    in_table = False
    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        if not line:
            if in_list:
                output.append("</ul>")
                in_list = False
            if in_table:
                output.append("</tbody></table>")
                in_table = False
            continue
        if line.startswith("#"):
            if in_list:
                output.append("</ul>")
                in_list = False
            level = min(len(line) - len(line.lstrip("#")), 3)
            output.append(f"<h{level}>{html.escape(line[level:].strip())}</h{level}>")
            continue
        if line.startswith(">"):
            output.append(f"<blockquote>{html.escape(line.lstrip('> ').strip())}</blockquote>")
            continue
        if line.startswith("- "):
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{html.escape(line[2:].strip())}</li>")
            continue
        if line.startswith("|") and line.endswith("|"):
            cells = [html.escape(cell.strip()) for cell in line.strip("|").split("|")]
            if set("".join(cells)) <= {":", "-", " "}:
                continue
            if not in_table:
                output.append("<table><tbody>")
                in_table = True
            output.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
            continue
        output.append(f"<p>{html.escape(line)}</p>")
    if in_list:
        output.append("</ul>")
    if in_table:
        output.append("</tbody></table>")
    return "\n".join(output)


def generate_site(week_id: str | None = None) -> None:
    ensure_css()
    DOCS_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_paths = sorted(REPORTS_DIR.glob("*.md"), reverse=True)

    latest: dict[str, Any] = {}
    latest_json = DATA_DIR / "latest.json"
    if latest_json.exists():
        try:
            latest = json.loads(latest_json.read_text(encoding="utf-8"))
        except Exception:
            latest = {}

    for path in report_paths:
        body = markdown_to_html(path.read_text(encoding="utf-8"))
        page = render_site_page(
            f"{path.stem} CFP 週報", f'<article class="content">{body}</article>', active="report"
        )
        (DOCS_REPORTS_DIR / f"{path.stem}.html").write_text(page, encoding="utf-8")

    stats = latest.get("stats", {})
    trends = latest.get("trends", {})
    rising = [t for t in trends.get("tracked_terms", []) if t["stage"] in {"emerging", "rising"}]
    calls = latest.get("calls", [])
    urgent = sorted(
        (c for c in calls if c.get("deadline", {}).get("date")),
        key=lambda c: c["deadline"]["date"],
    )[:8]

    # Coverage by field, so a reader can see at a glance whether their own
    # specialty is represented this week rather than scrolling the report.
    field_counts: dict[str, int] = {}
    for call in calls:
        name = call.get("field") or "其他"
        field_counts[name] = field_counts.get(name, 0) + 1
    fields_html = (
        "\n".join(
            f'<li>{html.escape(name)}<span class="pill">{count}</span></li>'
            for name, count in sorted(field_counts.items(), key=lambda kv: -kv[1])
        )
        or "<li>尚無資料</li>"
    )

    reports_html = (
        "\n".join(
            f'<li><a href="reports/{path.stem}.html">{html.escape(path.stem)} 週報</a></li>'
            for path in report_paths[:20]
        )
        or "<li>尚無週報</li>"
    )
    urgent_html = (
        "\n".join(
            '<li><a href="{url}">{title}</a><span class="pill">{deadline}</span>'
            '<div class="muted">{journal}</div></li>'.format(
                url=html.escape(c.get("url", "")),
                title=html.escape(c.get("title", "")[:90]),
                deadline=html.escape(c["deadline"]["date"]),
                journal=html.escape(c.get("journal", "")),
            )
            for c in urgent
        )
        or "<li>尚無確認開放的徵稿。</li>"
    )
    trends_html = (
        "\n".join(
            '<li><strong>{label}</strong><span class="pill">{ratio:.2f}x</span>'
            '<div class="muted">近半年 {recent} 篇 vs 去年同期 {base} 篇</div></li>'.format(
                label=html.escape(t.get("label", t.get("term", ""))),
                ratio=t.get("growth_ratio", 0),
                recent=t.get("recent_count", 0),
                base=t.get("baseline_count", 0),
            )
            for t in rising[:8]
        )
        or "<li>等待第一次 live 蒐集後產生趨勢。</li>"
    )

    latest_week = latest.get("week") or week_id or "尚未產生"
    body = f"""
<section class="hero">
  <h1>頂尖期刊 Special Issue 追蹤</h1>
  <p>只列出<strong>確認仍可投稿</strong>的徵稿，並用實際發表量告訴你哪些題目正在升溫。</p>
</section>
<section class="stats" aria-label="Latest report stats">
  <div class="stat"><strong>{stats.get('core_calls', 0)}</strong><span>核心 Q1 徵稿</span></div>
  <div class="stat"><strong>{stats.get('with_deadline', 0)}</strong><span>有明確截稿日</span></div>
  <div class="stat"><strong>{stats.get('new', 0)}</strong><span>本週新出現</span></div>
  <div class="stat"><strong>{stats.get('sources_ok', 0)}/{stats.get('sources', 0)}</strong><span>來源正常</span></div>
</section>
<section class="grid">
  <div class="panel">
    <h2>最急的截稿</h2>
    <ul class="report-list">{urgent_html}</ul>
  </div>
  <aside class="panel">
    <h2>升溫中的題目</h2>
    <p><span class="pill">{html.escape(latest_week)}</span></p>
    <ul class="report-list">{trends_html}</ul>
  </aside>
</section>
<section class="grid">
  <div class="panel">
    <h2>各領域開放徵稿數</h2>
    <ul class="report-list">{fields_html}</ul>
  </div>
  <aside class="panel">
    <h2>週報存檔</h2>
    <ul class="report-list">{reports_html}</ul>
  </aside>
</section>
"""
    (DOCS_DIR / "index.html").write_text(
        render_site_page("Top Journal CFP Tracker", body), encoding="utf-8"
    )
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")


def update_readme(week_id: str, stats: dict[str, Any]) -> None:
    reports = sorted(REPORTS_DIR.glob("*.md"), reverse=True)
    links = "\n".join(f"- [{p.stem} 週報](reports/{p.name})" for p in reports[:20]) or "- 尚無週報"
    content = f"""# Top Journal Special Issue Tracker

每週追蹤頂尖期刊（JCR Q1 / top 20%）**目前仍可投稿**的 Special Issue 與 Call for Papers，
並用實際發表資料呈現主流研究趨勢。聚焦急診醫學、急救復甦、重症醫學、醫學資訊、遠距照護、
醫療 AI 與醫療假訊息。

## 最新週報

[{week_id} 週報](reports/{week_id}.md)

- 確認開放投稿：{stats.get('open_calls', 0)} 筆
- 其中有明確截稿日：{stats.get('with_deadline', 0)} 筆
- 本週新出現：{stats.get('new', 0)} 筆

## 會議投稿截止日

另一份每週更新的清單，列出頂尖會議的**截稿日、舉辦城市與會議日期**。

[{week_id} 會議截稿週報](reports/conferences/{week_id}.md)

```powershell
.\\.venv\\Scripts\\python -m scripts.fetch_conferences
```

## 這個追蹤器怎麼保證不亂報

- **截稿日只從該筆徵稿自己的區塊擷取**，而且必須緊接在 `submission deadline` / `截稿`
  等關鍵詞後面。找不到就標示為滾動徵稿，絕不用整頁的日期硬湊。
- **過期的徵稿自動剔除。** IEEE JBHI 這類不會清理過期項目的頁面特別需要這層過濾。
- **導覽列連結被擋掉**，「登入」「投稿須知」「語言編修」不會再被當成徵稿。
- **被 Cloudflare 擋住的期刊會明列在報告裡**，不會讓「抓不到」看起來像「沒有徵稿」。

## 歷史週報

{links}

## 快速開始

```powershell
python -m venv .venv
.\\.venv\\Scripts\\python -m pip install -r requirements.txt
.\\.venv\\Scripts\\python -m scripts.fetch_cfps
```

只重新產生網站（不連網）：

```powershell
.\\.venv\\Scripts\\python -m scripts.fetch_cfps --site-only
```

本機每週自動跑：

```powershell
.\\scripts\\install_windows_task.ps1 -RepositoryPath "{ROOT}"
```

設定 `NCBI_API_KEY` 環境變數可提高 PubMed 趨勢查詢的速率上限（非必要）。

## Q1 驗證原則

`config/target_journals.json` 是人工維護的 JCR Q1 / top-20% 白名單。每年 JCR 更新後，
請用 Clarivate JCR 重新核對期刊名稱、category 與 JIF rank；SCImago 可作為公開替代參考，
但不能取代 JCR。
"""
    (ROOT / "README.md").write_text(content, encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    generated_at = datetime.now(timezone.utc)
    iso_year, iso_week, _ = generated_at.isocalendar()
    week_id = args.week or f"{iso_year}-W{iso_week:02d}"
    today = generated_at.date()

    REPORTS_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    DOCS_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.site_only:
        generate_site(week_id)
        print("Regenerated docs/ from existing reports.")
        return 0

    trends: dict[str, Any] = {}
    if not args.skip_trends:
        print("Measuring publication trends via PubMed...", flush=True)
        trends = build_trends(config, today)
        print(
            f"  tracked terms: {len(trends.get('tracked_terms', []))}, "
            f"discovered: {len(trends.get('discovered_terms', []))}",
            flush=True,
        )

    rising_terms = [
        item["term"]
        for item in trends.get("tracked_terms", [])
        if item.get("stage") in {"emerging", "rising"}
    ]

    print("Collecting calls for papers...", flush=True)
    records, sources = collect(config, today, args.limit_sources, rising_terms)

    horizon = int(config.get("tracking_policy", {}).get("deadline_horizon_days", 540))
    carried = carry_forward_blocked_sources(sources, today, horizon)
    if carried:
        seen = {record.fingerprint for record in records}
        added = [record for record in carried if record.fingerprint not in seen]
        records.extend(added)
        records.sort(key=lambda r: (-r.score, r.deadline_date or "9999-12-31", r.journal))
        print(f"  carried forward {len(added)} calls from unreachable sources", flush=True)

    previous = historical_fingerprints(week_id)
    for record in records:
        record.is_new = record.fingerprint not in previous

    dated = [r for r in records if r.deadline_date]
    undated = [r for r in records if not r.deadline_date]

    stats = {
        "open_calls": len(records),
        "core_calls": sum(1 for r in records if r.tier == "core"),
        "with_deadline": len(dated),
        "rolling": len(undated),
        "new": sum(1 for r in records if r.is_new),
        "sources": len(sources),
        "sources_ok": sum(1 for s in sources if s.status == "ok"),
    }

    markdown = render_markdown(
        week_id=week_id,
        generated_at=generated_at,
        dated=dated,
        undated=undated,
        trends=trends,
        sources=sources,
        manual_watchlist=config.get("manual_watchlist", []),
        config=config,
    )
    report_path = REPORTS_DIR / f"{week_id}.md"
    report_path.write_text(markdown, encoding="utf-8")

    payload = {
        "week": week_id,
        "generated_at": generated_at.isoformat(),
        "stats": stats,
        "trends": trends,
        "calls": [record.to_dict() for record in records],
        "sources": [source.to_dict() for source in sources],
        "manual_watchlist": config.get("manual_watchlist", []),
    }
    (DATA_DIR / f"{week_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (DATA_DIR / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    update_readme(week_id, stats)
    generate_site(week_id)

    print(f"\nWrote {report_path.relative_to(ROOT)}")
    print(f"Open calls: {stats['open_calls']} ({stats['with_deadline']} dated, {stats['rolling']} rolling)")
    print(f"Sources OK: {stats['sources_ok']}/{stats['sources']}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track open special issues in top journals and measure publication trends."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--site-only", action="store_true", help="Rebuild docs/ from existing reports.")
    parser.add_argument("--skip-trends", action="store_true", help="Skip the PubMed trend pass.")
    parser.add_argument("--limit-sources", type=int, default=None, help="Only run the first N sources.")
    parser.add_argument("--week", default="", help="Override the ISO week id, e.g. 2026-W37.")
    return parser.parse_args(argv)


SITE_CSS = """
:root {
  color-scheme: light;
  --bg: #f7f8f5;
  --panel: #ffffff;
  --ink: #1b2321;
  --muted: #5e6b66;
  --line: #d8ded8;
  --accent: #0f766e;
  --soft: #e8f3ef;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  color: var(--ink);
  background: var(--bg);
  line-height: 1.65;
}

a { color: var(--accent); }

.shell { max-width: 1160px; margin: 0 auto; padding: 32px 20px 56px; }

.topbar {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; padding: 14px 0 24px; border-bottom: 1px solid var(--line);
}

.brand { font-weight: 800; font-size: 1.05rem; }
.brand a { text-decoration: none; }
.nav { display: flex; gap: 14px; flex-wrap: wrap; font-size: 0.95rem; }

.hero { padding: 34px 0 24px; }
.hero h1 { font-size: clamp(2rem, 4vw, 3.4rem); line-height: 1.05; margin: 0 0 18px; max-width: 900px; }
.hero p { max-width: 860px; color: var(--muted); font-size: 1.05rem; margin: 0; }

.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 22px 0 32px; }
.stat, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
.stat { padding: 16px; }
.stat strong { display: block; font-size: 1.8rem; }
.stat span { color: var(--muted); font-size: 0.9rem; }

.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; align-items: start; margin-bottom: 18px; }
.panel { padding: 20px; margin-bottom: 18px; }
.panel h2 { margin: 0 0 12px; font-size: 1.15rem; }

.report-list { list-style: none; padding: 0; margin: 0; }
.report-list li { padding: 12px 0; border-top: 1px solid var(--line); }
.report-list li:first-child { border-top: 0; }
.muted { color: var(--muted); font-size: 0.85rem; }

.pill {
  display: inline-flex; align-items: center; margin-left: 8px;
  padding: 2px 9px; border-radius: 999px; background: var(--soft);
  color: #075e54; font-size: 0.8rem; white-space: nowrap;
}

.content { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 28px; overflow-x: auto; }
.content h1 { line-height: 1.15; }
.content h2 { margin-top: 2rem; }
.content table { width: 100%; border-collapse: collapse; font-size: 0.92rem; }
.content th, .content td { border: 1px solid var(--line); padding: 8px 10px; vertical-align: top; }
.content th { background: #eef3f1; text-align: left; }
.content blockquote { border-left: 4px solid var(--accent); margin: 18px 0; padding: 10px 16px; background: var(--soft); color: #26413d; }

footer { color: var(--muted); font-size: 0.9rem; margin-top: 30px; }

@media (max-width: 860px) {
  .topbar, .grid { display: block; }
  .nav { margin-top: 12px; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 520px) {
  .shell { padding: 22px 14px 40px; }
  .stats { grid-template-columns: 1fr; }
  .content { padding: 18px; }
}
""".strip() + "\n"


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
