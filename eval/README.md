# Retrieval evaluation

Compares the three ways `search_memory` could rank memories, on queries labeled by hand:

| Method | What it is |
|---|---|
| `vector` | pgvector cosine similarity only |
| `keyword` | pg_trgm `word_similarity` only (score ≥ 0.3) |
| `hybrid` | what the server actually uses: top 30 of each, fused with RRF (k = 60) |

Metrics per method: Hit@5, Recall@5 (capped at the number of relevant memories), MRR. Results are broken down by query type, and hybrid is compared with each baseline using a paired bootstrap over queries.

## Run it

1. In the Supabase SQL Editor, run `migrations/003_eval.sql` once. It only adds a read-only function.
2. Put your labeled queries in `eval/queries.jsonl` (gitignored), one per line:

   ```json
   {"id": "q01", "project": "my-site", "type": "proper_noun", "query": "Astro", "relevant": ["<memory id>"]}
   ```

   Types used so far: `proper_noun`, `zh_short`, `paraphrase`, `english`.
3. With the same `.env` as the server:

   ```bash
   python eval/run_eval.py
   ```

Output goes to `eval/results/`: `summary.md` has aggregate numbers only; `per_query.jsonl` has every query and each method's top 5, and stays local.

The script also calls the live `search_memories` and checks that its local hybrid ranking matches it, so the comparison is against the real search, not a reimplementation. Labeled memories that have since been superseded or retired are reported and dropped.
