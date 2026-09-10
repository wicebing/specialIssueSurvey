"""Ranking maths for the "what is hot in top journals" radar.

The previous report's "趨勢雷達" counted keyword hits across the scraped rows,
so it ranked whatever the scraper happened to over-collect.  With garbage rows
in the input it reported "AI、資料科學與臨床預測：19 筆" whose example was the
string "Sign up for alerts".

This module scores topics from *publication* data instead, and it ranks by
growth rather than volume.  A term that is merely large is not actionable: by
the time "sepsis" is the biggest bucket, it has been the biggest bucket for a
decade.  What a researcher can act on is a term whose share is climbing.

Inputs are deliberately generic ``TermWindow`` records so the same maths works
over PubMed hit counts, OpenAlex topic shares, or Crossref title n-grams.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "TermWindow",
    "TrendScore",
    "score_term",
    "rank_terms",
    "classify_stage",
]


# Additive smoothing keeps a term that went 0 -> 3 from posting an infinite
# growth ratio and swamping terms with real mass behind them.
SMOOTHING = 3.0

# Below this many recent mentions we do not trust the signal at all.
MIN_RECENT_COUNT = 4


@dataclass
class TermWindow:
    """One term's mass in a recent window versus a baseline window.

    ``recent_total`` / ``baseline_total`` are the corpus sizes for each window.
    Supplying them converts raw counts into shares, which is what makes the
    comparison fair when the two windows differ in size or when a journal's
    output grew.
    """

    term: str
    recent_count: int
    baseline_count: int
    recent_total: int = 0
    baseline_total: int = 0
    label: str = ""
    journals: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()

    @property
    def recent_share(self) -> float:
        return self.recent_count / self.recent_total if self.recent_total else float(self.recent_count)

    @property
    def baseline_share(self) -> float:
        return (
            self.baseline_count / self.baseline_total
            if self.baseline_total
            else float(self.baseline_count)
        )


@dataclass
class TrendScore:
    """A ranked term, with every input kept so the report can show its working."""

    term: str
    label: str
    recent_count: int
    baseline_count: int
    growth_ratio: float
    momentum: float
    volume_weight: float
    score: float
    stage: str
    journals: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "label": self.label or self.term,
            "recent_count": self.recent_count,
            "baseline_count": self.baseline_count,
            "growth_ratio": round(self.growth_ratio, 3),
            "momentum": round(self.momentum, 3),
            "volume_weight": round(self.volume_weight, 3),
            "score": round(self.score, 2),
            "stage": self.stage,
            "journals": list(self.journals),
            "examples": list(self.examples),
        }


def classify_stage(growth_ratio: float, recent_count: int) -> str:
    """Label a term by where it sits in its lifecycle.

    The labels drive the advice column in the report: an ``emerging`` term is
    where a fast submission has the least competition, while ``saturated``
    warns that the field is already crowded.
    """
    if recent_count < MIN_RECENT_COUNT:
        return "too_sparse"
    if growth_ratio >= 2.0:
        return "emerging"
    if growth_ratio >= 1.25:
        return "rising"
    if growth_ratio >= 0.8:
        return "steady"
    return "cooling"


def score_term(window: TermWindow) -> TrendScore:
    """Combine growth and volume into one ranking score.

    Growth dominates, because catching a wave early is the point.  Volume enters
    logarithmically, only as a tiebreak, so a term with genuine mass outranks a
    statistical blip at the same growth ratio without burying it.
    """
    recent = window.recent_share
    baseline = window.baseline_share

    # Smoothing is applied on the same scale as the shares so it damps small
    # numbers without distorting terms that already carry real mass.
    scale = 1.0
    if window.recent_total and window.baseline_total:
        scale = SMOOTHING / max(window.recent_total, window.baseline_total)
    else:
        scale = SMOOTHING

    growth_ratio = (recent + scale) / (baseline + scale)
    momentum = math.log2(growth_ratio) if growth_ratio > 0 else 0.0
    volume_weight = math.log1p(max(window.recent_count, 0))

    score = momentum * 10.0 + volume_weight
    if window.recent_count < MIN_RECENT_COUNT:
        # Keep sparse terms visible for inspection but never let them lead.
        score -= 15.0

    return TrendScore(
        term=window.term,
        label=window.label or window.term,
        recent_count=window.recent_count,
        baseline_count=window.baseline_count,
        growth_ratio=growth_ratio,
        momentum=momentum,
        volume_weight=volume_weight,
        score=score,
        stage=classify_stage(growth_ratio, window.recent_count),
        journals=window.journals,
        examples=window.examples,
    )


def rank_terms(windows: Iterable[TermWindow], limit: int | None = None) -> list[TrendScore]:
    """Score every term and return them best-first."""
    scored = [score_term(window) for window in windows]
    scored.sort(key=lambda item: (-item.score, -item.recent_count, item.term))
    return scored[:limit] if limit else scored
