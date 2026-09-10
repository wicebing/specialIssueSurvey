"""Per-publisher adapters that locate real call-for-papers entries.

Each adapter knows the one page that lists a publisher's *open* calls and the
markup that wraps a single entry.  That containment is the point: an adapter
hands :mod:`scripts.cfp_extract` the text of one entry, never the whole page, so
a deadline can only ever be read from the entry it belongs to.

Adding a publisher means writing one function that yields ``(title, url,
entry_text, explicit_state)`` tuples.  Everything after that - validation,
topic tagging, scoring, deduplication - is shared.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse, urlunparse

from scripts.cfp_extract import DeadlineInfo, SubmissionStatus, validate_candidate
from scripts.http_client import PoliteSession

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None


__all__ = [
    "CFPRecord",
    "SourceReport",
    "RawEntry",
    "ADAPTERS",
    "collect_from_spec",
    "normalize_url",
]


# ---------------------------------------------------------------------------
# Shared records
# ---------------------------------------------------------------------------


@dataclass
class RawEntry:
    """One candidate as located by an adapter, before validation."""

    title: str
    url: str
    entry_text: str
    explicit_state: str | None = None
    journal: str = ""
    summary: str = ""
    context_confirmed: bool = False


@dataclass
class CFPRecord:
    """A validated call for papers."""

    journal: str
    title: str
    url: str
    publisher: str = ""
    source_id: str = ""
    source_label: str = ""
    summary: str = ""
    deadline: dict[str, Any] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)
    topics: list[dict[str, Any]] = field(default_factory=list)
    jcr_band: str = ""
    journal_priority: int = 1
    journal_category: str = ""
    score: int = 0
    fingerprint: str = ""
    is_new: bool = False
    tier: str = "other"

    @property
    def deadline_date(self) -> str | None:
        return self.deadline.get("date")

    @property
    def days_left(self) -> int | None:
        return self.status.get("days_left")

    def to_dict(self) -> dict[str, Any]:
        return {
            "journal": self.journal,
            "title": self.title,
            "url": self.url,
            "publisher": self.publisher,
            "source_id": self.source_id,
            "source_label": self.source_label,
            "summary": self.summary,
            "deadline": self.deadline,
            "status": self.status,
            "topics": self.topics,
            "jcr_band": self.jcr_band,
            "journal_priority": self.journal_priority,
            "journal_category": self.journal_category,
            "score": self.score,
            "fingerprint": self.fingerprint,
            "is_new": self.is_new,
            "tier": self.tier,
        }


@dataclass
class SourceReport:
    """Honest per-source accounting for the report's health table."""

    source_id: str
    label: str
    url: str
    status: str = "ok"
    http_status: int | None = None
    transport: str = ""
    found: int = 0
    accepted: int = 0
    notes: str = ""
    reject_reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "label": self.label,
            "url": self.url,
            "status": self.status,
            "http_status": self.http_status,
            "transport": self.transport,
            "found": self.found,
            "accepted": self.accepted,
            "notes": self.notes,
            "reject_reasons": self.reject_reasons,
        }


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    kept = {
        key: values
        for key, values in query.items()
        if not key.lower().startswith(("utm_", "fbclid", "gclid"))
    }
    query_string = "&".join(
        f"{quote_plus(key)}={quote_plus(value)}"
        for key, values in sorted(kept.items())
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


def fingerprint_for(journal: str, title: str, url: str) -> str:
    stable = "|".join([normalize_url(url), clean_text(journal).lower(), clean_text(title).lower()])
    return hashlib.sha1(stable.encode("utf-8")).hexdigest()[:16]


def _soup(html_text: str):
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 is required. Run pip install -r requirements.txt.")
    return BeautifulSoup(html_text, "lxml")


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def springer_collections(
    session: PoliteSession, spec: dict[str, Any]
) -> tuple[list[RawEntry], SourceReport]:
    """Springer Nature / BMC ``/collections?filter=Open`` listings.

    Springer fronts these pages with a TLS-fingerprinting bot wall that answers
    200 with a "Client Challenge" stub, which is why the session's curl
    fallback matters here more than anywhere else.
    """
    url = spec["url"]
    report = SourceReport(spec["id"], spec.get("label", spec["id"]), url)
    result = session.get(url)
    report.http_status = result.status_code
    report.transport = result.transport
    if not result.ok:
        report.status = result.status
        report.notes = result.error
        return [], report

    soup = _soup(result.text)
    cards = soup.select("article.app-card-collection")
    entries: list[RawEntry] = []
    for card in cards:
        link = card.select_one("a.app-card-collection__heading-link") or card.select_one("a[href]")
        if not link:
            continue
        title = clean_text(link.get_text(" ", strip=True))
        href = urljoin(result.final_url or url, link.get("href", ""))
        text = clean_text(card.get_text(" ", strip=True))
        summary = clean_text(
            (card.select_one("div.app-card-collection__text") or card).get_text(" ", strip=True)
        )
        entries.append(
            RawEntry(
                title=title,
                url=href,
                entry_text=text,
                journal=spec.get("journal", ""),
                summary=summary[:400],
                context_confirmed=True,
            )
        )
    report.found = len(entries)
    if not entries:
        report.status = "no_entries"
        report.notes = "page fetched but no collection cards matched"
    return entries, report


def nature_calls_for_papers(
    session: PoliteSession, spec: dict[str, Any]
) -> tuple[list[RawEntry], SourceReport]:
    """Nature portfolio ``/calls-for-papers`` feeds.

    This endpoint is open-only by construction, and each card states
    "Submission status: Open" plus a deadline, so status comes from the
    publisher rather than from our inference.
    """
    url = spec["url"]
    report = SourceReport(spec["id"], spec.get("label", spec["id"]), url)
    entries: list[RawEntry] = []
    max_pages = int(spec.get("max_pages", 2))

    for page in range(1, max_pages + 1):
        page_url = url if page == 1 else f"{url}{'&' if '?' in url else '?'}page={page}"
        result = session.get(page_url)
        if page == 1:
            report.http_status = result.status_code
            report.transport = result.transport
            if not result.ok:
                report.status = result.status
                report.notes = result.error
                return [], report
        if not result.ok:
            break

        soup = _soup(result.text)
        cards = soup.select("article.c-card")
        if not cards:
            break
        for card in cards:
            link = card.select_one("a[href]")
            if not link:
                continue
            title = clean_text(link.get_text(" ", strip=True))
            href = urljoin(result.final_url or page_url, link.get("href", ""))
            text = clean_text(card.get_text(" ", strip=True))
            summary = clean_text(
                (card.select_one("div.c-card__summary") or card).get_text(" ", strip=True)
            )
            lowered = text.lower()
            explicit = None
            if "submission status:" in lowered:
                tail = lowered.split("submission status:", 1)[1][:40]
                if "open" in tail:
                    explicit = "open"
                elif "closed" in tail:
                    explicit = "closed"
            entries.append(
                RawEntry(
                    title=title,
                    url=href,
                    entry_text=text,
                    explicit_state=explicit,
                    journal=spec.get("journal", ""),
                    summary=summary[:400],
                    context_confirmed=True,
                )
            )
    report.found = len(entries)
    if not entries:
        report.status = "no_entries"
        report.notes = "page fetched but no call cards matched"
    return entries, report


def embs_special_issues(
    session: PoliteSession, spec: dict[str, Any]
) -> tuple[list[RawEntry], SourceReport]:
    """IEEE EMBS "Special Issues Now Accepting Submissions" listings.

    EMBS never prunes this page: roughly half of the entries it advertises as
    accepting submissions have deadlines that already passed.  Each entry does
    carry its own "Submission Deadline: ..." string, so the shared gate can
    retire the stale ones on the deadline rather than trusting the heading.
    """
    url = spec["url"]
    report = SourceReport(spec["id"], spec.get("label", spec["id"]), url)
    result = session.get(url)
    report.http_status = result.status_code
    report.transport = result.transport
    if not result.ok:
        report.status = result.status
        report.notes = result.error
        return [], report

    soup = _soup(result.text)
    entries: list[RawEntry] = []
    seen: set[str] = set()
    for node in soup.find_all(["p", "li", "h3", "h4", "div"]):
        text = clean_text(node.get_text(" ", strip=True))
        if "submission deadline" not in text.lower():
            continue
        # Skip wrappers: keep the tightest node that still states a deadline.
        if len(text) > 400:
            continue
        link = node.find("a", href=True)
        if not link:
            continue
        href = urljoin(result.final_url or url, link["href"])
        if href in seen:
            continue
        seen.add(href)
        # The visible title is the entry text minus its deadline clause.
        title = clean_text(re.split(r"submission deadline", text, flags=re.IGNORECASE)[0])
        if not title:
            title = clean_text(link.get_text(" ", strip=True))
        entries.append(
            RawEntry(
                title=title,
                url=href,
                entry_text=text,
                journal=spec.get("journal", ""),
                summary=title[:400],
                context_confirmed=True,
            )
        )
    report.found = len(entries)
    if not entries:
        report.status = "no_entries"
        report.notes = "page fetched but no 'Submission Deadline' entries matched"
    return entries, report


def jmir_announcements(
    session: PoliteSession, spec: dict[str, Any]
) -> tuple[list[RawEntry], SourceReport]:
    """JMIR announcement feeds.

    The listing carries titles only, so a call's deadline lives one click away.
    We follow only the announcements whose title already reads as a call, which
    keeps the extra requests proportionate.
    """
    url = spec["url"]
    report = SourceReport(spec["id"], spec.get("label", spec["id"]), url)
    result = session.get(url)
    report.http_status = result.status_code
    report.transport = result.transport
    if not result.ok:
        report.status = result.status
        report.notes = result.error
        return [], report

    soup = _soup(result.text)
    cards = soup.select("div.full-width-card") or soup.select("article")
    max_detail = int(spec.get("max_detail_fetches", 8))
    entries: list[RawEntry] = []
    detail_fetches = 0

    for card in cards:
        heading = card.select_one("h2.full-width-card-info-title") or card.find(["h2", "h3"])
        link = card.find("a", href=True)
        if not heading or not link:
            continue
        title = clean_text(heading.get_text(" ", strip=True))
        lowered = title.lower()
        if not any(
            marker in lowered
            for marker in ("call for papers", "theme issue", "special issue", "call for")
        ):
            continue
        href = urljoin(result.final_url or url, link["href"])
        text = clean_text(card.get_text(" ", strip=True))

        # The card rarely states a deadline; the announcement page does.
        if "deadline" not in text.lower() and detail_fetches < max_detail:
            detail = session.get(href)
            detail_fetches += 1
            if detail.ok:
                body = _soup(detail.text)
                for tag in body(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                main = body.select_one("main") or body.select_one("article") or body
                text = clean_text(main.get_text(" ", strip=True))[:4000]
        entries.append(
            RawEntry(
                title=title,
                url=href,
                entry_text=text,
                journal=spec.get("journal", ""),
                summary=text[:400],
                context_confirmed=True,
            )
        )
    report.found = len(entries)
    if not entries:
        report.status = "no_entries"
        report.notes = "no announcement titled as a call for papers"
    return entries, report


def jmir_api(
    session: PoliteSession, spec: dict[str, Any]
) -> tuple[list[RawEntry], SourceReport]:
    """JMIR's JSON announcement API.

    JMIR ships an undocumented but stable JSON feed at ``/v1/announcements``.
    Reading it instead of the rendered page removes a whole class of scraping
    failure, and it also fixes the site's broken HTML pagination, where
    ``?page=2`` silently re-serves page 1.

    Announcement bodies state a deadline only sometimes; JMIR runs many theme
    issues on rolling submission. Those come through as open-without-a-date
    rather than being dropped or given an invented deadline.
    """
    base = spec["url"].rstrip("/")
    report = SourceReport(spec["id"], spec.get("label", spec["id"]), base)

    listing = session.get(f"{base}/v1/announcements", params={"page": 1, "perPage": 200})
    report.http_status = listing.status_code
    report.transport = listing.transport
    if not listing.ok:
        report.status = listing.status
        report.notes = listing.error
        return [], report

    try:
        payload = listing.json()
    except Exception as exc:
        report.status = "parse_error"
        report.notes = str(exc)
        return [], report
    items = payload if isinstance(payload, list) else (
        payload.get("results") or payload.get("data") or payload.get("items") or []
    )

    # Map numeric journal ids onto journal names so records are attributable.
    journal_names: dict[Any, str] = {}
    journals = session.get(f"{base}/v1/journals")
    if journals.ok:
        try:
            raw = journals.json()
            rows = raw if isinstance(raw, list) else (raw.get("results") or raw.get("data") or [])
            for row in rows:
                journal_names[str(row.get("journal_id"))] = clean_text(
                    row.get("title") or row.get("name") or ""
                )
        except Exception:
            journal_names = {}

    watchlist = {name.lower() for name in spec.get("journal_allowlist", [])}
    max_detail = int(spec.get("max_detail_fetches", 25))
    entries: list[RawEntry] = []
    detail_fetches = 0

    for item in items:
        title = clean_text(item.get("title", ""))
        lowered = title.lower()
        if not any(
            marker in lowered
            for marker in ("call for papers", "theme issue", "special issue", "call for abstracts")
        ):
            continue

        journal = journal_names.get(str(item.get("journal_id")), "")
        if watchlist and journal and journal.lower() not in watchlist:
            continue

        announcement_id = item.get("announcement_id")
        url = f"{base}/announcements/{announcement_id}"
        text = clean_text(item.get("description_short", "")) or title

        if detail_fetches < max_detail:
            detail = session.get(f"{base}/v1/announcements/{announcement_id}")
            detail_fetches += 1
            if detail.ok:
                try:
                    body = detail.json()
                    description = body.get("description", "")
                    if description:
                        text = clean_text(_soup(description).get_text(" ", strip=True))[:6000]
                except Exception:
                    pass

        entries.append(
            RawEntry(
                title=title,
                url=url,
                entry_text=text,
                journal=journal or spec.get("journal", ""),
                summary=text[:400],
                context_confirmed=True,
            )
        )

    report.found = len(entries)
    if not entries:
        report.status = "no_entries"
        report.notes = "no announcement titled as a call for papers"
    return entries, report


def generic_cfp_page(
    session: PoliteSession, spec: dict[str, Any]
) -> tuple[list[RawEntry], SourceReport]:
    """Fallback for publishers without a bespoke adapter.

    Deliberately strict: an anchor qualifies only when its *own* text names a
    call for papers, and the entry text is the anchor's nearest small container
    rather than a sprawling ancestor.  This is the direct replacement for the
    behaviour that produced "Log in" rows.
    """
    url = spec["url"]
    report = SourceReport(spec["id"], spec.get("label", spec["id"]), url)
    result = session.get(url)
    report.http_status = result.status_code
    report.transport = result.transport
    if not result.ok:
        report.status = result.status
        report.notes = result.error
        return [], report

    soup = _soup(result.text)
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    entries: list[RawEntry] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        title = clean_text(link.get_text(" ", strip=True))
        if len(title) < 12:
            continue
        lowered = title.lower()
        if not any(
            marker in lowered
            for marker in (
                "call for paper",
                "special issue",
                "special collection",
                "theme issue",
                "themed issue",
                "topical collection",
                "article collection",
                "research topic",
            )
        ):
            continue
        href = urljoin(result.final_url or url, link["href"])
        key = normalize_url(href)
        if key in seen:
            continue
        seen.add(key)

        # Climb only until we have a container with real prose, and stop well
        # before the page shell.
        container = link
        for _ in range(3):
            if container.parent is None:
                break
            container = container.parent
            text = clean_text(container.get_text(" ", strip=True))
            if len(text) > 120:
                break
        entry_text = clean_text(container.get_text(" ", strip=True))[:1200]
        entries.append(
            RawEntry(
                title=title,
                url=href,
                entry_text=entry_text,
                journal=spec.get("journal", ""),
                summary=entry_text[:400],
            )
        )
    report.found = len(entries)
    if not entries:
        report.status = "no_entries"
        report.notes = "no anchor whose own text names a call for papers"
    return entries, report


ADAPTERS: dict[str, Callable[[PoliteSession, dict[str, Any]], tuple[list[RawEntry], SourceReport]]] = {
    "springer_collections": springer_collections,
    "nature_calls": nature_calls_for_papers,
    "embs_special_issues": embs_special_issues,
    "jmir_announcements": jmir_announcements,
    "jmir_api": jmir_api,
    "generic": generic_cfp_page,
}


# ---------------------------------------------------------------------------
# Validation + scoring
# ---------------------------------------------------------------------------


_KEYWORD_PATTERNS: dict[str, Any] = {}


def _keyword_matches(keyword: str, haystack: str) -> bool:
    """Match a topic keyword without matching inside a longer word.

    A plain substring test tagged "Effects of Psychedelics on the **Brain**" as
    emergency medicine, because "AI" sits inside "brain" and "EMS" inside
    "systems".  ASCII keywords therefore need word boundaries; CJK keywords do
    not, since Chinese is written without spaces.
    """
    pattern = _KEYWORD_PATTERNS.get(keyword)
    if pattern is None:
        if keyword.isascii():
            pattern = re.compile(rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])")
        else:
            pattern = re.compile(re.escape(keyword.lower()))
        _KEYWORD_PATTERNS[keyword] = pattern
    return bool(pattern.search(haystack))


def classify_topics(text: str, taxonomy: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    haystack = text.lower()
    matched = []
    for topic in taxonomy:
        hits = [kw for kw in topic.get("keywords", []) if _keyword_matches(kw, haystack)]
        if hits:
            matched.append(
                {"id": topic["id"], "label": topic["label"], "hits": sorted(set(hits), key=str.lower)}
            )
    return matched



def resolve_tier(journal: str, spec: dict[str, Any]) -> str:
    """Split core top-20% journals from everything else.

    Multi-journal sources need this most: JMIR's API serves one feed for the
    whole family, and only a handful of those titles are genuinely Q1. Listing
    a JMIR Serious Games call beside a Critical Care one would misrepresent both.
    """
    core_titles = {name.lower() for name in spec.get("core_journals", [])}
    if core_titles:
        return "core" if journal.lower() in core_titles else "other"
    return "core" if "watchlist" in spec.get("jcr_band", "") else "other"


def score_record(
    deadline: DeadlineInfo,
    status: SubmissionStatus,
    topics: Sequence[dict[str, Any]],
    journal_priority: int,
    jcr_band: str,
    trend_terms: Sequence[str] = (),
    text: str = "",
) -> int:
    """Rank by how useful this call is to act on this week.

    A confirmed deadline outranks a vague "ongoing" call, a comfortable runway
    outranks one closing in days, and alignment with a rising topic breaks ties,
    because that is where a fast submission has the best odds.
    """
    score = 0
    if "watchlist" in jcr_band:
        score += 30
    elif "candidate" in jcr_band:
        score += 12
    score += min(20, max(0, journal_priority) * 4)
    score += min(15, len(topics) * 5)

    if status.state == "open" and deadline.date:
        score += 20
        days = status.days_left if status.days_left is not None else 0
        if 30 <= days <= 240:
            score += 15  # enough runway to actually write the paper
        elif 14 <= days < 30:
            score += 6
        elif days < 14:
            score -= 5
    elif status.state == "open":
        score += 8  # open but undated

    if trend_terms and text:
        lowered = text.lower()
        if any(term.lower() in lowered for term in trend_terms):
            score += 12
    return max(0, min(100, score))


def collect_from_spec(
    session: PoliteSession,
    spec: dict[str, Any],
    config: dict[str, Any],
    today: date,
    trend_terms: Sequence[str] = (),
) -> tuple[list[CFPRecord], SourceReport]:
    """Run one source spec end to end: locate, validate, score."""
    adapter = ADAPTERS.get(spec.get("adapter", "generic"), generic_cfp_page)
    try:
        entries, report = adapter(session, spec)
    except Exception as exc:
        report = SourceReport(spec["id"], spec.get("label", spec["id"]), spec.get("url", ""))
        report.status = "error"
        report.notes = f"{type(exc).__name__}: {exc}"
        return [], report

    policy = config.get("tracking_policy", {})
    horizon = int(policy.get("deadline_horizon_days", 400))
    taxonomy = config.get("topic_taxonomy", [])
    records: list[CFPRecord] = []

    for entry in entries:
        verdict = validate_candidate(
            entry.title,
            entry.url,
            entry.entry_text,
            today=today,
            explicit_state=entry.explicit_state,
            horizon_days=horizon,
            context_confirmed=entry.context_confirmed,
        )
        if not verdict.accepted:
            key = verdict.reject_reason.split(":")[0][:60] or "rejected"
            report.reject_reasons[key] = report.reject_reasons.get(key, 0) + 1
            continue

        journal = entry.journal or spec.get("journal", "")
        text_for_topics = f"{entry.title} {entry.summary}"
        topics = classify_topics(text_for_topics, taxonomy)

        # General-medicine journals publish across every specialty, so their
        # call lists are mostly irrelevant here. Requiring a topic hit keeps
        # oncology and dermatology collections out of an emergency-medicine
        # report without excluding the journal itself.
        if spec.get("require_topic_match") and not topics:
            report.reject_reasons["off-topic for this tracker"] = (
                report.reject_reasons.get("off-topic for this tracker", 0) + 1
            )
            continue
        records.append(
            CFPRecord(
                journal=journal,
                title=entry.title,
                url=entry.url,
                publisher=spec.get("publisher", ""),
                source_id=spec["id"],
                source_label=spec.get("label", spec["id"]),
                summary=entry.summary,
                deadline=verdict.deadline.to_dict(),
                status=verdict.status.to_dict(),
                topics=topics,
                jcr_band=spec.get("jcr_band", "unverified_candidate"),
                journal_priority=int(spec.get("priority", 1)),
                journal_category=spec.get("category", ""),
                score=score_record(
                    verdict.deadline,
                    verdict.status,
                    topics,
                    int(spec.get("priority", 1)),
                    spec.get("jcr_band", ""),
                    trend_terms,
                    text_for_topics,
                ),
                fingerprint=fingerprint_for(journal, entry.title, entry.url),
                tier=resolve_tier(journal, spec),
            )
        )
    report.accepted = len(records)
    return records, report


def dedupe(records: Iterable[CFPRecord]) -> list[CFPRecord]:
    best: dict[str, CFPRecord] = {}
    for record in records:
        current = best.get(record.fingerprint)
        if current is None or record.score > current.score:
            best[record.fingerprint] = record
    return sorted(
        best.values(),
        key=lambda item: (-item.score, item.deadline_date or "9999-12-31", item.journal),
    )
