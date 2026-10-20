"""Unit tests for the ranking reproduction and metrics in eval/run_eval.py."""
import run_eval as ev


def row(mid, sim, kw, ts="2026-10-01T00:00:00+00:00"):
    return {"id": mid, "similarity": sim, "keyword_score": kw, "updated_at": ts}


ROWS = [row("a", 0.9, 0.1), row("b", 0.5, 0.9), row("c", 0.7, 0.5), row("d", 0.2, 0.0)]


def test_single_rankings():
    assert ev.rank_vector(ROWS) == ["a", "c", "b", "d"]
    assert ev.rank_keyword(ROWS) == ["b", "c"]  # below-threshold memories are not ranked


def test_hybrid_rrf():
    # b: 1/63 + 1/61 > c: 2/62 > a: 1/61 > d: 1/64
    assert ev.rank_hybrid(ROWS) == ["b", "c", "a", "d"]


def test_ties_break_like_migration_004():
    tied = [row("b", 0.2, 0.5), row("a", 0.2, 0.5), row("c", 0.9, 0.5)]
    assert ev.rank_keyword(tied) == ["c", "a", "b"]  # keyword tie -> closer vector -> id
    assert ev.rank_vector(tied) == ["c", "a", "b"]


def test_score_query():
    s = ev.score_query(["a", "c", "b", "d"], ["b", "x"], k=2)
    assert s == {"hit": 0.0, "recall": 0.0, "mrr": 1 / 3}
    # recall is capped at k when more memories are relevant than fit in the top k
    assert ev.score_query(list("abcdefg"), list("abcdefg"), k=5)["recall"] == 1.0
    assert ev.score_query(["b"], ["d"], k=5)["mrr"] == 0.0


def test_paired_bootstrap_is_seeded():
    a, b = [1, 1, 0, 1, 1], [0, 1, 0, 0, 1]
    assert ev.paired_bootstrap(a, b, n=300) == ev.paired_bootstrap(a, b, n=300)
    m, lo, hi = ev.paired_bootstrap(a, b, n=300)
    assert lo <= m <= hi and m == 0.4
