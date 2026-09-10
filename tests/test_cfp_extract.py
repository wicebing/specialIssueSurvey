"""Regression tests built from the real false positives in reports/2026-W37.md.

Every rejection case below is a row that actually shipped in a published weekly
report while presenting itself as a call for papers.
"""

from datetime import date

import pytest

from scripts.cfp_extract import (
    detect_submission_status,
    extract_deadline_near_cue,
    is_navigation_chrome,
    looks_like_cfp_title,
    validate_candidate,
)

TODAY = date(2026, 9, 10)


# --- the rows that shipped as CFPs but are navigation chrome ---------------

SHIPPED_JUNK = [
    ("Skip to main content", "https://link.springer.com/journal/13049#main"),
    ("Log in", "https://idp.springer.com/auth/personal/springernature?redirect_uri=x"),
    ("Cookie settings", "https://emj.bmj.com/pages/special-issues-and-collections#!"),
    (
        "Sign up for alerts Get notified when new articles are published.",
        "https://journal-alerts.springernature.com/subscribe?journal_id=13054",
    ),
    ("Language editing", "https://authorservices.springernature.com/go/sn/"),
    ("Pre-submission checklist", "https://link.springer.com/pre-submission?journalId=13054"),
    ("Submission guidelines", "https://link.springer.com/journal/13054/submission-guidelines"),
    ("How to publish with us", "https://link.springer.com/journal/13054/how-to-publish-with-us"),
    ("Submit your manuscript", "https://submission.nature.com/new-submission/13054/3"),
    ("Journal updates", "https://link.springer.com/journal/13054/updates"),
    ("Featured Articles", "https://www.embs.org/jbhi/featured-articles/"),
    ("Current Issue", "https://ieeexplore.ieee.org/xpl/mostRecentIssue.jsp?punumber=6221020"),
    ("Popular Articles", "https://ieeexplore.ieee.org/xpl/topAccessedArticles.jsp?punumber=6221020"),
    ("Past Call for Special Issues", "https://www.embs.org/jbhi/past-call-for-special-issues/"),
    ("Past Special Issues", "https://www.embs.org/jbhi/past-special-issues/"),
    ("JBHI on Xplore", "https://ieeexplore.ieee.org/xpl/RecentIssue.jsp?punumber=6221020"),
    ("Explore JBHI", "https://www.embs.org/jbhi/"),
]


@pytest.mark.parametrize("title,url", SHIPPED_JUNK)
def test_shipped_junk_rows_are_rejected(title, url):
    result = validate_candidate(title, url, entry_text=title, today=TODAY)
    assert not result.accepted, f"{title!r} should never reach the report"


@pytest.mark.parametrize("title,url", SHIPPED_JUNK)
def test_shipped_junk_rows_are_recognised_as_chrome(title, url):
    assert is_navigation_chrome(title, url) or not looks_like_cfp_title(title, url)


# --- real calls must survive the gate --------------------------------------

REAL_CALLS = [
    (
        "Call for Papers - Theme Issue: Ambient AI Scribes and AI-Driven Documentation Technologies",
        "https://medinform.jmir.org/announcements/601",
    ),
    (
        "Theme Issue 2024: Health Natural Language Processing and Applications with Large Language Models",
        "https://medinform.jmir.org/announcements/533",
    ),
    (
        "Special Issue on Federated Learning for Critical Care Prediction",
        "https://www.embs.org/jbhi/special-issues/federated-learning",
    ),
    (
        "Machine Learning for Prehospital Triage",
        "https://link.springer.com/collections/abcdefg",
    ),
]


@pytest.mark.parametrize("title,url", REAL_CALLS)
def test_real_calls_pass_the_title_gate(title, url):
    assert looks_like_cfp_title(title, url)


def test_real_call_with_deadline_is_accepted_and_open():
    result = validate_candidate(
        "Call for Papers: Large Language Models in Emergency Triage",
        "https://medinform.jmir.org/announcements/601",
        entry_text="Guest editors invite original research. Submission deadline: 31 March 2027.",
        today=TODAY,
    )
    assert result.accepted
    assert result.deadline.date == date(2027, 3, 31)
    assert result.status.state == "open"
    assert result.status.days_left == (date(2027, 3, 31) - TODAY).days


# --- the fabricated-deadline bug -------------------------------------------


def test_no_deadline_cue_yields_no_date():
    """The old parser turned any stray date into a deadline. This one must not."""
    text = (
        "Critical Care is a leading journal. Copyright 2026 BioMed Central. "
        "Published 17 September 2026. Read the latest issue."
    )
    assert not extract_deadline_near_cue(text, today=TODAY).found


def test_date_far_from_cue_is_not_captured():
    text = (
        "Submission deadline: see the guest editorial for details. "
        + "Filler sentence. " * 30
        + "The conference was held on 17 September 2026."
    )
    assert not extract_deadline_near_cue(text, today=TODAY).found


def test_deadline_next_to_cue_is_captured():
    info = extract_deadline_near_cue(
        "Deadline for manuscript submissions: 30 November 2026.", today=TODAY
    )
    assert info.date == date(2026, 11, 30)
    assert info.precision == "day"
    assert "30 November 2026" in info.text


def test_iso_and_chinese_date_formats():
    assert extract_deadline_near_cue("截稿：2027-01-15", today=TODAY).date == date(2027, 1, 15)
    assert extract_deadline_near_cue("截止日期 2026年12月31日", today=TODAY).date == date(2026, 12, 31)


def test_month_precision_resolves_to_month_end():
    info = extract_deadline_near_cue("Submission deadline: November 2026", today=TODAY)
    assert info.date == date(2026, 11, 30)
    assert info.precision == "month"


# --- open / closed ----------------------------------------------------------


def test_explicitly_closed_collection_is_rejected():
    result = validate_candidate(
        "Special Issue on Sepsis Biomarkers",
        "https://link.springer.com/collections/xyz",
        entry_text="This collection is now closed to new submissions.",
        today=TODAY,
    )
    assert not result.accepted
    assert result.status.state == "closed"


def test_expired_deadline_is_rejected_even_with_open_wording():
    result = validate_candidate(
        "Call for Papers: Wearables in Cardiac Arrest",
        "https://example.org/special-issue/wearables",
        entry_text="Open for submissions. Submission deadline: 1 March 2026.",
        today=TODAY,
    )
    assert not result.accepted
    assert result.status.state == "closed"


def test_open_wording_without_deadline_is_open_but_undated():
    status = detect_submission_status("This collection is open for submissions.", today=TODAY)
    assert status.state == "open"
    assert status.days_left is None


def test_unknown_when_nothing_is_stated():
    assert detect_submission_status("A guest edited collection.", today=TODAY).state == "unknown"


def test_publisher_markup_status_outranks_prose():
    status = detect_submission_status(
        "Some ambiguous prose.", today=TODAY, explicit_state="closed"
    )
    assert status.state == "closed"


def test_horizon_filter_drops_far_future_calls():
    result = validate_candidate(
        "Special Issue on Digital Twins",
        "https://example.org/special-issue/twins",
        entry_text="Submission deadline: 31 December 2030.",
        today=TODAY,
        horizon_days=365,
    )
    assert not result.accepted
    assert "horizon" in result.reject_reason


def test_publisher_open_label_does_not_resurrect_an_expired_call():
    """IEEE lists expired calls under an 'accepting submissions' heading."""
    result = validate_candidate(
        "Special Issue on Quantum Key Distribution",
        "https://example.org/special-issue/qkd",
        entry_text="Submission Deadline: 31 August 2026",
        today=TODAY,
        explicit_state="open",
    )
    assert not result.accepted
    assert result.status.state == "closed"


def test_publisher_open_label_stands_when_no_deadline_is_given():
    status = detect_submission_status("Ongoing collection.", today=TODAY, explicit_state="open")
    assert status.state == "open"


def test_publisher_closed_label_always_wins():
    status = detect_submission_status(
        "Submission deadline: 31 December 2027", today=TODAY, explicit_state="closed"
    )
    assert status.state == "closed"
