<h1 align="center">shared-brain</h1>

<p align="center"><em>Stop re-explaining your project to every AI you use.</em></p>

<p align="center">
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

- **Any MCP client.** Claude, ChatGPT, Claude Code, or anything else that speaks MCP. Adding one is a URL in its settings, no code change.
- **Hybrid search.** Semantic search (pgvector) plus keyword matching (pg_trgm), merged with reciprocal rank fusion. Finds both "what did we decide about evaluation?" and exact names like `emotion2vec`.
- **Typed memories with history.** Every entry is a `state`, `decision`, `blocker`, `context`, `log` or `session-summary`. New states supersede old ones instead of overwriting them.
- **Duplicate-aware.** Near-duplicates are rejected and handed back to the assistant, which decides whether to update, supersede or reword.

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

Check it with the MCP Inspector (`npx @modelcontextprotocol/inspector`) at `http://localhost:8000/<MCP_SECRET_PATH>`.

<details>
<summary><b>Full setup: deploy and connect your assistants</b></summary>

1. **Database.** Row Level Security is on with no public policies, so only your server can touch the tables.

2. **Environment.** `MCP_SECRET_PATH` is the output of `openssl rand -hex 24`. It becomes the URL path and is the only thing standing between the internet and your memories, so treat it like a password. `ALLOWED_PROJECTS` lists the project names assistants may use.

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
| `save_memory` | Save one fact, decision or status, with a `kind`. |
| `update_memory` | Correct wording or reclassify kind/tags. |
| `retire_memory` | Mark an entry as no longer valid, without deleting it. |
| `search_memory` | Hybrid search within one project. |
| `recent_memories` | Newest first by last change. Good for "what happened this week." |
| `delete_memory` | For things that were wrong from the start. |

- `save_memory` rejects near-duplicates (cosine ≥ 0.9 by default) and returns the existing entry. With `supersedes=<id>` it replaces an outdated entry and keeps the old one as history.
- `update_memory` archives the old wording through a database trigger, because assistants sometimes "update" by writing something shorter.
- Superseded and retired entries are hidden unless you pass `include_history`.

## Design choices

**The server is deliberately dumb.** It stores, embeds, searches and checks for near-duplicates, but never runs an LLM of its own. The assistant already has the conversation context, so judgement calls go back to it.

**Kinds, because memories age differently.** A decision stays true until it is reversed; a state is wrong the moment the next one arrives; a blocker should disappear once solved.

**Supersede instead of overwrite.** Normal search shows only what is true now, while the path that led there stays queryable. Same idea as the bi-temporal edges in [Graphiti](https://github.com/getzep/graphiti), scaled down to two columns.

**Hybrid search, because projects are full of exact names.** Embeddings handle meaning but miss names like `Memobase` or `BERSt`, and short Chinese queries against mixed-language entries often score low on cosine similarity alone. Trigram matching catches those; RRF merges the two rankings without tuning weights.

**A project whitelist.** Without it, three assistants invent three spellings of the same project and quietly split your memory into three piles. This happened during testing.

## Why I built this

I use Claude on the web for thinking, ChatGPT for second opinions, and Claude Code for the actual work. None of them talk to each other, so every switch meant re-explaining the plan, the options already ruled out, and the next step.

Now I mostly forget it is there. The moment it clicked: Claude Code, on a remote machine, wrote a batch of project state into memory late one night. The next morning I opened a fresh Claude web chat, asked "what's waiting on my decision?" and got a correct, prioritised list I had not typed.

## Things I learned the hard way

- The `mcp` Python package shipped a 2.0 the week I built this and renamed `FastMCP`. Pin `mcp<2`.
- Claude Code sometimes passes list arguments as JSON strings. `server.py` coerces them.
- "Don't store sensitive data" is not specific enough. An assistant will avoid the data itself and then store a paragraph *about* it. Spell out what sensitive means.

## Roadmap

- [x] Memory kinds, supersede/retire, hybrid search (migration 002)
- [ ] `project_brief` tool: recent progress, open decisions, blockers and last session summary in one call
- [ ] Session summaries written automatically by Claude Code via a `Stop` hook
- [ ] Project registry in the database instead of an env var
- [ ] Later: OAuth instead of a secret path, a small dashboard

## Inspiration

- [AusDavo/mcp-memory-server](https://github.com/AusDavo/mcp-memory-server): same stack; embedding-model tracking and list coercion
- [Caricalia/mcp-brain](https://github.com/Caricalia/mcp-brain): cross-client architecture
- [rnblanco/supa-brain](https://github.com/rnblanco/supa-brain): tool design
- [open-brain-server](https://github.com/Bobby-cell-commits/open-brain-server): dedup and pruning ideas

## Repository

```
server.py                    the MCP server
schema.sql                   initial database setup
migrations/001_history.sql   memory_history table + trigger
migrations/002_kinds_validity_hybrid.sql   memory kinds, supersede/retire, hybrid search (backs up both tables first)
requirements.txt
.env.example
CLAUDE.md                    instructions for Claude Code when working on this repo
docs/architecture.svg        animated architecture diagram used in this README
```
