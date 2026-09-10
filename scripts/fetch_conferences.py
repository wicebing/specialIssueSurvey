"""Weekly conference deadline collection and page generation.

Companion to :mod:`scripts.fetch_cfps`. Where that module tracks journal
special issues, this one tracks conference submission deadlines, host cities
and meeting dates, and publishes them as their own page.

Run ``python -m scripts.fetch_conferences``.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.conference_report import render_conference_markdown
from scripts.conferences import Conference, collect_conferences
from scripts.http_client import PoliteSession

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "target_conferences.json"
REPORTS_DIR = ROOT / "reports" / "conferences"
DATA_DIR = ROOT / "data" / "conferences"
DOCS_DIR = ROOT / "docs"
DOCS_CONF_DIR = DOCS_DIR / "conferences"


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def markdown_to_html(markdown_text: str) -> str:
    from scripts.fetch_cfps import markdown_to_html as convert

    return convert(markdown_text)


def render_page(title: str, body: str, depth: int = 0) -> str:
    from scripts.fetch_cfps import render_site_page

    return render_site_page(title, body, active="report" if depth else "")


def generate_conference_site(week_id: str) -> None:
    """Build the conference archive pages and the conferences landing page."""
    DOCS_CONF_DIR.mkdir(parents=True, exist_ok=True)
    report_paths = sorted(REPORTS_DIR.glob("*.md"), reverse=True)

    for path in report_paths:
        body = markdown_to_html(path.read_text(encoding="utf-8"))
        page = render_page(f"{path.stem} 會議截稿週報", f'<article class="content">{body}</article>', depth=1)
        (DOCS_CONF_DIR / f"{path.stem}.html").write_text(page, encoding="utf-8")

    latest: dict[str, Any] = {}
    latest_path = DATA_DIR / "latest.json"
    if latest_path.exists():
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
        except Exception:
            latest = {}

    stats = latest.get("stats", {})
    conferences = latest.get("conferences", [])
    upcoming = sorted(
        (c for c in conferences if c.get("status") == "open" and c.get("_next_deadline")),
        key=lambda c: c["_next_deadline"],
    )[:10]

    rows = "\n".join(
        '<li><a href="{url}">{name} {edition}</a><span class="pill">{deadline}</span>'
        '<div class="muted">{city} · {dates}</div></li>'.format(
            url=html.escape(c.get("url", "") or "#"),
            name=html.escape(c.get("name", "")),
            edition=html.escape(str(c.get("edition", ""))),
            deadline=html.escape(c.get("_next_deadline", "")),
            city=html.escape(c.get("city", "") or "地點未定"),
            dates=html.escape(c.get("dates", "") or "日期未定"),
        )
        for c in upcoming
    ) or "<li>尚無已公布的截稿日。</li>"

    archive = "\n".join(
        f'<li><a href="conferences/{p.stem}.html">{html.escape(p.stem)} 會議週報</a></li>'
        for p in report_paths[:20]
    ) or "<li>尚無週報</li>"

    body = f"""
<section class="hero">
  <h1>頂尖會議投稿截止日</h1>
  <p>每週更新。列出截稿日、<strong>舉辦城市</strong>與會議日期，已截稿的自動換成下一屆。</p>
</section>
<section class="stats" aria-label="Conference stats">
  <div class="stat"><strong>{stats.get('urgent', 0)}</strong><span>90 天內截稿</span></div>
  <div class="stat"><strong>{stats.get('open', 0)}</strong><span>已公布截稿日</span></div>
  <div class="stat"><strong>{stats.get('pending', 0)}</strong><span>下一屆待公布</span></div>
  <div class="stat"><strong>{stats.get('tracked', 0)}</strong><span>追蹤會議數</span></div>
</section>
<section class="grid">
  <div class="panel">
    <h2>最近要截稿的會議</h2>
    <ul class="report-list">{rows}</ul>
  </div>
  <aside class="panel">
    <h2>週報存檔</h2>
    <ul class="report-list">{archive}</ul>
  </aside>
</section>
"""
    (DOCS_DIR / "conferences.html").write_text(
        render_page("頂尖會議投稿截止日", body), encoding="utf-8"
    )


def run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    generated_at = datetime.now(timezone.utc)
    iso_year, iso_week, _ = generated_at.isocalendar()
    week_id = args.week or f"{iso_year}-W{iso_week:02d}"
    today = generated_at.date()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_CONF_DIR.mkdir(parents=True, exist_ok=True)

    if args.site_only:
        generate_conference_site(week_id)
        print("Regenerated docs/conferences from existing reports.")
        return 0

    print("Collecting conference deadlines...", flush=True)
    session = PoliteSession(contact_email=config.get("policy", {}).get("contact_email", ""))
    conferences, sources = collect_conferences(session, config, today)
    for source in sources:
        print(f"  {source.label}: {source.accepted}/{source.found} announced", flush=True)

    urgent_days = int(config.get("policy", {}).get("urgent_days", 90))
    open_ones = [c for c in conferences if c.status == "open" and c.next_deadline]
    stats = {
        "tracked": len(conferences),
        "open": len(open_ones),
        "urgent": sum(1 for c in open_ones if (c.days_left(today) or 0) <= urgent_days),
        "pending": sum(1 for c in conferences if c.status != "open"),
        "sources": len(sources),
    }

    markdown = render_conference_markdown(week_id, generated_at, conferences, sources, config)
    report_path = REPORTS_DIR / f"{week_id}.md"
    report_path.write_text(markdown, encoding="utf-8")

    payload = {
        "week": week_id,
        "generated_at": generated_at.isoformat(),
        "stats": stats,
        "conferences": [
            {
                **conference.to_dict(),
                # Precomputed so the site builder need not re-derive it.
                "_next_deadline": conference.next_deadline.isoformat()
                if conference.next_deadline
                else None,
                "_days_left": conference.days_left(today),
            }
            for conference in conferences
        ],
        "sources": [source.to_dict() for source in sources],
    }
    (DATA_DIR / f"{week_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (DATA_DIR / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    generate_conference_site(week_id)

    print(f"\nWrote {report_path.relative_to(ROOT)}")
    print(f"Tracked {stats['tracked']} conferences; {stats['open']} with announced deadlines, "
          f"{stats['urgent']} closing within {urgent_days} days")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track top conference submission deadlines.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--site-only", action="store_true")
    parser.add_argument("--week", default="")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
