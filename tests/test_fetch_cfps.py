from datetime import datetime, timezone

from scripts.fetch_cfps import candidate_fingerprint, classify_topics, extract_deadline


def test_extract_deadline_future_english_date():
    result = extract_deadline(
        "Submission deadline: March 31, 2027. Papers should focus on emergency care.",
        today=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )
    assert result["kind"] == "date"
    assert result["date"] == "2027-03-31"


def test_extract_deadline_rolling():
    result = extract_deadline("This theme issue accepts rolling submissions.")
    assert result["kind"] == "rolling"


def test_topic_classification_matches_aliases():
    topics = [
        {
            "id": "digital_health_telecare",
            "label": "遠距照護與數位醫療 / Telecare & Digital Health",
            "keywords": ["telemedicine", "remote monitoring"],
        }
    ]
    matched = classify_topics("Special issue on telemedicine in emergency departments", topics)
    assert matched[0]["id"] == "digital_health_telecare"


def test_candidate_fingerprint_is_stable_against_tracking_query():
    one = {
        "url": "https://example.org/cfp?utm_source=x&id=1",
        "journal": "JAMIA",
        "title": "Call for Papers",
    }
    two = {
        "url": "https://example.org/cfp?id=1&utm_campaign=y",
        "journal": "JAMIA",
        "title": "Call for Papers",
    }
    assert candidate_fingerprint(one) == candidate_fingerprint(two)

