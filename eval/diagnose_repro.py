"""Find out why the local hybrid ranking differs from the live search_memories.

search_memories returns similarity, keyword_score and the fused score for each hit,
so we can compare every live hit with the values eval_rank_memories gives for the
same memory and see which part disagrees.

    python eval/diagnose_repro.py               # only queries whose top k differs
    python eval/diagnose_repro.py --ids q06 q07  # specific queries
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_eval as ev  # noqa: E402


def local_scores(rows):
    vec = ev.rank_vector(rows)
    kw = ev.rank_keyword(rows)
    vr = {m: i for i, m in enumerate(vec, 1)}
    kr = {m: i for i, m in enumerate(kw, 1)}
    fused = {}
    for ranking in (vec[:ev.CANDIDATES], kw[:ev.CANDIDATES]):
        for i, m in enumerate(ranking, 1):
            fused[m] = fused.get(m, 0.0) + 1.0 / (ev.RRF_K + i)
    return vr, kr, fused


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", default=str(ev.ROOT / "queries.jsonl"))
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    from dotenv import load_dotenv
    from openai import OpenAI
    from supabase import create_client
    load_dotenv(ev.ROOT.parent / ".env")
    db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    queries = ev.load_queries(args.queries)
    if args.ids:
        queries = [q for q in queries if q["id"] in set(args.ids)]
    vecs = ev.embed_all(OpenAI(), [q["query"] for q in queries], ev.ROOT / "cache" / "embeddings.json")

    n_diff = 0
    for q in queries:
        params = {"query_embedding": vecs[q["query"]], "query_text": q["query"],
                  "filter_project": q["project"]}
        rows = db.rpc("eval_rank_memories", params).execute().data
        live = db.rpc("search_memories", {**params, "match_count": 10}).execute().data
        by_id = {r["id"]: r for r in rows}
        vr, kr, fused = local_scores(rows)
        local_top = ev.rank_hybrid(rows)[:args.k]
        if not args.ids and [r["id"] for r in live[:args.k]] == local_top:
            continue
        n_diff += 1

        print(f"\n== {q['id']} [{q['project']}] {q['query']!r}  pool={len(rows)}")
        print("   rank  id        live_sim  local_sim  live_kw  local_kw  live_score  local_score  vec_r  kw_r")
        for i, r in enumerate(live, 1):
            m = by_id.get(r["id"])
            if m is None:
                print(f"   {i:>4}  {r['id'][:8]}  NOT IN eval_rank_memories POOL")
                continue
            flag = []
            if abs(r["similarity"] - m["similarity"]) > 1e-6:
                flag.append("SIM")
            if abs(r["keyword_score"] - m["keyword_score"]) > 1e-6:
                flag.append("KW")
            if abs(r["score"] - fused.get(r["id"], 0.0)) > 1e-9:
                flag.append("SCORE")
            print(f"   {i:>4}  {r['id'][:8]}  {r['similarity']:.6f}  {m['similarity']:.6f}   "
                  f"{r['keyword_score']:.4f}   {m['keyword_score']:.4f}    {r['score']:.6f}    "
                  f"{fused.get(r['id'], 0.0):.6f}   {vr.get(r['id'], '-'):>5}  {kr.get(r['id'], '-'):>4}"
                  f"  {' '.join(flag)}")
        print("   local top:", [m[:8] for m in local_top])

    print(f"\n{n_diff} queries shown")


if __name__ == "__main__":
    main()
