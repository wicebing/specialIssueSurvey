from scripts.trend_score import TermWindow, classify_stage, rank_terms, score_term


def window(term, recent, baseline, total=5000):
    return TermWindow(
        term=term,
        recent_count=recent,
        baseline_count=baseline,
        recent_total=total,
        baseline_total=total,
    )


def test_rising_term_outranks_large_flat_term():
    """The whole point of the radar: catchable waves beat entrenched topics."""
    ranked = rank_terms([window("sepsis", 400, 390), window("llm", 120, 25)])
    assert ranked[0].term == "llm"


def test_flat_high_volume_term_is_steady_not_emerging():
    assert score_term(window("sepsis", 400, 390)).stage == "steady"


def test_declining_term_is_cooling_and_ranks_last():
    ranked = rank_terms([window("llm", 120, 25), window("blockchain", 8, 40)])
    assert ranked[-1].term == "blockchain"
    assert ranked[-1].stage == "cooling"


def test_sparse_blip_is_penalised():
    scored = score_term(window("blip", 2, 0))
    assert scored.stage == "too_sparse"
    assert scored.score < 0


def test_zero_baseline_does_not_produce_infinite_growth():
    scored = score_term(window("brand new", 30, 0))
    assert scored.growth_ratio < float("inf")
    assert scored.stage == "emerging"


def test_shares_normalise_across_differently_sized_windows():
    """Same share in both windows is steady even when the corpus doubled."""
    scored = score_term(
        TermWindow("x", recent_count=200, baseline_count=100, recent_total=10000, baseline_total=5000)
    )
    assert scored.stage == "steady"


def test_growth_survives_corpus_growth():
    scored = score_term(
        TermWindow("x", recent_count=600, baseline_count=100, recent_total=10000, baseline_total=5000)
    )
    assert scored.stage in {"rising", "emerging"}


def test_classify_stage_boundaries():
    assert classify_stage(2.5, 50) == "emerging"
    assert classify_stage(1.4, 50) == "rising"
    assert classify_stage(1.0, 50) == "steady"
    assert classify_stage(0.5, 50) == "cooling"
    assert classify_stage(5.0, 2) == "too_sparse"


def test_ranking_is_deterministic():
    terms = [window("a", 10, 5), window("b", 10, 5), window("c", 20, 5)]
    assert [t.term for t in rank_terms(terms)] == [t.term for t in rank_terms(terms)]


def test_limit_truncates():
    terms = [window(str(i), 10 + i, 5) for i in range(10)]
    assert len(rank_terms(terms, limit=3)) == 3
