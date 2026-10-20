"""Database tests: run against a real Postgres with every migration applied (see conftest.db)."""
import random
import uuid

import pytest
import run_eval

DIM = 1536
PROJECT = "test-search"
# A small vocabulary makes many memories share the same keyword score for short
# queries, which is exactly the situation migration 004 fixes.
WORDS = ["alpha", "beta", "gamma", "delta", "memory", "search", "ranking", "Jetson", "emotion2vec", "卡点", "部署"]


def vec(rng):
    v = [rng.gauss(0, 1) for _ in range(DIM)]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def lit(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def insert(db, content, embedding, project=PROJECT, kind="context", source="test"):
    return db.execute(
        "insert into memories (content, project, source, kind, embedding, embedding_model) "
        "values (%s, %s, %s, %s, %s::vector, 'test') returning id",
        (content, project, source, kind, lit(embedding)),
    ).fetchone()[0]


@pytest.fixture(scope="module")
def corpus(db):
    rng = random.Random(0)
    ids = []
    for i in range(80):
        words = rng.sample(WORDS, rng.randint(1, 3))
        ids.append(insert(db, f"note {i}: " + " ".join(words), vec(rng),
                          kind=rng.choice(["state", "decision", "context"])))
    return ids


def rpc(db, fn, **params):
    names = ", ".join(f"{k} => %({k})s" for k in params)
    cur = db.execute(f"select * from {fn}({names})", params)
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def search(db, qvec, text, **extra):
    return rpc(db, "search_memories", query_embedding=lit(qvec), query_text=text,
               filter_project=extra.pop("project", PROJECT), **extra)


def reference_ranking(db, qvec, text):
    rows = rpc(db, "eval_rank_memories", query_embedding=lit(qvec), query_text=text,
               filter_project=PROJECT)
    for r in rows:
        r["id"] = str(r["id"])
        r["updated_at"] = r["updated_at"].isoformat()
    return rows, run_eval.rank_hybrid(rows)


QUERIES = ["memory", "Jetson", "emotion2vec", "卡点", "search ranking", "部署", "gamma delta"]


@pytest.mark.parametrize("text", QUERIES)
def test_search_matches_offline_reference(db, corpus, text):
    """search_memories must rank exactly as eval/run_eval.py reproduces it, ties included."""
    qvec = vec(random.Random(text))
    live = [str(r["id"]) for r in search(db, qvec, text, match_count=15)]
    _, ref = reference_ranking(db, qvec, text)
    assert live == ref[:15]


def test_corpus_has_keyword_ties(db, corpus):
    """Guard: the reference test above is only meaningful if keyword scores actually tie."""
    rows, _ = reference_ranking(db, vec(random.Random("memory")), "memory")
    scores = [round(r["keyword_score"], 6) for r in rows if r["keyword_score"] >= run_eval.KW_THRESHOLD]
    assert len(scores) - len(set(scores)) >= 5


def test_search_is_deterministic(db, corpus):
    qvec = vec(random.Random("ties"))
    first = [r["id"] for r in search(db, qvec, "memory", match_count=20)]
    for _ in range(3):
        assert [r["id"] for r in search(db, qvec, "memory", match_count=20)] == first


def test_keyword_rescues_exact_term(db):
    """A memory whose embedding is far from the query is still found through keyword match."""
    rng = random.Random(1)
    project = "test-keyword"
    qvec = vec(rng)
    target = insert(db, "Decided on emotion2vec for speech emotion", [-x for x in qvec], project=project)
    for i in range(40):
        insert(db, f"unrelated note {i}", vec(rng), project=project)
    ids = [r["id"] for r in search(db, qvec, "emotion2vec", project=project, match_count=5)]
    assert target in ids


def test_inactive_memories_hidden_by_default(db):
    rng = random.Random(2)
    project = "test-validity"
    qvec = vec(rng)
    old = insert(db, "status: step one done", qvec, project=project, kind="state")
    new = insert(db, "status: step two done", qvec, project=project, kind="state")
    retired = insert(db, "blocker: waiting for access", qvec, project=project, kind="blocker")
    db.execute("update memories set superseded_by = %s, valid_until = now() where id = %s", (new, old))
    db.execute("update memories set valid_until = now() where id = %s", (retired,))

    default = {r["id"] for r in search(db, qvec, "status", project=project, match_count=10)}
    assert default == {new}

    everything = {r["id"] for r in search(db, qvec, "status", project=project,
                                          match_count=10, include_inactive=True)}
    assert {old, new, retired} <= everything

    dedup = {r["id"] for r in rpc(db, "match_memories", query_embedding=lit(qvec),
                                  filter_project=project, match_count=5)}
    assert dedup == {new}


def test_kind_filter(db, corpus):
    rows = search(db, vec(random.Random(3)), "memory", match_count=50, filter_kinds=["decision"])
    assert rows and all(r["kind"] == "decision" for r in rows)


def test_projects_are_isolated(db, corpus):
    rows = search(db, vec(random.Random(4)), "memory", project="no-such-project", match_count=10)
    assert rows == []


def test_history_trigger_archives_content_changes_only(db):
    mid = insert(db, "first wording", vec(random.Random(5)), project="test-history")
    db.execute("update memories set kind = 'decision' where id = %s", (mid,))
    assert db.execute("select count(*) from memory_history where memory_id = %s", (mid,)).fetchone()[0] == 0
    db.execute("update memories set content = 'second wording' where id = %s", (mid,))
    rows = db.execute("select old_content from memory_history where memory_id = %s", (mid,)).fetchall()
    assert rows == [("first wording",)]


@pytest.mark.parametrize("column, value", [("kind", "opinion"), ("source", "  ")])
def test_constraints_reject_bad_values(db, column, value):
    import psycopg

    kind = value if column == "kind" else "context"
    source = value if column == "source" else "test"
    with pytest.raises(psycopg.errors.CheckViolation):
        insert(db, f"bad {column} {uuid.uuid4()}", vec(random.Random(6)),
               project="test-constraints", kind=kind, source=source)
