"""Tests for conference deadline collection and rendering."""

import json
from datetime import date, datetime, timezone
from pathlib import Path

from scripts.conference_report import render_conference_markdown
from scripts.conferences import (
    Conference,
    ConferenceSourceReport,
    city_of,
    collect_curated,
    parse_conference_yaml,
)

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 10)


# --- the ccf-deadlines YAML shape ------------------------------------------

SAMPLE = """\
- title: MICCAI
  description: Medical Image Computing and Computer Assisted Intervention
  sub: MX
  rank:
    ccf: B
    core: A
  confs:
    - year: 2026
      id: miccai26
      link: https://conferences.miccai.org/2026/
      timeline:
        - abstract_deadline: '2026-02-12 23:59:59'
          deadline: '2026-02-19 23:59:59'
      timezone: UTC-8
      date: September 23-27, 2026
      place: Abu Dhabi, UAE
    - year: 2027
      id: miccai27
      link: https://conferences.miccai.org/2027/
      timeline:
        - deadline: '2027-02-18 23:59:59'
      timezone: UTC-8
      date: October 4-8, 2027
      place: Vienna, Austria
"""


def test_parses_nested_editions_and_timeline():
    parsed = parse_conference_yaml(SAMPLE)
    assert len(parsed) == 1
    entry = parsed[0]
    assert entry["title"] == "MICCAI"
    assert entry["rank"]["ccf"] == "B"
    assert len(entry["confs"]) == 2
    first = entry["confs"][0]
    assert first["year"] == 2026
    assert first["place"] == "Abu Dhabi, UAE"
    assert first["date"] == "September 23-27, 2026"
    assert first["timeline"][0]["deadline"].startswith("2026-02-19")
    assert first["timeline"][0]["abstract_deadline"].startswith("2026-02-12")


def test_parses_edition_without_abstract_deadline():
    entry = parse_conference_yaml(SAMPLE)[0]
    second = entry["confs"][1]
    assert second["year"] == 2027
    assert "abstract_deadline" not in second["timeline"][0]
    assert second["place"] == "Vienna, Austria"


# --- city extraction --------------------------------------------------------


def test_city_of_keeps_a_plain_city_and_country():
    assert city_of("Seoul, Korea") == ("Seoul, Korea", "")


def test_city_of_strips_a_venue_prefix():
    city, venue = city_of("Messe Wien Exhibition Congress Center, Vienna, Austria")
    assert city == "Vienna, Austria"
    assert "Messe Wien" in venue


def test_city_of_handles_three_part_us_addresses():
    assert city_of("Seattle, WA, United States")[0] == "Seattle, WA, United States"


def test_city_of_is_safe_on_empty_input():
    assert city_of("") == ("", "")
    assert city_of("Virtual") == ("Virtual", "")


# --- curated society entries ------------------------------------------------


def _curated(**overrides):
    entry = {
        "name": "SAEM Annual Meeting",
        "url": "https://www.saem.org/",
        "city": "Austin, TX, USA",
        "dates": "May 12-15, 2027",
        "abstract_deadline": "2027-01-06",
        "typical_month": "January",
        "relevance": "急診醫學",
    }
    entry.update(overrides)
    return {"id": "societies", "label": "Societies", "conferences": [entry]}


def test_curated_entry_with_future_deadline_is_open():
    found, report = collect_curated(None, _curated(), TODAY)
    assert found[0].status == "open"
    assert found[0].next_deadline == date(2027, 1, 6)
    assert report.accepted == 1


def test_curated_entry_with_passed_deadline_becomes_not_announced():
    """A stale config entry must not advertise a deadline that has gone."""
    found, _ = collect_curated(None, _curated(abstract_deadline="2026-01-06"), TODAY)
    assert found[0].status == "not_announced"
    assert found[0].next_deadline is None
    assert found[0].typical_month == "January"


def test_curated_entry_without_any_deadline_is_reported_not_guessed():
    found, report = collect_curated(None, _curated(abstract_deadline=""), TODAY)
    assert found[0].next_deadline is None
    assert report.accepted == 0


# --- deadline selection -----------------------------------------------------


def test_next_deadline_prefers_the_earlier_of_abstract_and_full():
    conference = Conference(
        name="X",
        abstract_deadline=date(2026, 10, 1),
        submission_deadline=date(2026, 10, 8),
    )
    assert conference.next_deadline == date(2026, 10, 1)
    assert conference.days_left(TODAY) == 21


def test_days_left_is_none_without_a_deadline():
    assert Conference(name="X").days_left(TODAY) is None


# --- report rendering -------------------------------------------------------


def _render(conferences):
    return render_conference_markdown(
        "2026-W37",
        datetime(2026, 9, 10, tzinfo=timezone.utc),
        conferences,
        [ConferenceSourceReport("s", "Source", "u")],
        {"policy": {"urgent_days": 90}},
    )


def test_report_shows_city_and_dates_for_an_open_conference():
    text = _render([
        Conference(
            name="MICCAI",
            edition="2027",
            url="https://x.org",
            submission_deadline=date(2026, 11, 20),
            city="Vienna, Austria",
            dates="October 4-8, 2027",
            status="open",
            relevance="醫學影像 AI",
        )
    ])
    assert "MICCAI" in text
    assert "Vienna, Austria" in text
    assert "October 4-8, 2027" in text
    assert "2026-11-20" in text


def test_report_warns_on_an_imminent_conference_deadline():
    text = _render([
        Conference(
            name="CHI", edition="2027", submission_deadline=date(2026, 9, 15), status="open"
        )
    ])
    assert "⚠️" in text


def test_report_lists_unannounced_conferences_with_their_typical_month():
    text = _render([
        Conference(name="ICML", status="not_announced", typical_month="January", url="https://icml.cc")
    ])
    assert "尚未公布" in text
    assert "ICML" in text
    assert "January" in text


def test_report_survives_an_empty_week():
    assert "本週行動摘要" in _render([])


# --- config integrity -------------------------------------------------------


def test_conference_config_is_valid():
    from scripts.conferences import COLLECTORS

    config = json.loads((ROOT / "config" / "target_conferences.json").read_text(encoding="utf-8"))
    assert config["conference_sources"]
    for spec in config["conference_sources"]:
        assert spec["collector"] in COLLECTORS
        assert spec["conferences"]
        for item in spec["conferences"]:
            if spec["collector"] == "ccf_dataset":
                assert item["category"] and item["slug"]
            else:
                assert item["name"] and item["url"]


# --- venue prefixes observed across the live feed ---------------------------

VENUE_CASES = [
    ("Hilton Union Square, San Francisco, USA", "San Francisco, USA"),
    ("Messe Wien Exhibition Congress Center, Vienna, Austria", "Vienna, Austria"),
    ("ADNEC Centre, Abu Dhabi, U.A.E.", "Abu Dhabi, U.A.E."),
    ("David L. Lawrence Convention Center, Pittsburgh, USA", "Pittsburgh, USA"),
    ("Centre de Convencions Internacional de Barcelona, Barcelona, Spain", "Barcelona, Spain"),
    ("Fields Institute, Toronto, Canada", "Toronto, Canada"),
    ("Cordis, Hong Kong SAR, China", "Hong Kong SAR, China"),
    ("San Jose, California, USA", "San Jose, California, USA"),
    ("Seattle, WA, United States", "Seattle, WA, United States"),
    ("Seoul, Korea", "Seoul, Korea"),
    ("Abu Dhabi", "Abu Dhabi"),
    ("Virtual", "Virtual"),
]


def test_venue_prefixes_are_stripped_from_the_city_column():
    """A hotel or convention centre name must not reach the 舉辦城市 column."""
    for place, expected in VENUE_CASES:
        assert city_of(place)[0] == expected, place
