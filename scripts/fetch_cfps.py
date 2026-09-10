from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse, urlunparse

try:
    import markdown as markdown_lib
except ImportError:  # pragma: no cover - exercised when dependencies are not installed yet
    markdown_lib = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

try:
    from dateparser.search import search_dates
except ImportError:  # pragma: no cover
    search_dates = None


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "target_journals.json"
REPORTS_DIR = ROOT / "reports"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
DOCS_REPORTS_DIR = DOCS_DIR / "reports"
CSS_PATH = DOCS_DIR / "assets" / "site.css"


@dataclass
class SourceHealth:
    source_id: str
    label: str
    url: str
    status: str
    candidates: int = 0
    error: str = ""
    status_code: int | None = None


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def now_in_timezone(_tz_name: str) -> datetime:
    # GitHub Actions normally runs in UTC. The report records UTC explicitly and
    # uses ISO week naming so local and hosted runs produce stable file names.
    return datetime.now(timezone.utc)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    filtered_query = {
        key: values
        for key, values in query.items()
        if not key.lower().startswith(("utm_", "fbclid", "gclid"))
    }
    query_string = "&".join(
        f"{quote_plus(key)}={quote_plus(value)}"
        for key, values in sorted(filtered_query.items())
        for value in values
    )
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            "",
            query_string,
            "",
        )
    )


def candidate_fingerprint(candidate: dict[str, Any]) -> str:
    stable = "|".join(
        [
            normalize_url(candidate.get("url", "")),
            clean_text(candidate.get("journal", "")).lower(),
            clean_text(candidate.get("title", "")).lower(),
        ]
    )
    return hashlib.sha1(stable.encode("utf-8")).hexdigest()[:16]


def build_headers(config: dict[str, Any]) -> dict[str, str]:
    policy = config.get("tracking_policy", {})
    user_agent = policy.get(
        "user_agent",
        "AcademicCFPTracker/1.0 (+https://github.com/academic-cfp-tracker)",
    )
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8",
    }


def request_page(session: requests.Session, url: str, config: dict[str, Any]) -> requests.Response:
    if requests is None:
        raise RuntimeError("The 'requests' package is required for live collection. Run pip install -r requirements.txt.")
    timeout = int(config.get("tracking_policy", {}).get("direct_source_timeout_seconds", 25))
    return session.get(url, headers=build_headers(config), timeout=timeout)


def build_journal_index(config: dict[str, Any]) -> list[dict[str, Any]]:
    indexed: list[dict[str, Any]] = []
    for journal in config.get("journals", []):
        names = [journal.get("journal", ""), *journal.get("aliases", [])]
        indexed.append(
            {
                **journal,
                "_match_names": [name.lower() for name in names if name],
            }
        )
    return indexed


def identify_journal(
    text: str,
    source: dict[str, Any],
    journals: list[dict[str, Any]],
) -> dict[str, Any] | None:
    explicit = clean_text(source.get("journal"))
    if explicit:
        for journal in journals:
            if journal.get("journal") == explicit:
                return journal
        return {
            "journal": explicit,
            "category": source.get("category_hint", "Unclassified"),
            "publisher": source.get("publisher", ""),
            "priority": 2,
            "jcr_band": "source_named_verify",
            "q1_evidence": "Named by source config; verify JCR before submission.",
        }

    haystack = text.lower()
    for journal in journals:
        if any(name and name in haystack for name in journal["_match_names"]):
            return journal
    return None


def classify_topics(text: str, topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    haystack = text.lower()
    matched = []
    for topic in topics:
        hits = [
            keyword
            for keyword in topic.get("keywords", [])
            if keyword.lower() in haystack
        ]
        if hits:
            matched.append(
                {
                    "id": topic["id"],
                    "label": topic["label"],
                    "hits": sorted(set(hits), key=str.lower),
                }
            )
    return matched


def deadline_sentences(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?。；;])\s+", clean_text(text))
    terms = [
        "deadline",
        "submission",
        "manuscript",
        "closing date",
        "due date",
        "closes",
        "submit by",
        "截稿",
        "截止",
    ]
    focused = [sentence for sentence in sentences if any(term in sentence.lower() for term in terms)]
    return focused or sentences[:3]


def extract_deadline(text: str, today: datetime | None = None) -> dict[str, Any]:
    today = today or datetime.now(timezone.utc)
    normalized = clean_text(text)
    lower = normalized.lower()
    if any(token in lower for token in ["rolling", "continuous submission", "no fixed deadline", "ongoing"]):
        return {"label": "Rolling / ongoing", "date": None, "kind": "rolling"}

    snippets = deadline_sentences(normalized)
    if search_dates is not None:
        for snippet in snippets:
            try:
                found = search_dates(
                    snippet,
                    settings={
                        "PREFER_DATES_FROM": "future",
                        "RELATIVE_BASE": today.replace(tzinfo=None),
                        "RETURN_AS_TIMEZONE_AWARE": False,
                    },
                )
            except Exception:
                found = None
            if not found:
                continue
            for label, parsed in found:
                parsed_date = parsed.date()
                if parsed_date.year < today.year:
                    continue
                return {
                    "label": label,
                    "date": parsed_date.isoformat(),
                    "kind": "date",
                }

    textual = extract_textual_deadline(snippets, today)
    if textual:
        return textual

    explicit_patterns = [
        r"\b(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b",
        r"\b(0?[1-9]|[12]\d|3[01])[-/](0?[1-9]|1[0-2])[-/](20\d{2})\b",
    ]
    for pattern in explicit_patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        try:
            parts = [int(group) for group in match.groups()]
            if len(str(parts[0])) == 4:
                parsed_date = datetime(parts[0], parts[1], parts[2]).date()
            else:
                parsed_date = datetime(parts[2], parts[1], parts[0]).date()
            return {
                "label": match.group(0),
                "date": parsed_date.isoformat(),
                "kind": "date",
            }
        except ValueError:
            continue

    return {"label": "Deadline not found - verify source page", "date": None, "kind": "unknown"}


def extract_textual_deadline(snippets: list[str], today: datetime) -> dict[str, Any] | None:
    months = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }
    patterns = [
        r"\b(?P<month>[A-Za-z]+)\s+(?P<day>[0-3]?\d)(?:st|nd|rd|th)?[,]?\s+(?P<year>20\d{2})\b",
        r"\b(?P<day>[0-3]?\d)(?:st|nd|rd|th)?\s+(?P<month>[A-Za-z]+)[,]?\s+(?P<year>20\d{2})\b",
    ]
    for snippet in snippets:
        for pattern in patterns:
            for match in re.finditer(pattern, snippet):
                month_name = match.group("month").lower()
                month = months.get(month_name)
                if not month:
                    continue
                try:
                    parsed = datetime(int(match.group("year")), month, int(match.group("day"))).date()
                except ValueError:
                    continue
                if parsed.year < today.year:
                    continue
                return {"label": match.group(0), "date": parsed.isoformat(), "kind": "date"}
    return None


def is_candidate_text(text: str, phrases: list[str]) -> bool:
    lower = text.lower()
    if any(phrase.lower() in lower for phrase in phrases):
        return True
    return ("special" in lower and "issue" in lower) or ("call" in lower and "paper" in lower)


def nearby_context(anchor: Any) -> str:
    node = anchor
    for _ in range(3):
        if not getattr(node, "parent", None):
            break
        node = node.parent
        context = clean_text(node.get_text(" ", strip=True))
        if len(context) > 140:
            return context[:2200]
    return clean_text(anchor.get_text(" ", strip=True))


def parse_candidates_from_html(
    html_text: str,
    source: dict[str, Any],
    config: dict[str, Any],
    final_url: str,
) -> list[dict[str, Any]]:
    if BeautifulSoup is None:
        raise RuntimeError("The 'beautifulsoup4' package is required for live HTML parsing. Run pip install -r requirements.txt.")
    soup = BeautifulSoup(html_text, "lxml")
    phrases = config.get("tracking_policy", {}).get("candidate_phrases", [])
    topics = config.get("topic_taxonomy", [])
    journals = build_journal_index(config)
    candidates: list[dict[str, Any]] = []
    seen_links: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        title = clean_text(anchor.get_text(" ", strip=True))
        if len(title) < 4:
            continue
        url = urljoin(final_url, anchor["href"])
        normalized = normalize_url(url)
        if normalized in seen_links:
            continue
        context = nearby_context(anchor)
        combined = f"{title} {context} {url}"
        if not is_candidate_text(combined, phrases):
            continue
        journal = identify_journal(combined, source, journals)
        topics_found = classify_topics(combined, topics)
        candidates.append(
            enrich_candidate(
                {
                    "title": title,
                    "url": url,
                    "description": context,
                    "source_id": source["id"],
                    "source_label": source.get("label", source["id"]),
                    "source_url": source.get("url", final_url),
                    "publisher": source.get("publisher", ""),
                    "official_source": bool(source.get("official", False)),
                    "category_hint": source.get("category_hint", ""),
                    "journal": journal.get("journal") if journal else "",
                    "journal_category": journal.get("category") if journal else source.get("category_hint", ""),
                    "journal_priority": int(journal.get("priority", 1)) if journal else 1,
                    "jcr_band": journal.get("jcr_band", "unverified_candidate") if journal else "unverified_candidate",
                    "q1_evidence": journal.get("q1_evidence", "Not matched to configured Q1 watchlist; verify in JCR.") if journal else "Not matched to configured Q1 watchlist; verify in JCR.",
                    "topics": topics_found,
                },
                config,
            )
        )
        seen_links.add(normalized)

    return candidates


def enrich_candidate(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    today = now_in_timezone(config.get("tracking_policy", {}).get("report_timezone", "UTC"))
    deadline = extract_deadline(
        f"{candidate.get('title', '')}. {candidate.get('description', '')}",
        today=today,
    )
    candidate["deadline"] = deadline
    candidate["fingerprint"] = candidate_fingerprint(candidate)
    candidate["score"] = score_candidate(candidate, config, today)
    candidate["priority_label"] = priority_label(candidate["score"])
    return candidate


def score_candidate(candidate: dict[str, Any], config: dict[str, Any], today: datetime) -> int:
    score = 0
    if candidate.get("official_source"):
        score += 15
    if "watchlist" in candidate.get("jcr_band", ""):
        score += 30
    elif "candidate" in candidate.get("jcr_band", ""):
        score += 10
    score += min(25, int(candidate.get("journal_priority", 1)) * 4)
    score += min(20, len(candidate.get("topics", [])) * 5)

    deadline = candidate.get("deadline", {})
    if deadline.get("kind") == "rolling":
        score += 8
    elif deadline.get("date"):
        try:
            deadline_date = datetime.fromisoformat(deadline["date"]).date()
            days_left = (deadline_date - today.date()).days
            if 21 <= days_left <= 210:
                score += 18
            elif 0 <= days_left < 21:
                score += 8
            elif days_left > 210:
                score -= 10
            else:
                score -= 40
        except ValueError:
            pass
    else:
        score -= 4
    return max(0, min(100, score))


def priority_label(score: int) -> str:
    if score >= 75:
        return "High"
    if score >= 55:
        return "Medium"
    return "Watch"


def candidate_is_in_scope(candidate: dict[str, Any], config: dict[str, Any], today: datetime) -> bool:
    policy = config.get("tracking_policy", {})
    min_topic_hits = int(policy.get("minimum_topic_hits", 1))
    direct_watchlist = "watchlist" in candidate.get("jcr_band", "")
    topic_ok = len(candidate.get("topics", [])) >= min_topic_hits
    if not topic_ok and not direct_watchlist:
        return False

    deadline = candidate.get("deadline", {})
    if deadline.get("kind") == "unknown":
        return bool(policy.get("include_unknown_deadline", True))
    if deadline.get("kind") == "rolling":
        return True
    if deadline.get("date"):
        try:
            deadline_date = datetime.fromisoformat(deadline["date"]).date()
        except ValueError:
            return False
        horizon = today.date() + timedelta(days=int(policy.get("deadline_horizon_days", 210)))
        return today.date() <= deadline_date <= horizon
    return False


def fetch_page_source(
    session: requests.Session,
    source: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], SourceHealth]:
    label = source.get("label", source["id"])
    url = source["url"]
    try:
        response = request_page(session, url, config)
        if response.status_code >= 400:
            return [], SourceHealth(source["id"], label, url, "http_error", 0, "", response.status_code)
        candidates = parse_candidates_from_html(response.text, source, config, response.url)
        return candidates, SourceHealth(source["id"], label, url, "ok", len(candidates), "", response.status_code)
    except Exception as exc:
        return [], SourceHealth(source["id"], label, url, "error", 0, str(exc), None)


def brave_search(session: requests.Session, query: str, config: dict[str, Any]) -> list[dict[str, str]]:
    key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not key:
        return []
    response = session.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={**build_headers(config), "X-Subscription-Token": key},
        params={"q": query, "count": config.get("tracking_policy", {}).get("max_search_results_per_query", 8)},
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "description": item.get("description", ""),
        }
        for item in payload.get("web", {}).get("results", [])
    ]


def serpapi_search(session: requests.Session, query: str, config: dict[str, Any]) -> list[dict[str, str]]:
    key = os.environ.get("SERPAPI_API_KEY")
    if not key:
        return []
    response = session.get(
        "https://serpapi.com/search.json",
        params={
            "engine": "google",
            "q": query,
            "api_key": key,
            "num": config.get("tracking_policy", {}).get("max_search_results_per_query", 8),
        },
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "description": item.get("snippet", ""),
        }
        for item in payload.get("organic_results", [])
    ]


def duckduckgo_lite_search(session: requests.Session, query: str, config: dict[str, Any]) -> list[dict[str, str]]:
    response = session.get(
        "https://lite.duckduckgo.com/lite/",
        params={"q": query},
        headers=build_headers(config),
        timeout=25,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    results: list[dict[str, str]] = []
    for link in soup.select("a.result-link"):
        title = clean_text(link.get_text(" ", strip=True))
        href = link.get("href", "")
        if not title or not href:
            continue
        results.append({"title": title, "url": href, "description": ""})
        if len(results) >= int(config.get("tracking_policy", {}).get("max_search_results_per_query", 8)):
            break
    return results


def search_web(session: requests.Session, query: str, config: dict[str, Any]) -> tuple[list[dict[str, str]], str]:
    providers = [
        ("brave", brave_search),
        ("serpapi", serpapi_search),
        ("duckduckgo_lite", duckduckgo_lite_search),
    ]
    last_error = ""
    for provider_name, provider in providers:
        try:
            results = provider(session, query, config)
            if results:
                return results, provider_name
        except Exception as exc:
            last_error = f"{provider_name}: {exc}"
    return [], last_error or "no_provider_results"


def fetch_search_source(
    session: requests.Session,
    query_config: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], SourceHealth]:
    query = query_config["query"]
    results, provider = search_web(session, query, config)
    source = {
        "id": f"search_{query_config['id']}",
        "label": f"Web search: {query_config.get('category_hint', query_config['id'])}",
        "url": f"search://{query}",
        "publisher": provider,
        "category_hint": query_config.get("category_hint", ""),
        "official": False,
    }
    candidates = []
    topics = config.get("topic_taxonomy", [])
    journals = build_journal_index(config)
    phrases = config.get("tracking_policy", {}).get("candidate_phrases", [])
    for item in results:
        combined = f"{item.get('title', '')} {item.get('description', '')} {item.get('url', '')}"
        if not is_candidate_text(combined, phrases):
            continue
        journal = identify_journal(combined, source, journals)
        candidates.append(
            enrich_candidate(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "description": item.get("description", ""),
                    "source_id": source["id"],
                    "source_label": source["label"],
                    "source_url": source["url"],
                    "publisher": journal.get("publisher", provider) if journal else provider,
                    "official_source": False,
                    "category_hint": source["category_hint"],
                    "journal": journal.get("journal") if journal else "",
                    "journal_category": journal.get("category") if journal else source["category_hint"],
                    "journal_priority": int(journal.get("priority", 1)) if journal else 1,
                    "jcr_band": journal.get("jcr_band", "unverified_candidate") if journal else "unverified_candidate",
                    "q1_evidence": journal.get("q1_evidence", "Discovered by web search; verify official page and JCR.") if journal else "Discovered by web search; verify official page and JCR.",
                    "topics": classify_topics(combined, topics),
                },
                config,
            )
        )
    status = "ok" if results else "no_results"
    return candidates, SourceHealth(source["id"], source["label"], source["url"], status, len(candidates), "" if results else provider, None)


def collect_candidates(config: dict[str, Any], offline: bool = False, limit_sources: int | None = None) -> tuple[list[dict[str, Any]], list[SourceHealth]]:
    today = now_in_timezone(config.get("tracking_policy", {}).get("report_timezone", "UTC"))
    session = requests.Session() if requests is not None else None
    all_sources = []
    seen_source_ids = set()

    for journal in config.get("journals", []):
        if journal.get("cfp_url"):
            source = {
                "id": "journal_" + re.sub(r"[^a-z0-9]+", "_", journal["journal"].lower()).strip("_"),
                "label": f"{journal['journal']} CFP",
                "type": "page",
                "url": journal["cfp_url"],
                "publisher": journal.get("publisher", ""),
                "journal": journal["journal"],
                "official": True,
                "category_hint": journal.get("category", ""),
            }
            all_sources.append(source)
            seen_source_ids.add(source["id"])

    for source in config.get("sources", []):
        if source["id"] not in seen_source_ids:
            all_sources.append(source)

    if limit_sources is not None:
        all_sources = all_sources[:limit_sources]

    candidates: list[dict[str, Any]] = []
    health: list[SourceHealth] = []

    if offline:
        for source in all_sources:
            health.append(
                SourceHealth(
                    source["id"],
                    source.get("label", source["id"]),
                    source.get("url", ""),
                    "skipped_offline",
                    0,
                    "Offline run: network fetching disabled.",
                    None,
                )
            )
        return [], health

    if session is None:
        raise RuntimeError("Live collection requires Python dependencies. Run pip install -r requirements.txt.")

    for source in all_sources:
        source_candidates, source_health = fetch_page_source(session, source, config)
        candidates.extend(source_candidates)
        health.append(source_health)

    for query in config.get("search_queries", []):
        query_candidates, query_health = fetch_search_source(session, query, config)
        candidates.extend(query_candidates)
        health.append(query_health)

    in_scope = [candidate for candidate in candidates if candidate_is_in_scope(candidate, config, today)]
    return dedupe_candidates(in_scope), health


def dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = candidate["fingerprint"]
        current = best.get(key)
        if current is None or candidate.get("score", 0) > current.get("score", 0):
            best[key] = candidate
    return sorted(best.values(), key=lambda item: (-item.get("score", 0), item.get("deadline", {}).get("date") or "9999-99-99", item.get("journal", "")))


def historical_fingerprints(current_week: str) -> set[str]:
    fingerprints: set[str] = set()
    if not DATA_DIR.exists():
        return fingerprints
    for path in DATA_DIR.glob("*.json"):
        if path.name in {"latest.json", f"{current_week}.json"}:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in payload.get("candidates", []):
            if item.get("fingerprint"):
                fingerprints.add(item["fingerprint"])
    return fingerprints


def summarize_trends(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    examples: dict[str, str] = {}
    labels: dict[str, str] = {}
    for candidate in candidates:
        for topic in candidate.get("topics", []):
            counts[topic["id"]] += 1
            labels[topic["id"]] = topic["label"]
            examples.setdefault(topic["id"], candidate.get("title", ""))
    return [
        {"topic_id": topic_id, "label": labels[topic_id], "count": count, "example": examples.get(topic_id, "")}
        for topic_id, count in counts.most_common()
    ]


def markdown_escape_table(value: str) -> str:
    return clean_text(value).replace("|", "\\|")


def render_report(
    candidates: list[dict[str, Any]],
    health: list[SourceHealth],
    config: dict[str, Any],
    generated_at: datetime,
    week_id: str,
    offline: bool,
) -> str:
    previous = historical_fingerprints(week_id)
    for candidate in candidates:
        candidate["is_new"] = candidate.get("fingerprint") not in previous
    high_priority = [item for item in candidates if "watchlist" in item.get("jcr_band", "") and item.get("score", 0) >= 55]
    verify = [item for item in candidates if item not in high_priority]
    trends = summarize_trends(candidates)
    deadline_horizon = generated_at.date() + timedelta(days=int(config.get("tracking_policy", {}).get("deadline_horizon_days", 210)))

    lines = [
        f"# JCR Q1 / Top 20% Special Issue 與 CFP 週報：{week_id}",
        "",
        f"> 產生時間：{generated_at.strftime('%Y-%m-%d %H:%M UTC')}  ",
        f"> 截稿篩選：{generated_at.date().isoformat()} 至 {deadline_horizon.isoformat()}  ",
        f"> 追蹤核心：急診醫學、急救復甦、重症、醫學資訊、遠距照護、醫療 AI、醫療假訊息。",
        "",
    ]
    if offline:
        lines.extend(
            [
                "> 這份是離線啟動報告，尚未執行網路蒐集。第一次 GitHub Actions 或本機排程跑完後會自動更新成 live report。",
                "",
            ]
        )

    new_count = sum(1 for item in candidates if item.get("is_new"))
    lines.extend(
        [
            "## 本週摘要",
            "",
            f"- 掃描來源：{len(health)} 個官方頁面 / 搜尋入口",
            f"- 收錄候選：{len(candidates)} 筆；其中高優先 Q1 白名單：{len(high_priority)} 筆",
            f"- 本週新發現：{new_count} 筆",
            f"- 來源異常：{sum(1 for item in health if item.status not in {'ok', 'no_results', 'skipped_offline'})} 個",
            "",
        ]
    )

    lines.extend(["## 高優先 Q1 白名單 CFP", ""])
    if not high_priority:
        lines.append("_目前沒有符合期限與主題條件的高優先項目。_")
        lines.append("")
    else:
        lines.extend(
            [
                "| 優先 | 新 | 期刊 | 主題 / CFP | 截稿 | 趨勢標籤 | 來源 |",
                "| :--- | :---: | :--- | :--- | :--- | :--- | :--- |",
            ]
        )
        for item in high_priority:
            topics = ", ".join(topic["label"].split(" / ")[0] for topic in item.get("topics", [])) or "-"
            deadline = item.get("deadline", {}).get("date") or item.get("deadline", {}).get("label", "Verify")
            lines.append(
                "| {priority} | {new} | **{journal}** | [{title}]({url}) | `{deadline}` | {topics} | {source} |".format(
                    priority=item.get("priority_label", "Watch"),
                    new="NEW" if item.get("is_new") else "",
                    journal=markdown_escape_table(item.get("journal") or "未辨識期刊"),
                    title=markdown_escape_table(item.get("title", "")),
                    url=item.get("url", ""),
                    deadline=markdown_escape_table(deadline),
                    topics=markdown_escape_table(topics),
                    source=markdown_escape_table(item.get("source_label", "")),
                )
            )
        lines.append("")

    lines.extend(["## 候選清單：需人工驗證 JCR / 官方頁面", ""])
    if not verify:
        lines.append("_沒有額外候選。_")
        lines.append("")
    else:
        lines.extend(
            [
                "| 分數 | 期刊 / 類別 | 主題 / CFP | 截稿 | 驗證提醒 |",
                "| :--- | :--- | :--- | :--- | :--- |",
            ]
        )
        for item in verify:
            journal = item.get("journal") or item.get("journal_category") or "平台候選"
            deadline = item.get("deadline", {}).get("date") or item.get("deadline", {}).get("label", "Verify")
            lines.append(
                "| {score} | {journal} | [{title}]({url}) | `{deadline}` | {evidence} |".format(
                    score=item.get("score", 0),
                    journal=markdown_escape_table(journal),
                    title=markdown_escape_table(item.get("title", "")),
                    url=item.get("url", ""),
                    deadline=markdown_escape_table(deadline),
                    evidence=markdown_escape_table(item.get("q1_evidence", "")),
                )
            )
        lines.append("")

    lines.extend(["## 研究主題趨勢雷達", ""])
    if not trends:
        lines.append("_本週資料量不足，尚未形成可判讀的趨勢。_")
        lines.append("")
    else:
        for trend in trends:
            lines.append(
                f"- **{trend['label']}**：{trend['count']} 筆；例：{trend['example']}"
            )
        lines.append("")
        lines.extend(
            [
                "建議每週優先觀察：",
                "",
                "1. 是否出現 `generative AI / LLM + clinical workflow / triage` 的明確徵稿。",
                "2. 是否出現 `remote monitoring / hospital at home / wearable` 與急診或院前場域結合。",
                "3. 是否出現 `misinformation / infodemic` 與急重症、公衛應變或社群資料監測結合。",
                "",
            ]
        )

    lines.extend(["## 來源健康狀態", ""])
    lines.extend(["| 來源 | 狀態 | 候選數 | HTTP | 備註 |", "| :--- | :--- | ---: | :---: | :--- |"])
    for item in health:
        lines.append(
            f"| {markdown_escape_table(item.label)} | `{item.status}` | {item.candidates} | {item.status_code or ''} | {markdown_escape_table(item.error)} |"
        )
    lines.append("")

    lines.extend(
        [
            "## 方法與限制",
            "",
            "- 高優先清單只把 `config/target_journals.json` 內標記為 Q1/top-20% watchlist 的期刊視為白名單；年度 JCR 分區仍需用 Clarivate JCR 重新確認。",
            "- 平台候選會保留在「需人工驗證」區，避免把搜尋引擎摘要或非官方頁面誤當成真實 CFP。",
            "- 若要提高全面性，建議在 GitHub Secrets 加入 `BRAVE_SEARCH_API_KEY` 或 `SERPAPI_API_KEY`；沒有 API key 時仍會掃官方頁面並使用 DuckDuckGo Lite fallback。",
            "- 截稿日由網頁文字自動解析，投稿前務必點入官方頁面確認最新日期、客座編輯與投稿系統。",
            "",
        ]
    )
    return "\n".join(lines)


def save_json_report(
    candidates: list[dict[str, Any]],
    health: list[SourceHealth],
    trends: list[dict[str, Any]],
    generated_at: datetime,
    week_id: str,
    offline: bool,
) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    payload = {
        "week": week_id,
        "generated_at": generated_at.isoformat(),
        "offline": offline,
        "stats": {
            "candidates": len(candidates),
            "new": sum(1 for item in candidates if item.get("is_new")),
            "high_priority": sum(1 for item in candidates if "watchlist" in item.get("jcr_band", "") and item.get("score", 0) >= 55),
            "sources": len(health),
        },
        "trends": trends,
        "candidates": candidates,
        "sources": [item.__dict__ for item in health],
    }
    path = DATA_DIR / f"{week_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA_DIR / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def update_readme(week_id: str, candidates: list[dict[str, Any]], offline: bool) -> None:
    reports = sorted(REPORTS_DIR.glob("*.md"), reverse=True)
    links = "\n".join(f"- [{path.stem} 週報](reports/{path.name})" for path in reports[:20]) or "- 尚無週報"
    marker = "離線啟動報告" if offline else "最新自動週報"
    content = f"""# Academic CFP Tracker

每週追蹤 JCR Q1 / top 20% 期刊的 Special Issue 與 Call for Papers，聚焦急診醫學、急救復甦、重症醫學、醫學資訊、遠距照護、醫療 AI 與醫療假訊息。

## 最新週報

[{week_id} {marker}](reports/{week_id}.md)  
本期收錄：{len(candidates)} 筆候選。

GitHub Pages 入口會由 `docs/` 自動產生；啟用 Pages 後可直接用網頁瀏覽歷史週報。

## 歷史週報

{links}

## 快速開始

```powershell
python -m venv .venv
.\\.venv\\Scripts\\python -m pip install -r requirements.txt
.\\.venv\\Scripts\\python scripts\\fetch_cfps.py
```

本機每週自動跑：

```powershell
.\\scripts\\install_windows_task.ps1 -RepositoryPath "{ROOT}"
```

若要提高搜尋覆蓋率，可在本機環境或 GitHub Secrets 加入 `BRAVE_SEARCH_API_KEY` 或 `SERPAPI_API_KEY`。

## Q1 驗證原則

`config/target_journals.json` 是人工維護的 JCR Q1 / top-20% 白名單。每年 JCR 更新後，請用 Clarivate JCR 重新核對期刊名稱、category 與 JIF rank；SCImago 可作為公開替代參考，但不能取代 JCR。
"""
    (ROOT / "README.md").write_text(content, encoding="utf-8")


def ensure_css() -> None:
    CSS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CSS_PATH.exists():
        return
    CSS_PATH.write_text(
        """
:root {
  color-scheme: light;
  --bg: #f7f8f5;
  --panel: #ffffff;
  --ink: #1b2321;
  --muted: #5e6b66;
  --line: #d8ded8;
  --accent: #0f766e;
  --accent-2: #7c2d12;
  --soft: #e8f3ef;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--ink);
  background: var(--bg);
  line-height: 1.65;
}

a { color: var(--accent); }

.shell {
  max-width: 1160px;
  margin: 0 auto;
  padding: 32px 20px 56px;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 0 24px;
  border-bottom: 1px solid var(--line);
}

.brand {
  font-weight: 800;
  letter-spacing: 0;
  font-size: 1.05rem;
}

.nav {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 0.95rem;
}

.hero {
  padding: 34px 0 24px;
}

.hero h1 {
  font-size: clamp(2rem, 4vw, 4.3rem);
  line-height: 1.03;
  margin: 0 0 18px;
  max-width: 900px;
}

.hero p {
  max-width: 860px;
  color: var(--muted);
  font-size: 1.05rem;
  margin: 0;
}

.stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin: 22px 0 32px;
}

.stat, .panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}

.stat {
  padding: 16px;
}

.stat strong {
  display: block;
  font-size: 1.8rem;
}

.stat span {
  color: var(--muted);
  font-size: 0.9rem;
}

.grid {
  display: grid;
  grid-template-columns: 1.25fr 0.75fr;
  gap: 18px;
  align-items: start;
}

.panel {
  padding: 20px;
}

.panel h2 {
  margin: 0 0 12px;
  font-size: 1.15rem;
}

.report-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.report-list li {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 0;
  border-top: 1px solid var(--line);
}

.report-list li:first-child { border-top: 0; }

.pill {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  padding: 3px 9px;
  border-radius: 999px;
  background: var(--soft);
  color: #075e54;
  font-size: 0.82rem;
  white-space: nowrap;
}

.content {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 28px;
  overflow-x: auto;
}

.content h1 { line-height: 1.15; }
.content h2 { margin-top: 2rem; }
.content table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.92rem;
}
.content th, .content td {
  border: 1px solid var(--line);
  padding: 8px 10px;
  vertical-align: top;
}
.content th {
  background: #eef3f1;
  text-align: left;
}
.content blockquote {
  border-left: 4px solid var(--accent);
  margin: 18px 0;
  padding: 10px 16px;
  background: var(--soft);
  color: #26413d;
}

footer {
  color: var(--muted);
  font-size: 0.9rem;
  margin-top: 30px;
}

@media (max-width: 860px) {
  .topbar, .grid { display: block; }
  .nav { margin-top: 12px; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .panel { margin-bottom: 16px; }
}

@media (max-width: 520px) {
  .shell { padding: 22px 14px 40px; }
  .stats { grid-template-columns: 1fr; }
  .content { padding: 18px; }
  .report-list li { display: block; }
  .pill { margin-top: 8px; }
}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def render_site_page(title: str, body: str, active: str = "") -> str:
    prefix = "../" if active == "report" else ""
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <meta name="description" content="Weekly tracker for JCR Q1 special issues and calls for papers in emergency medicine, critical care, medical informatics, digital health, and healthcare AI.">
  <link rel="stylesheet" href="{prefix}assets/site.css">
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div class="brand"><a href="{prefix}index.html">Academic CFP Tracker</a></div>
      <nav class="nav" aria-label="Primary">
        <a href="{prefix}index.html">首頁</a>
        <a href="{prefix}../README.md">Repo README</a>
        <a href="{prefix}../config/target_journals.json">設定檔</a>
      </nav>
    </header>
    {body}
    <footer>Generated by Academic CFP Tracker. Verify all deadlines and JCR ranks before submission.</footer>
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
            text = html.escape(line[level:].strip())
            output.append(f"<h{level}>{text}</h{level}>")
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
            row = "".join(f"<td>{cell}</td>" for cell in cells)
            output.append(f"<tr>{row}</tr>")
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
    latest_payload = {}
    latest_json = DATA_DIR / "latest.json"
    if latest_json.exists():
        try:
            latest_payload = json.loads(latest_json.read_text(encoding="utf-8"))
        except Exception:
            latest_payload = {}

    for path in report_paths:
        body = markdown_to_html(path.read_text(encoding="utf-8"))
        html_doc = render_site_page(f"{path.stem} CFP Report", f'<article class="content">{body}</article>', active="report")
        (DOCS_REPORTS_DIR / f"{path.stem}.html").write_text(html_doc, encoding="utf-8")

    stats = latest_payload.get("stats", {})
    trends = latest_payload.get("trends", [])
    reports_html = "\n".join(
        f'<li><a href="reports/{path.stem}.html">{html.escape(path.stem)} 週報</a><span class="pill">Markdown: <a href="../reports/{path.name}">raw</a></span></li>'
        for path in report_paths[:20]
    ) or "<li>尚無週報</li>"
    trends_html = "\n".join(
        f"<li><strong>{html.escape(item.get('label', ''))}</strong>：{item.get('count', 0)} 筆</li>"
        for item in trends[:8]
    ) or "<li>等待第一次 live 蒐集後產生趨勢。</li>"
    latest_week = latest_payload.get("week") or week_id or "尚未產生"
    body = f"""
<section class="hero">
  <h1>每週追蹤頂尖醫學期刊 Special Issue 與 CFP</h1>
  <p>聚焦急診、急救復甦、重症、醫學資訊、遠距照護、醫療 AI 與醫療假訊息。白名單來源以 JCR Q1 / top 20% 期刊為核心，搜尋候選會另外標示需人工驗證。</p>
</section>
<section class="stats" aria-label="Latest report stats">
  <div class="stat"><strong>{stats.get('candidates', 0)}</strong><span>本期候選</span></div>
  <div class="stat"><strong>{stats.get('high_priority', 0)}</strong><span>高優先 Q1</span></div>
  <div class="stat"><strong>{stats.get('new', 0)}</strong><span>本週新發現</span></div>
  <div class="stat"><strong>{stats.get('sources', 0)}</strong><span>掃描來源</span></div>
</section>
<section class="grid">
  <div class="panel">
    <h2>週報存檔</h2>
    <ul class="report-list">{reports_html}</ul>
  </div>
  <aside class="panel">
    <h2>最新趨勢</h2>
    <p><span class="pill">{html.escape(latest_week)}</span></p>
    <ul>{trends_html}</ul>
  </aside>
</section>
"""
    (DOCS_DIR / "index.html").write_text(render_site_page("Academic CFP Tracker", body), encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    generated_at = now_in_timezone(config.get("tracking_policy", {}).get("report_timezone", "UTC"))
    iso_year, iso_week, _ = generated_at.isocalendar()
    week_id = args.week or f"{iso_year}-W{iso_week:02d}"

    REPORTS_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    DOCS_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.site_only:
        generate_site(week_id)
        return 0

    candidates, health = collect_candidates(config, offline=args.offline, limit_sources=args.limit_sources)
    report_markdown = render_report(candidates, health, config, generated_at, week_id, args.offline)
    report_path = REPORTS_DIR / f"{week_id}.md"
    report_path.write_text(report_markdown, encoding="utf-8")
    trends = summarize_trends(candidates)
    save_json_report(candidates, health, trends, generated_at, week_id, args.offline)
    update_readme(week_id, candidates, args.offline)
    generate_site(week_id)

    print(f"Wrote {report_path.relative_to(ROOT)}")
    print(f"Wrote {(DATA_DIR / f'{week_id}.json').relative_to(ROOT)}")
    print(f"Wrote {DOCS_DIR.relative_to(ROOT) / 'index.html'}")
    print(f"Candidates: {len(candidates)}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Q1 journal CFPs and generate weekly reports.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to target_journals.json")
    parser.add_argument("--offline", action="store_true", help="Skip network fetching and create a bootstrap report.")
    parser.add_argument("--site-only", action="store_true", help="Regenerate docs/ HTML from existing reports only.")
    parser.add_argument("--limit-sources", type=int, default=None, help="Limit direct sources for debugging.")
    parser.add_argument("--week", default="", help="Override ISO week id, e.g. 2026-W37.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
