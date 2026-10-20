"""Retrieval evaluation: vector-only vs keyword-only vs hybrid (RRF).

Needs migrations/003_eval.sql and the same .env as server.py
(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, OPENAI_API_KEY).

    python eval/run_eval.py                    # uses eval/queries.jsonl
    python eval/run_eval.py --queries my.jsonl --k 5

queries.jsonl, one query per line:
    {"id": "q01", "project": "...", "type": "proper_noun", "query": "...", "relevant": ["<memory id>", ...]}

Writes
    eval/results/summary.md       aggregate numbers only (safe to publish)
    eval/results/per_query.jsonl  every query with each method's top-k (contains memory ids
                                  and query text; gitignored)
Query embeddings are cached in eval/cache/ so re-runs cost nothing.
"""
import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

# Must mirror search_memories in migrations/002_kinds_validity_hybrid.sql
RRF_K = 60
CANDIDATES = 30
KW_THRESHOLD = 0.3
EMBED_MODEL = "text-embedding-3-small"

METHODS = ("vector", "keyword", "hybrid")
METRICS = ("hit", "recall", "mrr")

ROOT = Path(__file__).resolve().parent


# ---------- ranking (pure functions, mirror the SQL) ----------
def rank_vector(rows):
    return [r["id"] for r in sorted(rows, key=lambda r: -r["similarity"])]


def rank_keyword(rows):
    kw = [r for r in rows if r["keyword_score"] >= KW_THRESHOLD]
    return [r["id"] for r in sorted(kw, key=lambda r: -r["keyword_score"])]


def rank_hybrid(rows):
    vec = rank_vector(rows)[:CANDIDATES]
    kw = rank_keyword(rows)[:CANDIDATES]
    score = {}
    for ranking in (vec, kw):
        for i, mid in enumerate(ranking, 1):
            score[mid] = score.get(mid, 0.0) + 1.0 / (RRF_K + i)
    updated = {r["id"]: r["updated_at"] for r in rows}
    # SQL: order by fscore desc, updated_at desc
    return sorted(score, key=lambda m: (-score[m], _neg_time(updated[m])))


def _neg_time(ts):
    return -datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


RANKERS = {"vector": rank_vector, "keyword": rank_keyword, "hybrid": rank_hybrid}


# ---------- metrics ----------
def score_query(ranking, relevant, k):
    rel = set(relevant)
    top = ranking[:k]
    found = len(rel & set(top))
    first = next((i for i, m in enumerate(ranking, 1) if m in rel), None)
    return {
        "hit": 1.0 if found else 0.0,                 # any relevant memory in top k
        "recall": found / min(len(rel), k),           # capped so 6 relevant / k=5 can still reach 1
        "mrr": 1.0 / first if first else 0.0,         # unranked (e.g. below keyword threshold) = 0
    }


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def paired_bootstrap(a, b, n=2000, seed=0):
    """95% CI of mean(a - b), resampling queries."""
    rng = random.Random(seed)
    d = [x - y for x, y in zip(a, b)]
    stats = sorted(mean([d[rng.randrange(len(d))] for _ in d]) for _ in range(n))
    return mean(d), stats[int(0.025 * n)], stats[int(0.975 * n) - 1]


# ---------- data access ----------
def load_queries(path):
    qs = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    ids = [q["id"] for q in qs]
    if len(ids) != len(set(ids)):
        sys.exit("duplicate query ids in " + str(path))
    for q in qs:
        if not q.get("relevant"):
            sys.exit(f"{q['id']}: no relevant memories labeled")
    return qs


def embed_all(oai, texts, cache_path):
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    missing = [t for t in dict.fromkeys(texts) if f"{EMBED_MODEL}::{t}" not in cache]
    for i in range(0, len(missing), 100):
        batch = missing[i:i + 100]
        res = oai.embeddings.create(model=EMBED_MODEL, input=batch)
        for t, d in zip(batch, res.data):
            cache[f"{EMBED_MODEL}::{t}"] = d.embedding
    if missing:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache))
    return {t: cache[f"{EMBED_MODEL}::{t}"] for t in texts}


# ---------- report ----------
def fmt_ci(t):
    m, lo, hi = t
    return f"{m:+.3f} [{lo:+.3f}, {hi:+.3f}]"


def write_summary(path, per_query, k, n_boot, run_meta):
    lines = [f"# Retrieval evaluation", "",
             f"Run {run_meta['time']} · {len(per_query)} queries · k = {k} · "
             f"embedding {EMBED_MODEL} · RRF k = {RRF_K}, {CANDIDATES} candidates per list, "
             f"keyword threshold {KW_THRESHOLD}", ""]
    if run_meta["skipped"]:
        lines += [f"Skipped {len(run_meta['skipped'])} queries whose labeled memories are no longer "
                  f"active: {', '.join(run_meta['skipped'])}", ""]
    lines += [f"Hybrid reproduction check against `search_memories`: "
              f"{run_meta['repro_ok']}/{len(per_query)} queries identical top {k}", ""]

    groups = {"all": per_query}
    for q in per_query:
        groups.setdefault(q["type"], []).append(q)

    lines += ["## Scores", "",
              f"| Queries | n | Method | Hit@{k} | Recall@{k} | MRR |", "|---|---|---|---|---|---|"]
    for g, qs in groups.items():
        for m in METHODS:
            vals = [mean([q["scores"][m][x] for q in qs]) for x in METRICS]
            lines.append(f"| {g} | {len(qs)} | {m} | " + " | ".join(f"{v:.3f}" for v in vals) + " |")
    lines += ["", f"## Hybrid minus baseline (all queries, paired bootstrap, {n_boot} resamples, 95% CI)", "",
              f"| Baseline | Hit@{k} | Recall@{k} | MRR |", "|---|---|---|---|"]
    for base in ("vector", "keyword"):
        cells = [fmt_ci(paired_bootstrap([q["scores"]["hybrid"][x] for q in per_query],
                                         [q["scores"][base][x] for q in per_query], n=n_boot))
                 for x in METRICS]
        lines.append(f"| {base} | " + " | ".join(cells) + " |")
    lines += ["", "Recall@k is capped: found / min(#relevant, k). "
              "MRR counts a memory the method never ranks (keyword score below threshold) as 0.", ""]
    path.write_text("\n".join(lines))


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", default=str(ROOT / "queries.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "results"))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--bootstrap", type=int, default=2000)
    args = ap.parse_args()

    from dotenv import load_dotenv
    from openai import OpenAI
    from supabase import create_client
    import os
    load_dotenv(ROOT.parent / ".env")
    db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    oai = OpenAI()

    queries = load_queries(args.queries)
    vecs = embed_all(oai, [q["query"] for q in queries], ROOT / "cache" / "embeddings.json")

    per_query, skipped, repro_ok = [], [], 0
    for q in queries:
        rows = db.rpc("eval_rank_memories", {
            "query_embedding": vecs[q["query"]], "query_text": q["query"],
            "filter_project": q["project"],
        }).execute().data
        active = {r["id"] for r in rows}
        stale = [m for m in q["relevant"] if m not in active]
        if stale:
            print(f"{q['id']}: {len(stale)} labeled memories no longer active: {stale}", file=sys.stderr)
        relevant = [m for m in q["relevant"] if m in active]
        if not relevant:
            skipped.append(q["id"])
            continue

        rankings = {m: RANKERS[m](rows) for m in METHODS}
        live = db.rpc("search_memories", {
            "query_embedding": vecs[q["query"]], "query_text": q["query"],
            "filter_project": q["project"], "match_count": args.k,
        }).execute().data
        live_top = [r["id"] for r in live]
        same = live_top == rankings["hybrid"][:args.k]
        repro_ok += same
        if not same:
            print(f"{q['id']}: hybrid reproduction differs from search_memories "
                  f"(ties?) local={rankings['hybrid'][:args.k]} live={live_top}", file=sys.stderr)

        per_query.append({
            **{x: q[x] for x in ("id", "project", "type", "query")},
            "relevant": relevant, "stale": stale, "pool_size": len(rows),
            "top": {m: rankings[m][:args.k] for m in METHODS},
            "scores": {m: score_query(rankings[m], relevant, args.k) for m in METHODS},
        })

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "per_query.jsonl").write_text(
        "\n".join(json.dumps(q, ensure_ascii=False) for q in per_query) + "\n")
    meta = {"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "skipped": skipped, "repro_ok": repro_ok}
    write_summary(out / "summary.md", per_query, args.k, args.bootstrap, meta)
    print((out / "summary.md").read_text())


if __name__ == "__main__":
    main()
