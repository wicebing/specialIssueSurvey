"""Mainstream-trend engine over the top-journal literature.

Answers the question "what should I write about right now?" using what those
journals are actually publishing, rather than what our CFP scraper happened to
collect.

Two complementary passes:

*Tracked terms* score a curated vocabulary (LLMs, prehospital triage, sepsis
phenotyping, ...) by how much its share of the corpus grew between a baseline
window and a recent one.

*Discovered terms* mine n-grams straight out of recent article titles and
compare them against the baseline window's titles.  This is what surfaces a
topic nobody thought to put on the list, which is precisely the wave a
researcher wants to catch early.

Both passes run against NCBI E-utilities, which is free and needs no API key
(an ``NCBI_API_KEY`` merely raises the rate limit from 3/sec to 10/sec).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from scripts.http_client import PoliteSession
from scripts.trend_score import TermWindow, TrendScore, rank_terms

__all__ = [
    "TrendWindows",
    "pubmed_count",
    "collect_titles",
    "score_tracked_terms",
    "discover_emerging_terms",
    "build_trend_report",
]

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


@dataclass
class TrendWindows:
    """The two comparison windows.

    Defaults compare the trailing 180 days against the same 180 days one year
    earlier.  Aligning the windows on the calendar cancels out the seasonality
    in academic publishing, which a naive "last 6 months vs the 6 before that"
    comparison would read as a trend.
    """

    recent_start: date
    recent_end: date
    baseline_start: date
    baseline_end: date

    @classmethod
    def trailing(cls, today: date, span_days: int = 180) -> "TrendWindows":
        recent_end = today
        recent_start = today - timedelta(days=span_days)
        return cls(
            recent_start=recent_start,
            recent_end=recent_end,
            baseline_start=recent_start - timedelta(days=365),
            baseline_end=recent_end - timedelta(days=365),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "recent_start": self.recent_start.isoformat(),
            "recent_end": self.recent_end.isoformat(),
            "baseline_start": self.baseline_start.isoformat(),
            "baseline_end": self.baseline_end.isoformat(),
        }


def _date_clause(start: date, end: date) -> str:
    return f'("{start:%Y/%m/%d}"[dp] : "{end:%Y/%m/%d}"[dp])'


def _journal_clause(journals: Sequence[str]) -> str:
    if not journals:
        return ""
    return "(" + " OR ".join(f'"{name}"[Journal]' for name in journals) + ")"


def build_query(
    term: str | None,
    journals: Sequence[str],
    start: date,
    end: date,
) -> str:
    parts = [_date_clause(start, end)]
    journal_clause = _journal_clause(journals)
    if journal_clause:
        parts.append(journal_clause)
    if term:
        # Title/abstract only: a MeSH-expanded search would match papers that
        # merely touch the topic and would wash out the growth signal.
        parts.append(f'("{term}"[tiab])')
    return " AND ".join(parts)


def pubmed_count(
    session: PoliteSession,
    query: str,
    api_key: str = "",
) -> int:
    """Return how many PubMed records match, using esearch's count mode."""
    params: dict[str, Any] = {
        "db": "pubmed",
        "term": query,
        "rettype": "count",
        "retmode": "json",
        "tool": "academic-cfp-tracker",
    }
    if session.contact_email:
        params["email"] = session.contact_email
    if api_key:
        params["api_key"] = api_key

    result = session.get(f"{EUTILS}/esearch.fcgi", params=params)
    if not result.ok:
        return 0
    try:
        return int(result.json()["esearchresult"]["count"])
    except (KeyError, ValueError, TypeError):
        return 0


def collect_titles(
    session: PoliteSession,
    journals: Sequence[str],
    start: date,
    end: date,
    limit: int = 400,
    api_key: str = "",
) -> list[str]:
    """Fetch article titles for one journal set and window."""
    query = build_query(None, journals, start, end)
    params: dict[str, Any] = {
        "db": "pubmed",
        "term": query,
        "retmax": limit,
        "retmode": "json",
        "sort": "pub_date",
        "tool": "academic-cfp-tracker",
    }
    if session.contact_email:
        params["email"] = session.contact_email
    if api_key:
        params["api_key"] = api_key

    search = session.get(f"{EUTILS}/esearch.fcgi", params=params)
    if not search.ok:
        return []
    try:
        ids = search.json()["esearchresult"]["idlist"]
    except (KeyError, TypeError):
        return []
    if not ids:
        return []

    titles: list[str] = []
    # esummary caps out well below our retmax, so page through in chunks.
    for offset in range(0, len(ids), 200):
        chunk = ids[offset : offset + 200]
        summary_params: dict[str, Any] = {
            "db": "pubmed",
            "id": ",".join(chunk),
            "retmode": "json",
            "tool": "academic-cfp-tracker",
        }
        if session.contact_email:
            summary_params["email"] = session.contact_email
        if api_key:
            summary_params["api_key"] = api_key
        summary = session.get(f"{EUTILS}/esummary.fcgi", params=summary_params)
        if not summary.ok:
            continue
        try:
            payload = summary.json()["result"]
        except (KeyError, TypeError):
            continue
        for uid in payload.get("uids", []):
            record = payload.get(uid) or {}
            title = (record.get("title") or "").strip()
            if title:
                titles.append(title)
    return titles


def score_tracked_terms(
    session: PoliteSession,
    terms: Iterable[dict[str, Any]],
    journals: Sequence[str],
    windows: TrendWindows,
    api_key: str = "",
) -> list[TrendScore]:
    """Score a curated vocabulary by growth in corpus share."""
    recent_total = pubmed_count(
        session,
        build_query(None, journals, windows.recent_start, windows.recent_end),
        api_key,
    )
    baseline_total = pubmed_count(
        session,
        build_query(None, journals, windows.baseline_start, windows.baseline_end),
        api_key,
    )

    term_windows: list[TermWindow] = []
    for entry in terms:
        term = entry["term"] if isinstance(entry, dict) else str(entry)
        label = entry.get("label", term) if isinstance(entry, dict) else term
        recent = pubmed_count(
            session,
            build_query(term, journals, windows.recent_start, windows.recent_end),
            api_key,
        )
        baseline = pubmed_count(
            session,
            build_query(term, journals, windows.baseline_start, windows.baseline_end),
            api_key,
        )
        term_windows.append(
            TermWindow(
                term=term,
                label=label,
                recent_count=recent,
                baseline_count=baseline,
                recent_total=recent_total,
                baseline_total=baseline_total,
            )
        )
    return rank_terms(term_windows)


# ---------------------------------------------------------------------------
# Discovery from titles
# ---------------------------------------------------------------------------

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "for", "to", "with",
    "from", "by", "at", "as", "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "it", "its", "we", "our", "their", "his",
    "her", "study", "trial", "analysis", "review", "using", "based", "among",
    "after", "before", "during", "between", "versus", "vs", "not", "no", "new",
    "use", "used", "usage", "effect", "effects", "impact", "role", "results",
    "outcome", "outcomes", "patients", "patient", "care", "clinical", "health",
    "medical", "medicine", "hospital", "association", "associated", "risk",
    "factors", "randomized", "randomised", "controlled", "cohort", "prospective",
    "retrospective", "systematic", "meta", "multicenter", "multicentre", "pilot",
    "case", "report", "letter", "editorial", "comment", "response", "reply",
    "correction", "erratum", "author", "authors", "versus", "one", "two", "three",
    "can", "may", "does", "do", "how", "what", "why", "who", "when", "which",
    "more", "most", "less", "than", "into", "over", "under", "about", "per",
}

TOKEN_RE = re.compile(r"[a-z][a-z0-9\-]{2,}")


def _ngrams(title: str, sizes: Sequence[int] = (2, 3)) -> list[str]:
    words = [w for w in TOKEN_RE.findall(title.lower())]
    grams: list[str] = []
    for size in sizes:
        for index in range(len(words) - size + 1):
            window = words[index : index + size]
            # Drop a gram that is mostly filler; the interior may be a stopword
            # ("hospital at home"), but the edges carry the meaning.
            if window[0] in STOPWORDS or window[-1] in STOPWORDS:
                continue
            if all(word in STOPWORDS for word in window):
                continue
            grams.append(" ".join(window))
    return grams


def _stem(word: str) -> str:
    """Crude singularisation, used only for grouping near-duplicate n-grams."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ss"):
        return word
    if word.endswith("s") and len(word) > 3:
        return word[:-1]
    return word


def _key_tokens(term: str) -> frozenset[str]:
    return frozenset(_stem(part) for part in re.split(r"[\s\-]+", term.lower()) if part)


def consolidate_families(scores: Sequence[TrendScore]) -> list[TrendScore]:
    """Collapse overlapping n-grams into one representative phrase each.

    Raw n-gram mining emits "large language", "language model", "large language
    model" and "large language models" as four separate rows, which crowds out
    every other topic.  Terms whose token sets nest are one family; the
    representative is the most specific phrase that still carries most of the
    family's volume, so the report says "large language models" rather than the
    truncated "large language".
    """
    families: list[list[TrendScore]] = []
    for score in scores:
        tokens = _key_tokens(score.term)
        for family in families:
            if any(
                tokens <= _key_tokens(member.term) or _key_tokens(member.term) <= tokens
                for member in family
            ):
                family.append(score)
                break
        else:
            families.append([score])

    representatives: list[TrendScore] = []
    for family in families:
        peak = max(member.recent_count for member in family)
        # Ignore fragments that carry little of the family's mass, then prefer
        # the longest surviving phrase.
        substantial = [m for m in family if m.recent_count >= peak * 0.3] or family
        best = max(
            substantial,
            key=lambda m: (len(_key_tokens(m.term)), m.recent_count, m.score),
        )
        representatives.append(best)

    representatives.sort(key=lambda item: (-item.score, -item.recent_count, item.term))
    return representatives


# Study-design vocabulary. These n-grams rise and fall with reporting fashion,
# not with what a field is investigating, so they crowd out real topics.
METHOD_PHRASES = (
    "development and validation",
    "development and external",
    "external validation",
    "internal validation",
    "systematic review",
    "scoping review",
    "narrative review",
    "meta-analysis",
    "meta analysis",
    "network meta-analysis",
    "randomized controlled",
    "randomised controlled",
    "controlled trial",
    "mixed methods",
    "qualitative study",
    "cross-sectional",
    "cohort study",
    "observational study",
    "feasibility study",
    "pilot study",
    "secondary analysis",
    "post hoc",
    "study protocol",
    "protocol for",
    "consensus statement",
    "clinical practice guideline",
    "scientific statement",
    "narrative synthesis",
    "learning framework",
    "learning model",
    "support system",
    "predictive model",
    "prediction model",
    "machine learning-based",
    "deep learning-based",
    "based approach",
    "novel approach",
    "comparative study",
    "real-world data",
    "single center",
    "single centre",
    "model development",
)


def _is_methodology_phrase(term: str) -> bool:
    lowered = term.lower()
    return any(phrase in lowered or lowered in phrase for phrase in METHOD_PHRASES)


def discover_emerging_terms(
    recent_titles: Sequence[str],
    baseline_titles: Sequence[str],
    min_recent: int = 4,
    limit: int = 25,
    exclude_terms: Sequence[str] = (),
) -> list[TrendScore]:
    """Find title n-grams whose share grew, without a predefined vocabulary.

    ``exclude_terms`` drops grams already scored in the tracked-vocabulary pass,
    so this section shows only what the curated list missed - which is the whole
    reason it exists.
    """
    recent_counts: Counter[str] = Counter()
    for title in recent_titles:
        recent_counts.update(set(_ngrams(title)))
    baseline_counts: Counter[str] = Counter()
    for title in baseline_titles:
        baseline_counts.update(set(_ngrams(title)))

    excluded = {term.lower() for term in exclude_terms}

    def already_tracked(gram: str) -> bool:
        lowered = gram.lower()
        return any(lowered in term or term in lowered for term in excluded)

    windows = [
        TermWindow(
            term=gram,
            recent_count=count,
            baseline_count=baseline_counts.get(gram, 0),
            recent_total=max(len(recent_titles), 1),
            baseline_total=max(len(baseline_titles), 1),
        )
        for gram, count in recent_counts.items()
        if count >= min_recent and not _is_methodology_phrase(gram) and not already_tracked(gram)
    ]
    ranked = rank_terms(windows)
    growing = [item for item in ranked if item.stage in {"emerging", "rising"}]
    return consolidate_families(growing)[:limit]


def build_trend_report(
    session: PoliteSession,
    config: dict[str, Any],
    today: date,
    api_key: str = "",
) -> dict[str, Any]:
    """Run both passes and return a serialisable trend block."""
    trend_config = config.get("trend_engine", {})
    journals = trend_config.get("pubmed_journals", [])
    terms = trend_config.get("tracked_terms", [])
    span = int(trend_config.get("window_days", 180))
    windows = TrendWindows.trailing(today, span_days=span)

    tracked = score_tracked_terms(session, terms, journals, windows, api_key) if terms else []

    sample = int(trend_config.get("title_sample_size", 1000))
    recent_titles = collect_titles(
        session, journals, windows.recent_start, windows.recent_end,
        limit=sample, api_key=api_key,
    )
    baseline_titles = collect_titles(
        session, journals, windows.baseline_start, windows.baseline_end,
        limit=sample, api_key=api_key,
    )
    discovered = discover_emerging_terms(
        recent_titles,
        baseline_titles,
        exclude_terms=[entry["term"] if isinstance(entry, dict) else str(entry) for entry in terms],
    )

    return {
        "windows": windows.to_dict(),
        "journals_tracked": list(journals),
        "corpus": {
            "recent_titles": len(recent_titles),
            "baseline_titles": len(baseline_titles),
        },
        "tracked_terms": [item.to_dict() for item in tracked],
        "discovered_terms": [item.to_dict() for item in discovered],
    }
