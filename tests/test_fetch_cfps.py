"""Tests for the collection pipeline and report rendering."""

import json
from datetime import date, datetime, timezone
from pathlib import Path

from scripts.cfp_sources import (
    CFPRecord,
    RawEntry,
    dedupe,
    fingerprint_for,
    generic_cfp_page,
    normalize_url,
    score_record,
)
from scripts.cfp_extract import DeadlineInfo, SubmissionStatus
from scripts.http_client import looks_like_challenge
from scripts.report import render_markdown

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 10)


def test_config_is_valid_and_every_source_names_a_known_adapter():
    from scripts.cfp_sources import ADAPTERS

    config = json.loads((ROOT / "config" / "target_journals.json").read_text(encoding="utf-8"))
    assert config["sources"], "no sources configured"
    for spec in config["sources"]:
        assert spec["adapter"] in ADAPTERS, f"{spec['id']} uses unknown adapter {spec['adapter']}"
        assert spec["url"].startswith("https://")
        assert spec["id"]


def test_trend_engine_config_is_present():
    config = json.loads((ROOT / "config" / "target_journals.json").read_text(encoding="utf-8"))
    engine = config["trend_engine"]
    assert engine["pubmed_journals"]
    assert engine["tracked_terms"]
    assert all("term" in t for t in engine["tracked_terms"])


def test_blocked_publishers_are_listed_rather_than_silently_dropped():
    """A journal we cannot fetch must still be named, or its absence misleads."""
    config = json.loads((ROOT / "config" / "target_journals.json").read_text(encoding="utf-8"))
    names = {item["journal"] for item in config["manual_watchlist"]}
    for journal in ["Resuscitation", "JAMA Network Open", "The Lancet Digital Health"]:
        assert journal in names


# --- fingerprinting and dedupe ---------------------------------------------


def test_fingerprint_ignores_tracking_parameters():
    one = fingerprint_for("JAMIA", "Call for Papers", "https://x.org/cfp?utm_source=a&id=1")
    two = fingerprint_for("JAMIA", "Call for Papers", "https://x.org/cfp?id=1&utm_campaign=b")
    assert one == two


def test_normalize_url_strips_trailing_slash_and_fragment():
    assert normalize_url("https://X.org/a/b/#frag") == "https://x.org/a/b"


def test_dedupe_keeps_the_higher_scoring_duplicate():
    low = CFPRecord(journal="J", title="T", url="https://x.org/1", score=10, fingerprint="f1")
    high = CFPRecord(journal="J", title="T", url="https://x.org/1", score=80, fingerprint="f1")
    assert dedupe([low, high])[0].score == 80


# --- scoring ----------------------------------------------------------------


def _score(days_left, priority=5, band="Q1/top20_watchlist", topics=(), terms=(), text=""):
    deadline = DeadlineInfo(date=TODAY, text="x") if days_left is not None else DeadlineInfo()
    status = SubmissionStatus(
        state="open" if days_left is not None else "unknown", days_left=days_left
    )
    return score_record(deadline, status, topics, priority, band, terms, text)


def test_comfortable_runway_scores_above_a_call_closing_in_days():
    assert _score(90) > _score(5)


def test_watchlist_journal_scores_above_an_unverified_one():
    assert _score(90, band="Q1/top20_watchlist") > _score(90, band="unverified_candidate")


def test_matching_a_rising_topic_raises_the_score():
    plain = _score(90, text="a study of sepsis")
    boosted = _score(90, terms=["large language model"], text="a large language model study")
    assert boosted > plain


def test_score_is_bounded():
    assert 0 <= _score(90, priority=99, topics=[{}] * 9) <= 100


# --- challenge detection ----------------------------------------------------


def test_springer_style_challenge_page_is_detected():
    page = "<html><head><title>Client Challenge</title></head><body><noscript>x</noscript></body></html>"
    assert looks_like_challenge(page)


def test_real_listing_page_is_not_flagged_as_a_challenge():
    page = "<html><body>" + ("<article>Special Issue on Sepsis</article>" * 400) + "</body></html>"
    assert not looks_like_challenge(page)


def test_json_payload_is_never_flagged():
    assert not looks_like_challenge('{"results": []}', content_type="application/json")


# --- generic adapter stays strict -------------------------------------------


class _FakeSession:
    def __init__(self, html_text):
        self.html = html_text
        self.contact_email = ""

    def get(self, url, params=None, use_cache=True, allow_curl_fallback=True):
        from scripts.http_client import FetchResult

        return FetchResult(url=url, final_url=url, status_code=200, text=self.html, ok=True)


def test_generic_adapter_ignores_navigation_even_when_page_mentions_special_issues():
    """The exact shape of the original bug: chrome links on a CFP-ish page."""
    page = """
    <html><body>
      <h1>Calls for papers</h1>
      <a href="/login">Log in</a>
      <a href="#main">Skip to main content</a>
      <a href="/journal/x/submission-guidelines">Submission guidelines</a>
      <a href="/collections/abc">Special Issue on Prehospital Triage</a>
    </body></html>
    """
    entries, report = generic_cfp_page(_FakeSession(page), {"id": "s", "url": "https://x.org/p"})
    titles = [entry.title for entry in entries]
    assert titles == ["Special Issue on Prehospital Triage"]
    assert report.found == 1


# --- report rendering -------------------------------------------------------


def _record(**kwargs):
    base = dict(
        journal="Critical Care",
        title="Special Issue on Sepsis Phenotyping",
        url="https://x.org/c/1",
        summary="sepsis phenotyping with large language model methods",
        deadline={"date": "2026-12-01", "text": "1 December 2026"},
        status={"state": "open", "days_left": 82, "reason": "deadline is in the future"},
        topics=[{"id": "t", "label": "重症 / Critical Care"}],
        score=90,
        fingerprint="fp1",
    )
    base.update(kwargs)
    return CFPRecord(**base)


def _render(dated=(), undated=(), trends=None, manual=()):
    return render_markdown(
        week_id="2026-W37",
        generated_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        dated=list(dated),
        undated=list(undated),
        trends=trends or {},
        sources=[],
        manual_watchlist=list(manual),
        config={},
    )


def test_report_lists_a_dated_open_call_with_its_deadline():
    text = _render(dated=[_record()])
    assert "Special Issue on Sepsis Phenotyping" in text
    assert "2026-12-01" in text
    assert "82 天" in text


def test_report_flags_an_imminent_deadline():
    urgent = _record(deadline={"date": "2026-09-18"}, status={"state": "open", "days_left": 8})
    assert "⚠️" in _render(dated=[urgent])


def test_report_separates_rolling_calls_from_dated_ones():
    rolling = _record(
        deadline={}, status={"state": "open", "reason": "page says 'open for submissions'"}
    )
    text = _render(undated=[rolling])
    assert "滾動徵稿" in text


def test_report_names_blocked_publishers():
    text = _render(manual=[{"journal": "Resuscitation", "publisher": "Elsevier", "block": "captcha", "url": "https://x"}])
    assert "Resuscitation" in text
    assert "不代表沒有徵稿" in text


def test_report_matches_a_rising_topic_to_an_open_call():
    trends = {
        "tracked_terms": [
            {
                "term": "large language model",
                "label": "大型語言模型 / LLM",
                "stage": "rising",
                "recent_count": 120,
                "baseline_count": 25,
                "growth_ratio": 4.4,
            }
        ],
        "discovered_terms": [],
        "windows": {},
        "corpus": {},
        "journals_tracked": [],
    }
    text = _render(dated=[_record()], trends=trends)
    assert "建議切入點" in text
    assert "大型語言模型" in text


def test_report_handles_an_empty_week_without_crashing():
    text = _render()
    assert "確認開放投稿" in text


# --- carry-forward when a publisher blocks us -------------------------------


def test_carry_forward_only_triggers_for_fetch_failures(tmp_path, monkeypatch):
    """A source that answered fine with zero results must not be back-filled."""
    from scripts import fetch_cfps
    from scripts.cfp_sources import SourceReport

    monkeypatch.setattr(fetch_cfps, "DATA_DIR", tmp_path)
    (tmp_path / "latest.json").write_text(
        json.dumps({
            "week": "2026-W36",
            "calls": [{
                "source_id": "s1", "journal": "Critical Care", "title": "Sepsis",
                "url": "https://x.org/1", "deadline": {"date": "2027-01-01"},
                "status": {"state": "open"}, "fingerprint": "fp1", "tier": "core",
            }],
        }),
        encoding="utf-8",
    )

    healthy = SourceReport("s1", "Critical Care", "u", status="ok", accepted=0)
    assert fetch_cfps.carry_forward_blocked_sources([healthy], TODAY, 540) == []

    blocked = SourceReport("s1", "Critical Care", "u", status="bot_challenge", accepted=0)
    carried = fetch_cfps.carry_forward_blocked_sources([blocked], TODAY, 540)
    assert len(carried) == 1
    assert carried[0].carried_forward
    assert carried[0].last_verified == "2026-W36"
    assert blocked.accepted == 1


def test_carry_forward_drops_calls_whose_deadline_passed(tmp_path, monkeypatch):
    from scripts import fetch_cfps
    from scripts.cfp_sources import SourceReport

    monkeypatch.setattr(fetch_cfps, "DATA_DIR", tmp_path)
    (tmp_path / "latest.json").write_text(
        json.dumps({
            "week": "2026-W36",
            "calls": [{
                "source_id": "s1", "journal": "Critical Care", "title": "Expired",
                "url": "https://x.org/1", "deadline": {"date": "2026-08-01"},
                "status": {"state": "open"}, "fingerprint": "fp1",
            }],
        }),
        encoding="utf-8",
    )
    blocked = SourceReport("s1", "Critical Care", "u", status="bot_challenge", accepted=0)
    assert fetch_cfps.carry_forward_blocked_sources([blocked], TODAY, 540) == []


# --- field grouping ---------------------------------------------------------


def test_report_groups_core_calls_by_field():
    """With dozens of journals a flat table is unreadable, so fields become sections."""
    text = _render(dated=[
        _record(field="護理", title="Nursing Workforce Retention", fingerprint="a", tier="core"),
        _record(field="影像與電腦視覺", title="Foundation Models for Radiology", fingerprint="b", tier="core"),
    ])
    assert "### 護理" in text
    assert "### 影像與電腦視覺" in text
    assert "Nursing Workforce Retention" in text
    assert "Foundation Models for Radiology" in text


def test_field_order_puts_emergency_medicine_before_general_fields():
    from scripts.report import _group_by_field

    grouped = _group_by_field([
        _record(field="一般醫學", fingerprint="a"),
        _record(field="急診醫學", fingerprint="b"),
        _record(field="護理", fingerprint="c"),
    ])
    assert [name for name, _ in grouped] == ["急診醫學", "護理", "一般醫學"]


def test_unknown_field_sorts_last_and_is_labelled():
    from scripts.report import _group_by_field

    grouped = _group_by_field([
        _record(field="", fingerprint="a"),
        _record(field="急診醫學", fingerprint="b"),
    ])
    assert grouped[-1][0] == "其他"


def test_blocked_journals_are_sorted_by_impact_factor():
    text = _render(manual=[
        {"journal": "Low IF Journal", "publisher": "X", "impact_factor": 5.1,
         "url": "https://a", "field": "護理"},
        {"journal": "High IF Journal", "publisher": "Y", "impact_factor": 12.4,
         "url": "https://b", "field": "護理"},
    ])
    assert text.index("High IF Journal") < text.index("Low IF Journal")
    assert "12.4" in text
