"""Conference submission-deadline tracking.

The journal side of this tracker answers "where can I submit a paper?". This
module answers the other half: "what conference deadline is coming, where is it
held, and when?"

Two kinds of source, because no single one covers the ground:

*Dataset sources* read a community-maintained YAML feed. ``ccf-deadlines``
carries the computer-science venues (NeurIPS, ICML, CHI, ...) with deadline,
city and dates already structured, so nothing has to be scraped for those.

*Curated sources* cover the clinical societies, which that dataset omits
entirely. SAEM, SCCM, AMIA, MICCAI and the rest publish their deadlines on
sites that change layout every year, so their facts live in config, each with
the URL a human can check and the month the meeting normally closes.

The honesty rules from the journal side carry over unchanged: an unverified
deadline is reported as unknown, a passed deadline is retired, and a conference
whose next edition has not been announced says exactly that rather than
implying there is nothing to plan for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from scripts.http_client import PoliteSession

__all__ = [
    "Conference",
    "ConferenceSourceReport",
    "parse_simple_yaml",
    "load_ccf_conference",
    "collect_conferences",
    "city_of",
]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class Conference:
    """One conference edition, as it will appear in the report."""

    name: str
    acronym: str = ""
    edition: str = ""
    url: str = ""
    submission_deadline: date | None = None
    abstract_deadline: date | None = None
    deadline_text: str = ""
    city: str = ""
    venue: str = ""
    dates: str = ""
    start_date: date | None = None
    field: str = ""
    rank: str = ""
    relevance: str = ""
    source_id: str = ""
    status: str = "unknown"  # open | not_announced | passed | unknown
    typical_month: str = ""
    note: str = ""
    tier: str = "other"

    @property
    def next_deadline(self) -> date | None:
        """The date the user actually has to hit."""
        candidates = [d for d in (self.abstract_deadline, self.submission_deadline) if d]
        return min(candidates) if candidates else None

    def days_left(self, today: date) -> int | None:
        deadline = self.next_deadline
        return (deadline - today).days if deadline else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "acronym": self.acronym,
            "edition": self.edition,
            "url": self.url,
            "submission_deadline": self.submission_deadline.isoformat()
            if self.submission_deadline
            else None,
            "abstract_deadline": self.abstract_deadline.isoformat()
            if self.abstract_deadline
            else None,
            "deadline_text": self.deadline_text,
            "city": self.city,
            "venue": self.venue,
            "dates": self.dates,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "field": self.field,
            "rank": self.rank,
            "relevance": self.relevance,
            "source_id": self.source_id,
            "status": self.status,
            "typical_month": self.typical_month,
            "note": self.note,
            "tier": self.tier,
        }


@dataclass
class ConferenceSourceReport:
    source_id: str
    label: str
    url: str
    status: str = "ok"
    found: int = 0
    accepted: int = 0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "label": self.label,
            "url": self.url,
            "status": self.status,
            "found": self.found,
            "accepted": self.accepted,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# A very small YAML reader
# ---------------------------------------------------------------------------

# ccf-deadlines files use a narrow slice of YAML: nested maps, lists of maps,
# and scalars that are sometimes quoted. Parsing that slice directly keeps the
# project's dependency list short, and the shape is stable enough to rely on.


def _coerce(value: str) -> Any:
    text = value.strip()
    if not text:
        return ""
    if text[0] in "\"'" and text[-1] == text[0] and len(text) > 1:
        return text[1:-1]
    if text in {"true", "True"}:
        return True
    if text in {"false", "False"}:
        return False
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def parse_simple_yaml(text: str) -> Any:
    """Parse the subset of YAML used by the conference feeds."""
    root: list[Any] = []
    # Each stack frame is (indent, container). A container is a list or a dict.
    stack: list[tuple[int, Any]] = [(-1, root)]

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.lstrip()
        indent = len(raw) - len(stripped)

        while len(stack) > 1 and indent <= stack[-1][0] and not (
            stripped.startswith("- ") and indent == stack[-1][0] and isinstance(stack[-1][1], list)
        ):
            stack.pop()

        parent = stack[-1][1]

        if stripped.startswith("- "):
            body = stripped[2:]
            target = parent
            if not isinstance(target, list):
                continue
            if ":" in body and not body.startswith(("http", "'", '"')):
                item: dict[str, Any] = {}
                target.append(item)
                stack.append((indent, target))
                stack.append((indent + 2, item))
                key, _, rest = body.partition(":")
                if rest.strip():
                    item[key.strip()] = _coerce(rest)
                else:
                    child: dict[str, Any] = {}
                    item[key.strip()] = child
                    stack.append((indent + 2, child))
            else:
                target.append(_coerce(body))
            continue

        if ":" in stripped:
            key, _, rest = stripped.partition(":")
            key = key.strip()
            if not isinstance(parent, dict):
                continue
            if rest.strip():
                parent[key] = _coerce(rest)
            else:
                # A blank value opens either a nested map or a list; decide when
                # the first child line arrives.
                child_container: dict[str, Any] = {}
                parent[key] = child_container
                stack.append((indent, _PendingContainer(parent, key, child_container)))
    return root


class _PendingContainer:
    """Placeholder that turns into a list the first time a '- ' child appears."""

    def __init__(self, parent: dict[str, Any], key: str, mapping: dict[str, Any]):
        self.parent = parent
        self.key = key
        self.mapping = mapping

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Pending({self.key})"


def parse_conference_yaml(text: str) -> list[dict[str, Any]]:
    """Parse a ccf-deadlines conference file into plain dicts.

    Written as an explicit line walker rather than via the generic parser above:
    the file shape is fixed (a list of conferences, each with a ``confs`` list
    of editions, each with a ``timeline`` list), and walking it directly is far
    easier to reason about than a general YAML implementation.
    """
    conferences: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_conf: dict[str, Any] | None = None
    current_timeline: dict[str, Any] | None = None
    section = ""

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.lstrip()
        indent = len(raw) - len(stripped)

        if indent == 0 and stripped.startswith("- "):
            current = {"confs": [], "rank": {}}
            conferences.append(current)
            key, _, rest = stripped[2:].partition(":")
            current[key.strip()] = _coerce(rest)
            section = ""
            current_conf = None
            current_timeline = None
            continue
        if current is None:
            continue

        if indent == 2 and not stripped.startswith("- "):
            key, _, rest = stripped.partition(":")
            key = key.strip()
            if key in {"rank", "confs"}:
                section = key
            else:
                section = ""
                current[key] = _coerce(rest)
            continue

        if section == "rank" and indent >= 4:
            key, _, rest = stripped.partition(":")
            current["rank"][key.strip()] = _coerce(rest)
            continue

        if section == "confs":
            if stripped.startswith("- ") and indent == 4:
                current_conf = {"timeline": []}
                current["confs"].append(current_conf)
                current_timeline = None
                key, _, rest = stripped[2:].partition(":")
                current_conf[key.strip()] = _coerce(rest)
                continue
            if current_conf is None:
                continue
            if stripped.startswith("- ") and indent >= 8:
                current_timeline = {}
                current_conf["timeline"].append(current_timeline)
                key, _, rest = stripped[2:].partition(":")
                current_timeline[key.strip()] = _coerce(rest)
                continue
            key, _, rest = stripped.partition(":")
            key = key.strip()
            if key == "timeline":
                continue
            if indent >= 10 and current_timeline is not None:
                current_timeline[key] = _coerce(rest)
            elif indent >= 6:
                current_conf[key] = _coerce(rest)
    return conferences


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

# ccf-deadlines writes place as anything from "Seoul, Korea" to
# "Messe Wien Exhibition Congress Center, Vienna, Austria".
VENUE_WORDS = (
    "center", "centre", "convention", "conference", "exhibition", "congress",
    "hotel", "university", "college", "hall", "campus", "palace", "arena",
    "resort", "institute", "messe", "expo", "forum", "pavilion",
)


def city_of(place: str) -> tuple[str, str]:
    """Split a ``place`` string into (city_and_country, venue).

    Returns the trailing city/country pair, which is what a reader planning
    travel actually wants, and hands back the venue prefix separately rather
    than discarding it.
    """
    text = (place or "").strip()
    if not text:
        return "", ""
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) <= 2:
        return ", ".join(parts), ""

    # Drop leading segments that name a building rather than a place.
    venue_parts: list[str] = []
    while len(parts) > 2 and any(word in parts[0].lower() for word in VENUE_WORDS):
        venue_parts.append(parts.pop(0))
    if len(parts) > 3:
        venue_parts.extend(parts[: len(parts) - 3])
        parts = parts[-3:]
    return ", ".join(parts), ", ".join(venue_parts)


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Dataset source
# ---------------------------------------------------------------------------

CCF_RAW = "https://raw.githubusercontent.com/ccfddl/ccf-deadlines/main/conference"


def load_ccf_conference(
    session: PoliteSession,
    category: str,
    slug: str,
    spec: dict[str, Any],
    today: date,
) -> list[Conference]:
    """Read one ccf-deadlines file and return its future editions."""
    url = f"{CCF_RAW}/{category}/{slug}.yml"
    result = session.get(url)
    if not result.ok:
        return []

    MONTHS = (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )

    out: list[Conference] = []
    for entry in parse_conference_yaml(result.text):
        title = str(entry.get("title", slug.upper()))
        ranks = entry.get("rank") or {}
        rank_label = " / ".join(
            f"{key.upper()} {value}" for key, value in ranks.items() if value
        )

        def build(edition: dict[str, Any], submission, abstract, status: str) -> Conference:
            city, venue = city_of(str(edition.get("place", "")))
            return Conference(
                name=title,
                acronym=title,
                edition=str(edition.get("year", "")),
                url=str(edition.get("link", "")),
                submission_deadline=submission,
                abstract_deadline=abstract,
                city=city,
                venue=venue,
                dates=str(edition.get("date", "")),
                field=str(entry.get("sub", "")),
                rank=rank_label,
                relevance=spec.get("relevance", ""),
                source_id=spec.get("id", "ccf"),
                status=status,
                tier=spec.get("tier", "core"),
                note=str(entry.get("description", "")),
            )

        upcoming: list[Conference] = []
        newest_past: tuple[date, dict[str, Any]] | None = None

        for edition in entry.get("confs", []):
            timeline = edition.get("timeline") or [{}]
            submission = max(
                [d for d in (_parse_date(s.get("deadline")) for s in timeline) if d], default=None
            )
            abstract = max(
                [d for d in (_parse_date(s.get("abstract_deadline")) for s in timeline) if d],
                default=None,
            )
            latest = max([d for d in (submission, abstract) if d], default=None)
            if latest is None:
                continue
            if latest >= today:
                upcoming.append(build(edition, submission, abstract, "open"))
            elif newest_past is None or latest > newest_past[0]:
                newest_past = (latest, edition)

        if upcoming:
            out.extend(upcoming)
        elif newest_past is not None:
            # No next edition published yet. Reporting the venue as absent would
            # imply there is nothing to plan for, when in fact the deadline is
            # simply not out. Carry the last edition through as guidance.
            last_deadline, edition = newest_past
            record = build(edition, None, None, "not_announced")
            record.edition = f"{edition.get('year', '')} (最近一屆)"
            record.typical_month = MONTHS[last_deadline.month - 1]
            record.deadline_text = (
                f"下一屆尚未公布；上一屆截稿 {last_deadline.isoformat()}"
            )
            record.city = ""
            record.dates = ""
            out.append(record)
    return out


def collect_ccf(
    session: PoliteSession, spec: dict[str, Any], today: date
) -> tuple[list[Conference], ConferenceSourceReport]:
    report = ConferenceSourceReport(spec["id"], spec.get("label", spec["id"]), CCF_RAW)
    found: list[Conference] = []
    misses: list[str] = []
    for item in spec.get("conferences", []):
        try:
            editions = load_ccf_conference(
                session, item["category"], item["slug"], {**spec, **item}, today
            )
        except Exception as exc:  # a malformed file must not sink the run
            misses.append(f"{item.get('slug')}: {type(exc).__name__}")
            continue
        if not editions:
            misses.append(item["slug"])
        # Only the soonest upcoming edition is actionable.
        editions.sort(key=lambda c: c.next_deadline or date.max)
        found.extend(editions[:1])

    report.found = len(spec.get("conferences", []))
    # "accepted" means a deadline a reader can act on, so entries carried
    # through only as guidance must not inflate it.
    report.accepted = sum(1 for c in found if c.status == "open")
    pending = len(found) - report.accepted
    notes = []
    if pending:
        notes.append(f"{pending} awaiting next-edition announcement")
    if misses:
        notes.append(f"not in feed: {', '.join(sorted(misses))}")
    report.notes = "; ".join(notes)
    if not found:
        report.status = "no_entries"
    return found, report


# ---------------------------------------------------------------------------
# Curated source
# ---------------------------------------------------------------------------


def collect_curated(
    session: PoliteSession, spec: dict[str, Any], today: date
) -> tuple[list[Conference], ConferenceSourceReport]:
    """Read hand-verified society conferences from config.

    These are recorded rather than scraped because the societies publish their
    deadlines on pages that are rebuilt every year. Each entry carries the URL a
    human should open, and any entry whose deadline has passed is reported as
    awaiting its next announcement instead of being dropped, so the reader still
    learns that the meeting exists and roughly when it closes.
    """
    report = ConferenceSourceReport(spec["id"], spec.get("label", spec["id"]), spec.get("url", ""))
    out: list[Conference] = []
    for item in spec.get("conferences", []):
        submission = _parse_date(item.get("submission_deadline"))
        abstract = _parse_date(item.get("abstract_deadline"))
        latest = max([d for d in (submission, abstract) if d], default=None)

        status = item.get("status", "unknown")
        if latest and latest < today:
            status = "not_announced"
            submission = abstract = None
        elif latest:
            status = "open"

        out.append(
            Conference(
                name=item["name"],
                acronym=item.get("acronym", ""),
                edition=str(item.get("edition", "")),
                url=item.get("url", ""),
                submission_deadline=submission,
                abstract_deadline=abstract,
                deadline_text=item.get("deadline_text", ""),
                city=item.get("city", ""),
                dates=item.get("dates", ""),
                start_date=_parse_date(item.get("start_date")),
                field=item.get("field", ""),
                relevance=item.get("relevance", ""),
                source_id=spec["id"],
                status=status,
                typical_month=item.get("typical_month", ""),
                note=item.get("note", ""),
                tier=item.get("tier", spec.get("tier", "core")),
            )
        )
    report.found = len(out)
    report.accepted = sum(1 for c in out if c.status == "open")
    unannounced = sum(1 for c in out if c.status == "not_announced")
    if unannounced:
        report.notes = f"{unannounced} awaiting next-edition announcement"
    return out, report


COLLECTORS = {
    "ccf_dataset": collect_ccf,
    "curated": collect_curated,
}


def collect_conferences(
    session: PoliteSession, config: dict[str, Any], today: date
) -> tuple[list[Conference], list[ConferenceSourceReport]]:
    conferences: list[Conference] = []
    reports: list[ConferenceSourceReport] = []
    for spec in config.get("conference_sources", []):
        collector = COLLECTORS.get(spec.get("collector", "curated"))
        if collector is None:
            continue
        try:
            found, report = collector(session, spec, today)
        except Exception as exc:
            report = ConferenceSourceReport(
                spec["id"], spec.get("label", spec["id"]), spec.get("url", "")
            )
            report.status = "error"
            report.notes = f"{type(exc).__name__}: {exc}"
            found = []
        conferences.extend(found)
        reports.append(report)

    conferences.sort(key=lambda c: (c.next_deadline or date.max, c.name))
    return conferences, reports
