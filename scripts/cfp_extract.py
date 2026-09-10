"""Strict validation for call-for-papers candidates.

The first generation of this tracker accepted any ``<a href>`` whose *ancestor*
text happened to mention "special issue", then ran a date parser over that same
shared blob.  Every navigation link on a Springer journal page therefore became
a "CFP" ("Log in", "Skip to main content", "Language editing"), and unrelated
rows all inherited the same fabricated deadline.

This module is the gate that replaces that guesswork.  It answers three
questions, and refuses to guess on any of them:

1. Does this title actually name a call for papers, or is it site furniture?
2. Is there a real deadline, stated next to a deadline cue inside this entry?
3. Is the call still accepting submissions?

Everything here is publisher independent; per-publisher locating rules live in
``scripts/cfp_sources.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

__all__ = [
    "DeadlineInfo",
    "SubmissionStatus",
    "looks_like_cfp_title",
    "is_navigation_chrome",
    "extract_deadline_near_cue",
    "detect_submission_status",
    "validate_candidate",
]


# ---------------------------------------------------------------------------
# Navigation chrome
# ---------------------------------------------------------------------------

# Link texts that are site furniture on publisher pages.  Matched against the
# whole (normalised) title, so a real CFP that merely *contains* one of these
# words is not discarded.
NAV_EXACT = {
    "log in",
    "login",
    "sign in",
    "sign up",
    "register",
    "subscribe",
    "skip to main content",
    "skip to content",
    "skip to navigation",
    "cookie settings",
    "cookies settings",
    "manage cookies",
    "privacy policy",
    "terms and conditions",
    "accessibility",
    "contact us",
    "about us",
    "home",
    "search",
    "menu",
    "help",
    "faq",
    "rss",
    "sitemap",
    "advertising",
    "reprints",
    "permissions",
    "language editing",
    "english language editing",
    "pre-submission checklist",
    "submission guidelines",
    "author guidelines",
    "instructions for authors",
    "how to publish with us",
    "submit your manuscript",
    "submit manuscript",
    "submit a manuscript",
    "submit article",
    "current issue",
    "latest issue",
    "all issues",
    "archive",
    "back issues",
    "featured articles",
    "popular articles",
    "most read",
    "editorial board",
    "aims and scope",
    "journal updates",
    "journal information",
    "explore",
    "read more",
    "learn more",
    "view all",
    "see all",
    "more",
    "next",
    "previous",
    "back",
    "close",
    "share",
    "print",
    "download pdf",
    "open access",
    "for authors",
    "for reviewers",
    "peer review",
    "ethics",
    "news",
    "events",
    "jobs",
    "careers",
}

# Substrings that mark a link as chrome or as an *archive* of past calls.
NAV_SUBSTRINGS = (
    "skip to ",
    "cookie",
    "sign up for alerts",
    "get notified when new articles",
    "sign up for article alerts",
    "language editing",
    "pre-submission",
    "submission guidelines",
    "how to publish with us",
    "submit your manuscript",
    "editorial board",
    "aims and scope",
    "explore jbhi",
    "on xplore",
    "featured articles",
    "popular articles",
    "most accessed",
    "top accessed",
    "advertisement",
)

# Anything naming a *past* or *closed* call must never reach the open list.
ARCHIVE_MARKERS = (
    "past special issue",
    "past special issues",
    "past call for special issues",
    "past calls",
    "previous special issue",
    "previous collections",
    "closed collections",
    "closed calls",
    "archived collection",
    "completed collection",
    "past collections",
)

# URL fragments that indicate chrome, auth walls, or archives rather than a call.
JUNK_URL_PATTERNS = (
    "/login",
    "/signin",
    "/sign-in",
    "/register",
    "idp.springer.com",
    "authorservices.springernature.com",
    "submission.nature.com",
    "submission.springernature.com",
    "editorialmanager.com",
    "/pre-submission",
    "/submission-guidelines",
    "/how-to-publish-with-us",
    "/author-guidelines",
    "/instructions",
    "/editorial-board",
    "/aims-and-scope",
    "journal-alerts.springernature.com",
    "/past-special-issues",
    "/past-call-for-special-issues",
    "/mostRecentIssue",
    "/topAccessedArticles",
    "#main",
    "#content",
    "/cookies",
    "/privacy",
    "/terms",
    "javascript:",
    "mailto:",
)

# Phrases that positively identify a call for papers / special issue.
CFP_TITLE_MARKERS = (
    "call for paper",
    "calls for paper",
    "call for submission",
    "call for abstract",
    "special issue",
    "special issues",
    "special collection",
    "special section",
    "theme issue",
    "themed issue",
    "thematic issue",
    "topical collection",
    "article collection",
    "guest edited collection",
    "guest-edited collection",
    "research topic",
    "focus issue",
    "supplement on",
    "徵稿",
    "專刊",
    "特刊",
)


def _norm(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def is_navigation_chrome(title: str, url: str = "") -> bool:
    """True when the link is site furniture, an auth wall, or an archive index."""
    clean = _norm(title).lower().rstrip(" .:>»›|")
    if not clean or len(clean) < 4:
        return True
    if clean in NAV_EXACT:
        return True
    if any(fragment in clean for fragment in NAV_SUBSTRINGS):
        return True
    if any(marker in clean for marker in ARCHIVE_MARKERS):
        return True

    lowered_url = (url or "").lower()
    if any(pattern.lower() in lowered_url for pattern in JUNK_URL_PATTERNS):
        return True

    # Pure punctuation, icon text, or a bare number is never a CFP title.
    if not re.search(r"[a-z一-鿿]", clean):
        return True
    return False


def looks_like_cfp_title(title: str, url: str = "") -> bool:
    """True when the *title itself* names a call for papers.

    The decisive change from the previous generation: the evidence must live in
    the link text (or its own URL slug), never in a shared ancestor blob.
    """
    if is_navigation_chrome(title, url):
        return False
    clean = _norm(title).lower()
    if any(marker in clean for marker in CFP_TITLE_MARKERS):
        return True

    slug = (url or "").lower()
    slug_markers = (
        "call-for-paper",
        "calls-for-paper",
        "call_for_paper",
        "callforpapers",
        "special-issue",
        "special_issues",
        "specialissue",
        "theme-issue",
        "themed-issue",
        "/collections/",
        "research-topic",
    )
    return any(marker in slug for marker in slug_markers)


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------

DEADLINE_CUES = (
    "submission deadline",
    "manuscript submission deadline",
    "abstract submission deadline",
    "deadline for manuscript submission",
    "deadline for submission",
    "deadline for submissions",
    "deadline",
    "closing date",
    "closes on",
    "closes",
    "submit by",
    "submissions close",
    "submission closes",
    "open until",
    "accepting submissions until",
    "due date",
    "final date",
    "last date",
    "截稿",
    "截止",
    "收件截止",
)

# How far after a cue we will look for a date.  Keeping this tight is what
# prevents a page-wide blob from donating an unrelated date to an entry.
CUE_WINDOW_CHARS = 160

MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))

DATE_PATTERNS = (
    # 30 November 2026 / 30th Nov, 2026
    re.compile(
        rf"\b(?P<day>[0-3]?\d)(?:st|nd|rd|th)?\s+(?P<month>{_MONTH_ALT})\.?,?\s+(?P<year>20\d{{2}})\b",
        re.IGNORECASE,
    ),
    # November 30, 2026 / Nov 30 2026
    re.compile(
        rf"\b(?P<month>{_MONTH_ALT})\.?\s+(?P<day>[0-3]?\d)(?:st|nd|rd|th)?,?\s+(?P<year>20\d{{2}})\b",
        re.IGNORECASE,
    ),
    # 2026-11-30 / 2026/11/30
    re.compile(r"\b(?P<year>20\d{2})[-/](?P<month_n>0?[1-9]|1[0-2])[-/](?P<day>0?[1-9]|[12]\d|3[01])\b"),
    # 30/11/2026 (day-first; ambiguous d/m vs m/d is resolved conservatively below)
    re.compile(r"\b(?P<day>0?[1-9]|[12]\d|3[01])/(?P<month_n>0?[1-9]|1[0-2])/(?P<year>20\d{2})\b"),
    # 2026年11月30日
    re.compile(r"(?P<year>20\d{2})\s*年\s*(?P<month_n>1[0-2]|0?[1-9])\s*月\s*(?P<day>3[01]|[12]\d|0?[1-9])\s*日"),
    # November 2026 (month precision only)
    re.compile(rf"\b(?P<month>{_MONTH_ALT})\.?\s+(?P<year>20\d{{2}})\b", re.IGNORECASE),
)


@dataclass
class DeadlineInfo:
    """A deadline we can point at a source sentence for."""

    date: date | None = None
    text: str = ""
    cue: str = ""
    evidence: str = ""
    precision: str = "none"  # "day" | "month" | "none"

    @property
    def found(self) -> bool:
        return self.date is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat() if self.date else None,
            "text": self.text,
            "cue": self.cue,
            "evidence": self.evidence,
            "precision": self.precision,
        }


def _build_date(
    match: re.Match[str], month_precision_to_end: bool = True
) -> tuple[date | None, str]:
    groups = match.groupdict()
    year = int(groups["year"])

    if groups.get("month_n"):
        month = int(groups["month_n"])
    elif groups.get("month"):
        month = MONTHS.get(groups["month"].lower().rstrip("."), 0)
    else:
        return None, "none"
    if not 1 <= month <= 12:
        return None, "none"

    day_raw = groups.get("day")
    if day_raw is None:
        # Month precision: treat "November 2026" as the last day of November,
        # the reading that is generous to the submitter without inventing a day.
        if not month_precision_to_end:
            return None, "none"
        if month == 12:
            last = date(year, 12, 31)
        else:
            last = date(year, month + 1, 1).toordinal() - 1
            last = date.fromordinal(last)
        return last, "month"

    try:
        return date(year, month, int(day_raw)), "day"
    except ValueError:
        return None, "none"


def extract_deadline_near_cue(
    text: str,
    today: date | None = None,
    window: int = CUE_WINDOW_CHARS,
) -> DeadlineInfo:
    """Find a deadline that is stated *next to* a deadline cue.

    Returns an empty :class:`DeadlineInfo` when no cue is present or no date
    sits within ``window`` characters of one.  Returning "unknown" is the
    correct answer far more often than guessing, so there is deliberately no
    fallback that scans the whole string.
    """
    today = today or datetime.now(timezone.utc).date()
    normalised = _norm(text)
    if not normalised:
        return DeadlineInfo()
    lowered = normalised.lower()

    best: DeadlineInfo | None = None

    for cue in DEADLINE_CUES:
        start = 0
        while True:
            index = lowered.find(cue, start)
            if index == -1:
                break
            start = index + len(cue)
            segment = normalised[index : index + len(cue) + window]

            for pattern in DATE_PATTERNS:
                match = pattern.search(segment)
                if not match:
                    continue
                parsed, precision = _build_date(match)
                if parsed is None:
                    continue
                # A "deadline" more than a year in the past is almost always a
                # copyright date or an archived call bleeding into the window.
                if (today - parsed).days > 365:
                    continue
                candidate = DeadlineInfo(
                    date=parsed,
                    text=match.group(0),
                    cue=cue,
                    evidence=_norm(segment)[:240],
                    precision=precision,
                )
                # Prefer the earliest future deadline: it is the binding one.
                if best is None:
                    best = candidate
                elif parsed >= today and (best.date is None or best.date < today or parsed < best.date):
                    best = candidate
                break
    return best or DeadlineInfo()


# ---------------------------------------------------------------------------
# Submission status
# ---------------------------------------------------------------------------

CLOSED_MARKERS = (
    "this collection is now closed",
    "collection is now closed",
    "now closed",
    "is closed",
    "closed for submissions",
    "submissions are closed",
    "submission is closed",
    "no longer accepting",
    "no longer open",
    "call has closed",
    "call is closed",
    "deadline has passed",
    "deadline passed",
    "this call is now closed",
    "已截止",
    "已結束",
)

OPEN_MARKERS = (
    "open for submissions",
    "now open for submissions",
    "currently open",
    "accepting submissions",
    "now accepting",
    "submissions open",
    "submission status: open",
    "open call",
    "still open",
    "徵稿中",
    "開放投稿",
)


@dataclass
class SubmissionStatus:
    """Whether a call is accepting submissions, and why we believe that."""

    state: str = "unknown"  # "open" | "closed" | "unknown"
    reason: str = ""
    evidence: str = ""
    days_left: int | None = None

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "evidence": self.evidence,
            "days_left": self.days_left,
        }


def detect_submission_status(
    text: str,
    deadline: DeadlineInfo | None = None,
    today: date | None = None,
    explicit_state: str | None = None,
) -> SubmissionStatus:
    """Decide open / closed / unknown.

    ``explicit_state`` lets a publisher adapter pass a status it read straight
    from the markup (for example a Nature "Submission status: Open" field),
    which outranks any inference we could make from prose.
    """
    today = today or datetime.now(timezone.utc).date()
    lowered = _norm(text).lower()
    days_left = (deadline.date - today).days if deadline and deadline.date else None

    if explicit_state == "closed":
        return SubmissionStatus(
            state="closed",
            reason="publisher markup states the call is closed",
            evidence="closed",
            days_left=days_left,
        )

    if explicit_state == "open":
        # A stated deadline that has passed outranks an "open" label. Listings
        # go stale - IEEE EMBS advertises eleven expired calls under a heading
        # that says they are accepting submissions.
        if days_left is not None and days_left < 0:
            return SubmissionStatus(
                state="closed",
                reason="marked open but the stated deadline has passed",
                evidence=deadline.evidence if deadline else "",
                days_left=days_left,
            )
        return SubmissionStatus(
            state="open",
            reason="publisher markup states the call is open",
            evidence="open",
            days_left=days_left,
        )

    for marker in CLOSED_MARKERS:
        if marker in lowered:
            return SubmissionStatus(state="closed", reason=f"page says '{marker}'", evidence=marker)

    # An expired deadline closes the call regardless of upbeat marketing copy.
    if deadline and deadline.date:
        days_left = (deadline.date - today).days
        if days_left < 0:
            return SubmissionStatus(
                state="closed",
                reason="stated deadline has passed",
                evidence=deadline.evidence,
                days_left=days_left,
            )
        return SubmissionStatus(
            state="open",
            reason="deadline is in the future",
            evidence=deadline.evidence,
            days_left=days_left,
        )

    for marker in OPEN_MARKERS:
        if marker in lowered:
            return SubmissionStatus(state="open", reason=f"page says '{marker}'", evidence=marker)

    return SubmissionStatus(state="unknown", reason="no deadline and no status wording found")


# ---------------------------------------------------------------------------
# Combined gate
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    accepted: bool
    reject_reason: str = ""
    deadline: DeadlineInfo = field(default_factory=DeadlineInfo)
    status: SubmissionStatus = field(default_factory=SubmissionStatus)


def validate_candidate(
    title: str,
    url: str,
    entry_text: str,
    today: date | None = None,
    explicit_state: str | None = None,
    horizon_days: int | None = None,
    context_confirmed: bool = False,
) -> ValidationResult:
    """Run the full gate over one candidate entry.

    ``entry_text`` must be the text of the entry itself, not the surrounding
    page.  Passing a page-wide blob here reintroduces exactly the bug this
    module exists to prevent.

    ``context_confirmed`` says the adapter already established that this entry
    came out of a call-for-papers listing.  Real special issues are usually
    titled by their subject alone ("Extracorporeal Blood Purification"), so
    demanding the words "special issue" in the title would discard most of the
    genuine ones.  Chrome filtering still applies either way.
    """
    today = today or datetime.now(timezone.utc).date()

    if is_navigation_chrome(title, url):
        return ValidationResult(False, "navigation chrome or archive link")
    if not context_confirmed and not looks_like_cfp_title(title, url):
        return ValidationResult(False, "title does not name a call for papers")

    deadline = extract_deadline_near_cue(entry_text, today=today)
    status = detect_submission_status(
        entry_text, deadline=deadline, today=today, explicit_state=explicit_state
    )

    if status.state == "closed":
        return ValidationResult(False, f"closed: {status.reason}", deadline, status)

    if horizon_days is not None and deadline.date is not None:
        if (deadline.date - today).days > horizon_days:
            return ValidationResult(
                False, f"deadline beyond {horizon_days}-day horizon", deadline, status
            )

    return ValidationResult(True, "", deadline, status)
