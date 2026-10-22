<h1 align="center">shared-brain</h1>

<p align="center"><em>Stop re-explaining your project to every AI you use.</em></p>

<p align="center">
  <a href="https://github.com/anqiXu33/shared-brain/actions/workflows/ci.yml"><img src="https://github.com/anqiXu33/shared-brain/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/MCP-server-8A63D2" alt="MCP server">
  <img src="https://img.shields.io/badge/Postgres-pgvector%20%2B%20pg__trgm-336791?logo=postgresql&logoColor=white" alt="Postgres with pgvector and pg_trgm">
  <img src="https://img.shields.io/badge/Supabase-free%20tier-3ECF8E?logo=supabase&logoColor=white" alt="Supabase free tier">
</p>

<p align="center">A self-hosted MCP server that gives Claude, ChatGPT, Claude Code and any other MCP client one shared long-term memory, kept separate for each project.</p>

<p align="center">
  <img src="docs/architecture.svg" width="100%"
       alt="shared-brain architecture: MCP clients → MCP server on Render (with OpenAI embeddings) → Supabase Postgres">
</p>

## Features

- **Any MCP client**: Claude, ChatGPT, Claude Code and more. Adding one is a URL, no code change.
- **Hybrid search**: pgvector semantic search plus pg_trgm keyword matching, merged with RRF.
- **Typed memories with history**: new states supersede old ones instead of overwriting them.
- **Duplicate-aware**: near-duplicates go back to the assistant to decide.

## Example

```text
Monday, in ChatGPT
  you      Let's go with Astro for the site, the content is static.
  ChatGPT  save_memory(project="my-site", kind="decision",
                       content="my-site: decided on Astro because content is static")

Wednesday, in Claude Code
  you      Set up the site skeleton.
  Claude   search_memory(project="my-site", query="site framework")
           → "my-site: decided on Astro because content is static"
           Scaffolding an Astro project...
```

Nobody had to explain the decision twice.

## Quick start

You need a Supabase project (free), an OpenAI key for embeddings, and Python 3.12.

1. In the Supabase SQL Editor, run `schema.sql`, then the files in `migrations/` in order.
2. Copy `.env.example` to `.env` and fill it in.
3. Run the server:

```bash
pip install -r requirements.txt
python server.py
```

Or with Docker:

```bash
docker build -t shared-brain .
docker run --env-file .env -p 8000:8000 shared-brain
```

Check it with the MCP Inspector (`npx @modelcontextprotocol/inspector`) at `http://localhost:8000/<MCP_SECRET_PATH>`.

<details>
<summary><b>Full setup: deploy and connect your assistants</b></summary>

1. **Database.** Row Level Security is on with no public policies, so only your server can touch the tables.

2. **Environment.** `MCP_SECRET_PATH` is the output of `openssl rand -hex 24`. It becomes the URL path and is the only thing standing between the internet and your memories, so treat it like a password. Projects live in the `projects` table: migration 005 registers every project that already has memories, and new ones are added with the `add_project` tool, no redeploy needed. (`ALLOWED_PROJECTS` is only a fallback for databases that have not run migration 005.) Migration 006 adds project aliases: every tool that takes a `project` accepts an alias too, so "心力球" resolves to `ai-pendant` for every assistant, not just the one that happened to know the nickname.

3. **Deploy.** Push to GitHub and create a Render Web Service from the repo: start command `python server.py`, the same env vars, plus `PYTHON_VERSION=3.12.0`. Free instances sleep after 15 minutes of inactivity and take a few seconds to wake up.

4. **Connect the assistants.** The connector URL is `https://<your-service>.onrender.com/<MCP_SECRET_PATH>`.

   | Client | Where to add it |
   |---|---|
   | Claude web | Settings > Connectors > Add custom connector |
   | ChatGPT desktop | Settings > Plugins > Add MCP server (Streamable HTTP, no auth) |
   | Claude Code | `claude mcp add --transport http --scope user shared-brain <URL>`, on every machine it runs on |

5. **Tell them when to use it.** Tools alone do nothing; the assistant has to know when to reach for them. Put standing instructions in Claude (Profile or Project instructions), ChatGPT (Custom instructions) and Claude Code (`~/.claude/CLAUDE.md`). Mine says roughly:

   > When I mention a project or ask what we decided, search shared memory first. When I make a decision, change a plan or hit a milestone, save one short self-contained entry. When updating, keep the details and change only what changed. Never store secrets, credentials, personal data, or details of security or compliance incidents.

   Then add one line per project in its Claude Project, ChatGPT Project and repo `CLAUDE.md`: "the shared-memory project name is X." After that, "save that" is enough.

</details>

## Tools

| Tool | What it does |
|---|---|
| `save_memory` | Save one typed entry. Rejects near-duplicates (cosine ≥ 0.9); `supersedes` replaces an outdated entry. |
| `update_memory` | Fix wording or kind/tags. The old text is archived by a trigger. |
| `retire_memory` | Mark an entry as no longer valid. Hidden, not deleted. |
| `search_memory` | Hybrid search within one project. |
| `recent_memories` | Latest changes, newest first. |
| `project_brief` | One call for the whole picture: current state, open blockers, latest decisions, last session summary. |
| `delete_memory` | Remove something that was wrong from the start. |
| `list_projects` | Registered projects with a one-line description and aliases each. |
| `add_project` | Register a new project, optionally with aliases. Rejects badly formed names and asks first when a similar name exists (`scai-lab` vs `scai`). |
| `add_alias` | Record another name the user uses for a project. From then on it works as `project` everywhere; an alias cannot point to two projects. |

Superseded and retired entries are hidden unless you pass `include_history`.

## Design choices

| Choice | Why |
|---|---|
| The server never runs an LLM | The assistant already has the context, so judgement calls (like duplicates) go back to it. |
| Typed memories (`kind`) | Decisions, states and blockers age differently. |
| Supersede, don't overwrite | Search shows what is true now; how you got there stays queryable. |
| Vector + keyword search | Embeddings do find exact names and short Chinese queries, but often rank them lower; keyword matching pulls them to the top (see Evaluation). |
| Project registry | A table with a foreign key from every memory, so assistants can't split one project into three spellings. New projects are added by a tool call, not by editing env vars. |

## Evaluation

52 hand-labeled queries over my own memories (about 220 across three projects), comparing the three ways the server could rank results. Top 5, the same as the default `k`.

| Method | Hit@5 | Recall@5 | MRR |
|---|---|---|---|
| Vector only | 0.904 | 0.728 | 0.801 |
| Keyword only | 0.635 | 0.560 | 0.598 |
| **Hybrid (what the server uses)** | 0.885 | 0.743 | **0.854** |

- Hybrid ranks the right memory higher than vector alone: MRR +0.053, paired bootstrap 95% CI [+0.002, +0.110]. Whether it is found at all (Hit@5, Recall@5) is the same.
- The gain comes from exact names (MRR 0.85 → 0.96) and short Chinese queries (0.89 → 0.96). Vector search already found them; it just ranked them lower.
- Reworded questions still depend on vectors alone: keyword matching finds the answer for 2 of 16.

Caveats: a small set, one user's data, and the queries were drafted while looking at the memories, which favours wording that overlaps with the text.

Building the evaluation also found a bug. On short queries, many memories get exactly the same keyword score, and the search broke those ties arbitrarily, so which of them got the keyword boost changed from run to run. Migration 004 breaks ties by vector distance; afterwards the live search matched the offline reproduction on 52 of 52 queries. Code and how to run it: [`eval/`](eval/README.md).

## Why I built this

I use Claude for thinking, ChatGPT for second opinions and Claude Code for the actual work, and none of them talk to each other. Now project state that Claude Code saves late at night is there the next morning when I ask Claude web "what's waiting on my decision?"

## Things I learned the hard way

- The `mcp` Python package shipped a 2.0 the week I built this and renamed `FastMCP`. Pin `mcp<2`.
- Claude Code sometimes passes list arguments as JSON strings. `server.py` coerces them.
- "Don't store sensitive data" is not specific enough. An assistant will avoid the data itself and then store a paragraph *about* it. Spell out what sensitive means.

## Roadmap

- [x] Memory kinds, supersede/retire, hybrid search (migration 002)
- [x] Retrieval evaluation and deterministic tie-breaking (migrations 003, 004)
- [x] `project_brief` tool: recent progress, open decisions, blockers and last session summary in one call
- [ ] Session summaries written automatically by Claude Code via a `Stop` hook
- [x] Project registry in the database instead of an env var (migration 005)
- [x] Project aliases, so any assistant resolves a nickname to the right project (migration 006)
- [ ] Later: OAuth instead of a secret path, a small dashboard

## Tests and CI

Every push runs [GitHub Actions](.github/workflows/ci.yml):

- **Database tests** on a real Postgres with pgvector: `schema.sql` and every migration are applied to a fresh database, then the tests check that hybrid search ranks exactly as the evaluation reproduces it (including keyword ties, the bug migration 004 fixed), hides superseded and retired memories, filters by kind and project, archives old wording on update, and enforces the project registry (unregistered names, name format, renames, backfill).
- **Server unit tests** with a fake database: duplicate rejection, supersede guards, retire, project registration and its similar-name check, and argument coercion through the real MCP tool layer.
- **Docker**: builds the image, starts it with dummy credentials, and runs the MCP handshake and `tools/list` against it.

Locally:

```bash
pip install -r requirements-dev.txt
ruff check . && pytest            # database tests skip without DATABASE_URL
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres pytest
```

## Inspiration

- [AusDavo/mcp-memory-server](https://github.com/AusDavo/mcp-memory-server): same stack; embedding-model tracking and list coercion
- [Caricalia/mcp-brain](https://github.com/Caricalia/mcp-brain): cross-client architecture
- [rnblanco/supa-brain](https://github.com/rnblanco/supa-brain): tool design
- [open-brain-server](https://github.com/Bobby-cell-commits/open-brain-server): dedup and pruning ideas

## Repository

| File | Purpose |
|---|---|
| `server.py` | The MCP server |
| `schema.sql` | Initial database setup |
| `migrations/001_history.sql` | `memory_history` table and trigger |
| `migrations/002_kinds_validity_hybrid.sql` | Memory kinds, supersede/retire, hybrid search |
| `migrations/003_eval.sql` | Read-only scoring function for the retrieval evaluation |
| `migrations/004_deterministic_ties.sql` | Deterministic tie-breaking in hybrid search |
| `migrations/005_projects.sql` | `projects` table and foreign key from memories |
| `migrations/006_project_aliases.sql` | `aliases` column on `projects` |
| `eval/` | Retrieval evaluation: vector vs keyword vs hybrid ([how to run](eval/README.md)) |
| `tests/` | Database tests, server unit tests, HTTP smoke test |
| `Dockerfile` | Container image for the server |
| `.github/workflows/ci.yml` | Lint, tests and Docker build on every push |
| `requirements.txt` | Python dependencies (`requirements-dev.txt` adds test tools) |
| `.env.example` | Environment variable template |
| `CLAUDE.md` | Instructions for Claude Code when working on this repo |
| `docs/architecture.svg` | The architecture diagram above |
