"""共享记忆 MCP server

架构：Claude 网页版 / ChatGPT / Claude Code / 其他 MCP 客户端 --MCP--> 本 server --> Supabase (pgvector + pg_trgm)
                                                                           +--> OpenAI embedding

需要数据库已运行 schema.sql、migrations/001、002；项目注册表需要 005，项目别名需要 006。

环境变量（写在 .env，或托管平台后台）：
  SUPABASE_URL               Supabase 项目地址
  SUPABASE_SERVICE_ROLE_KEY  service_role 密钥，只能放在 server 端
  OPENAI_API_KEY             OpenAI API 密钥（只用来算 embedding）
  MCP_SECRET_PATH            一串长随机字符，作为 URL 路径，相当于密码
  DEDUP_THRESHOLD            去重阈值，默认 0.9
  ALLOWED_PROJECTS           已被 projects 表取代（迁移 005）；只在该表还不存在时作为后备白名单
  VIEWER_TOKEN               可选；设了才开启只读网页 /viewer（至少 24 个字符）
  PORT                       可选，默认 8000
"""
import difflib
import json
import os
import re
import time
from datetime import UTC, datetime
from typing import Annotated, Literal

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from openai import OpenAI
from postgrest.exceptions import APIError
from pydantic import BeforeValidator
from supabase import create_client

import viewer

load_dotenv()  # 读取同目录下的 .env 文件（如果存在）

# ---------- 配置 ----------
db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
oai = OpenAI()
EMBED_MODEL = "text-embedding-3-small"
DEDUP_THRESHOLD = float(os.environ.get("DEDUP_THRESHOLD", "0.9"))
ALLOWED_PROJECTS = [p.strip() for p in os.environ.get("ALLOWED_PROJECTS", "").split(",") if p.strip()]
PROJECT_CACHE_SECONDS = 60
# 和 migrations/005 里的 projects_name_format 约束保持一致
PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")

# 记忆类型，和 migrations/002 里的 check 约束保持一致
Kind = Literal["state", "decision", "blocker", "context", "log", "session-summary"]

COLUMNS = "id, content, project, tags, source, kind, superseded_by, valid_until, created_at, updated_at"

mcp = FastMCP(
    "shared-memory",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
    streamable_http_path="/" + os.environ.get("MCP_SECRET_PATH", "mcp"),
    stateless_http=True,
)


# ---------- 辅助函数 ----------
def _coerce_list(v):
    """有些客户端会把数组参数当字符串发送，如 '["a","b"]'。统一转回真正的列表。"""
    if v is None or isinstance(v, list):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                pass
        return [x.strip() for x in s.split(",") if x.strip()]
    return v


TagList = Annotated[list[str] | None, BeforeValidator(_coerce_list)]
KindList = Annotated[list[Kind] | None, BeforeValidator(_coerce_list)]


def _now() -> str:
    # 用 Z 结尾而不是 +00:00：时间会拼进 PostgREST 的查询参数，"+" 在 URL 里可能被当成空格
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def embed(text: str) -> list[float]:
    return oai.embeddings.create(model=EMBED_MODEL, input=text).data[0].embedding


# 项目清单（名字 + 别名）：读 projects 表，短暂缓存，避免每次调用都多查一次数据库
_project_cache: dict = {"rows": None, "at": 0.0}
_MISSING_TABLE = {"PGRST205", "42P01"}  # projects 表不存在（迁移 005 还没运行）
_MISSING_COLUMN = {"PGRST204", "42703"}  # aliases 列不存在（迁移 006 还没运行）


def _projects(refresh: bool = False) -> list[dict] | None:
    """已登记的项目，每项 {"name", "aliases"}；projects 表不存在时返回 None。
    迁移 006 之前没有 aliases 列，此时 aliases 为空列表。"""
    if not refresh and _project_cache["rows"] is not None \
            and time.monotonic() - _project_cache["at"] < PROJECT_CACHE_SECONDS:
        return _project_cache["rows"]
    try:
        rows = db.table("projects").select("*").execute().data
    except APIError as e:
        if e.code in _MISSING_TABLE:
            return None
        raise
    rows = sorted(({"name": r["name"], "aliases": list(r.get("aliases") or [])} for r in rows),
                  key=lambda r: r["name"])
    _project_cache.update(rows=rows, at=time.monotonic())
    return rows


def _project_names(refresh: bool = False) -> list[str] | None:
    rows = _projects(refresh)
    return None if rows is None else [r["name"] for r in rows]


def _norm(s: str) -> str:
    """别名比较用：去首尾空白、大小写不敏感、内部空白压成一个。"""
    return " ".join(s.split()).casefold()


def _match_project(project: str, rows: list[dict]) -> str | None:
    q = _norm(project)
    for r in rows:
        if r["name"] == q:
            return r["name"]
    for r in rows:
        if any(_norm(a) == q for a in r["aliases"]):
            return r["name"]
    return None


def _describe(rows: list[dict]) -> str:
    return ", ".join(f"{r['name']} ({'; '.join(r['aliases'])})" if r["aliases"] else r["name"] for r in rows)


def _resolve_project(project: str) -> str:
    """把用户叫法（正式名或别名，如 '心力球'）解析成正式项目名；找不到就报错。"""
    project = project.strip()
    rows = _projects()
    if rows is None:  # 迁移 005 之前：退回环境变量白名单
        if ALLOWED_PROJECTS and project not in ALLOWED_PROJECTS:
            raise ValueError(f"未知项目 '{project}'，允许的项目：{', '.join(ALLOWED_PROJECTS)}")
        return project
    name = _match_project(project, rows)
    if name is None:
        rows = _projects(refresh=True)  # 可能是别的客户端刚登记的
        name = _match_project(project, rows)
    if name is None:
        raise ValueError(
            f"Unknown project '{project}'. Registered projects (aliases in brackets): {_describe(rows)}. "
            "If this belongs to one of them, use that name (add a tag for the sub-topic), "
            "and call add_alias if the user's wording is a new nickname for it. "
            "Only if the user wants a genuinely new project, register it with add_project."
        )
    return name


def _check_project(project: str) -> None:
    _resolve_project(project)


def _clean_aliases(aliases: list[str] | None, name: str) -> list[str]:
    """去空、去重（大小写不敏感）、去掉和正式名相同的。"""
    out: list[str] = []
    for a in aliases or []:
        a = " ".join(str(a).split())
        if a and _norm(a) != name and all(_norm(a) != _norm(o) for o in out):
            out.append(a)
    return out


def _alias_conflicts(aliases: list[str], rows: list[dict], own: str | None = None) -> dict[str, str]:
    """返回 {别名: 已占用它的项目}，只看别的项目。"""
    taken = {}
    for r in rows:
        if r["name"] == own:
            continue
        taken[r["name"]] = r["name"]
        for a in r["aliases"]:
            taken[_norm(a)] = r["name"]
    return {a: taken[_norm(a)] for a in aliases if _norm(a) in taken}


def _clean_source(source: str) -> str:
    s = source.strip().lower()
    if not s:
        raise ValueError("source 不能为空，例如 'claude-web'、'chatgpt'、'claude-code'")
    return s


def _row(r: dict) -> dict:
    out = {k: r[k] for k in (
        "id", "content", "project", "tags", "source", "kind",
        "created_at", "updated_at",
    ) if k in r}
    # 失效信息只在有值时返回，保持结果简洁
    for k in ("superseded_by", "valid_until"):
        if r.get(k):
            out[k] = r[k]
    for k in ("similarity", "keyword_score"):
        if r.get(k) is not None:
            out[k] = round(r[k], 3)
    return out


def _get(memory_id: str) -> dict | None:
    res = db.table("memories").select(COLUMNS).eq("id", memory_id).execute()
    return res.data[0] if res.data else None


def _nearest_active(vec: list[float], project: str, exclude: str | None = None) -> dict | None:
    """最相似的一条仍有效的记忆（去重用），可排除某个 id。"""
    res = db.rpc("match_memories", {
        "query_embedding": vec, "filter_project": project, "match_count": 2,
    }).execute()
    for r in res.data:
        if r["id"] != exclude:
            return _row(r)
    return None


# ---------- MCP 工具 ----------
@mcp.tool()
def save_memory(
    content: str,
    project: str,
    source: str,
    kind: Kind = "context",
    tags: TagList = None,
    supersedes: str | None = None,
) -> dict:
    """Save one durable fact, decision or status update to the shared memory.
    Use after the user makes a decision, changes a plan, or reaches a milestone.

    content: ONE self-contained statement, e.g. "portfolio: decided on Astro because content is static".
    project: a registered project name or one of its aliases, e.g. 'ai-pendant' or '心力球'
      (see list_projects; new projects only via add_project, new nicknames via add_alias).
    source: which assistant or program is writing, e.g. 'claude-web', 'chatgpt', 'claude-code'.
    kind: what sort of memory this is:
      - 'state': where something currently stands ("report generation: framework done, accuracy not yet studied")
      - 'decision': a choice and its reason
      - 'blocker': an open problem or something waiting on a decision
      - 'context': background fact (default)
      - 'log': a one-off progress note
      - 'session-summary': summary of one working session
    tags: optional finer categories, e.g. ["evaluation", "memobase"].
    supersedes: id of an older memory this one replaces (typically an outdated 'state' or a
      changed 'decision'). The old one is kept as history and stops appearing in normal search.
      Prefer this over update_memory when the situation has moved on, so the history stays visible.

    If a very similar active memory already exists, nothing is saved and the existing one is
    returned with status "duplicate": decide whether to supersede it, update it,
    or save again with clearer, more specific content.
    """
    project = _resolve_project(project)
    source = _clean_source(source)

    old = None
    if supersedes:
        old = _get(supersedes)
        if not old:
            return {"status": "not_found", "id": supersedes, "hint": "The memory to supersede does not exist."}
        if old["project"] != project:
            return {"status": "error",
                    "hint": f"Memory {supersedes} belongs to project '{old['project']}', not '{project}'."}
        if old.get("superseded_by"):
            return {"status": "error", "hint": f"Memory {supersedes} is already superseded by {old['superseded_by']}."}

    vec = embed(content)
    near = _nearest_active(vec, project, exclude=supersedes)
    if near and near["similarity"] >= DEDUP_THRESHOLD:
        return {"status": "duplicate", "existing": near,
                "hint": "Similar memory exists. Supersede it (save_memory with supersedes=<id>), "
                        "fix it with update_memory, or save again with more specific wording if it is truly new."}

    row = db.table("memories").insert({
        "content": content, "project": project, "source": source, "kind": kind,
        "tags": tags or [], "embedding": vec, "embedding_model": EMBED_MODEL,
    }).execute().data[0]

    result = {"status": "saved", "memory": _row(row)}
    if old:
        db.table("memories").update({
            "superseded_by": row["id"], "valid_until": _now(),
        }).eq("id", old["id"]).execute()
        result["superseded"] = old["id"]
    return result


@mcp.tool()
def update_memory(
    memory_id: str,
    new_content: str | None = None,
    kind: Kind | None = None,
    tags: TagList = None,
) -> dict:
    """Correct an existing memory in place: fix wrong or unclear wording, or reclassify its kind/tags.
    The previous wording is archived automatically.
    If the situation itself has moved on (new status, changed decision), use
    save_memory(..., supersedes=<id>) instead so the old state stays visible as history.
    Get the id from search_memory, recent_memories, or a duplicate result of save_memory."""
    changes: dict = {}
    if new_content is not None:
        changes.update({"content": new_content, "embedding": embed(new_content), "embedding_model": EMBED_MODEL})
    if kind is not None:
        changes["kind"] = kind
    if tags is not None:
        changes["tags"] = tags
    if not changes:
        return {"status": "error", "hint": "Nothing to update: pass new_content, kind or tags."}
    changes["updated_at"] = _now()

    row = db.table("memories").update(changes).eq("id", memory_id).execute().data
    if not row:
        return {"status": "not_found", "id": memory_id}
    return {"status": "updated", "memory": _row(row[0])}


@mcp.tool()
def retire_memory(memory_id: str) -> dict:
    """Mark a memory as no longer valid without replacing it, e.g. a blocker that got resolved
    or a state that simply no longer applies. It is kept as history and stops appearing in
    normal search. If something new replaces it, use save_memory(..., supersedes=<id>) instead."""
    row = (db.table("memories").update({"valid_until": _now()})
             .eq("id", memory_id).is_("valid_until", "null").execute().data)
    if row:
        return {"status": "retired", "memory": _row(row[0])}
    existing = _get(memory_id)
    if not existing:
        return {"status": "not_found", "id": memory_id}
    return {"status": "already_inactive", "memory": _row(existing)}


@mcp.tool()
def search_memory(
    query: str,
    project: str,
    k: int = 5,
    kinds: KindList = None,
    include_history: bool = False,
) -> list[dict]:
    """Search one project's memories by meaning AND by keywords (exact names like 'Memobase'
    or 'emotion2vec' match even when the meaning is distant). Call this BEFORE answering when
    the user mentions a project, a past decision, or asks about progress or "what did we decide".

    project: registered name or alias (e.g. '心力球' resolves to 'ai-pendant').
    kinds: optional filter, e.g. ["decision"] or ["state", "blocker"].
    include_history: also return superseded / retired memories (they carry superseded_by or
      valid_until). Use when the user asks how something evolved or what was decided before.
    """
    project = _resolve_project(project)
    res = db.rpc("search_memories", {
        "query_embedding": embed(query), "query_text": query,
        "filter_project": project, "match_count": k,
        "filter_kinds": kinds, "include_inactive": include_history,
    }).execute()
    return [_row(r) for r in res.data]


@mcp.tool()
def recent_memories(
    project: str,
    n: int = 10,
    kinds: KindList = None,
    include_history: bool = False,
) -> list[dict]:
    """List the n most recently saved or changed memories of a project, newest first
    (no ranking). Good for "what happened this week". Same filters as search_memory;
    project may be a name or an alias."""
    project = _resolve_project(project)
    q = db.table("memories").select(COLUMNS).eq("project", project)
    if kinds:
        q = q.in_("kind", kinds)
    if not include_history:
        q = q.is_("superseded_by", "null").or_(f"valid_until.is.null,valid_until.gt.{_now()}")
    res = q.order("updated_at", desc=True).limit(n).execute()
    return [_row(r) for r in res.data]


@mcp.tool()
def project_brief(project: str, max_decisions: int = 5, max_logs: int = 5) -> dict:
    """One-call overview of a project: where it stands, what is blocking, the latest decisions
    and the last session summary. Call this at the start of a conversation about a project,
    before answering "what did we decide" or "where were we", instead of several separate searches.
    Only active memories are included (superseded or retired ones are left out).

    project: registered name or alias.
    max_decisions / max_logs: how many of the newest decisions and logs to include.
    Returns: {"project", "state": [...], "blockers": [...], "decisions": [...],
              "last_session_summary": {...} | None, "logs": [...], "context": [...],
              "counts": {kind: n}, "last_updated": iso timestamp | None}.
    """
    project = _resolve_project(project)
    res = (db.table("memories").select(COLUMNS).eq("project", project)
             .is_("superseded_by", "null").or_(f"valid_until.is.null,valid_until.gt.{_now()}")
             .order("updated_at", desc=True).limit(300).execute())
    rows = [_row(r) for r in res.data]
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    summaries = by_kind.get("session-summary", [])
    return {
        "project": project,
        "state": by_kind.get("state", []),
        "blockers": by_kind.get("blocker", []),
        "decisions": by_kind.get("decision", [])[:max_decisions],
        "last_session_summary": summaries[0] if summaries else None,
        "logs": by_kind.get("log", [])[:max_logs],
        "context": by_kind.get("context", []),
        "counts": {k: len(v) for k, v in sorted(by_kind.items())},
        "last_updated": rows[0]["updated_at"] if rows else None,
    }


@mcp.tool()
def delete_memory(memory_id: str) -> dict:
    """Permanently delete one memory by id. Only for entries that were wrong from the start
    or saved by mistake. For things that are merely outdated, use retire_memory or supersede them."""
    row = db.table("memories").delete().eq("id", memory_id).execute().data
    if not row:
        return {"status": "not_found", "id": memory_id}
    return {"status": "deleted", "id": memory_id}


@mcp.tool()
def list_projects() -> list[dict]:
    """List the registered projects with their one-line descriptions and aliases (other names
    the user calls them, e.g. '心力球' for 'ai-pendant'). Use it when unsure which project a
    memory belongs to, or when a project name was rejected as unknown."""
    try:
        rows = db.table("projects").select("*").order("name").execute().data
    except APIError as e:
        if e.code in _MISSING_TABLE:
            return [{"name": n, "description": "", "aliases": []} for n in ALLOWED_PROJECTS]
        raise
    return [_project_row(r) for r in rows]


def _project_row(r: dict) -> dict:
    return {"name": r["name"], "description": r.get("description", ""),
            "aliases": list(r.get("aliases") or []),
            "created_by": r.get("created_by"), "created_at": r.get("created_at")}


@mcp.tool()
def add_project(name: str, description: str, source: str, aliases: TagList = None,
                force: bool = False) -> dict:
    """Register a new project so memories can be saved under it.
    Only call this when the user explicitly wants to start tracking a new project. A sub-topic of
    an existing project (e.g. one app inside 'portfolio') is NOT a new project: save it under the
    existing project and add a tag instead.

    name: lowercase letters, digits and hyphens, 2-40 characters, e.g. 'ai-pendant'.
    description: one sentence on what the project is, so assistants can tell where things belong.
    source: who is registering it, e.g. 'claude-web', 'chatgpt', 'claude-code'.
    aliases: other names the user uses for it, in any language, e.g. ["心力球", "AI挂件"].
      Any of them can be passed as `project` to the other tools later.
    force: register even though a similarly named project exists (only after the user confirmed
      they really are different projects).
    """
    name = name.strip().lower()
    if not PROJECT_NAME.match(name):
        return {"status": "error",
                "hint": "Name must be lowercase letters, digits and hyphens, 2-40 characters, "
                        "starting with a letter or digit, e.g. 'ai-pendant'."}
    description = description.strip()
    if not description:
        return {"status": "error", "hint": "Give a one-sentence description of the project."}
    source = _clean_source(source)

    names = _project_names(refresh=True)
    if names is None:
        return {"status": "error",
                "hint": "The projects table does not exist yet: run migrations/005_projects.sql in Supabase."}
    if name in names:
        return {"status": "exists", "name": name}
    similar = _similar_names(name, names)
    if similar and not force:
        return {"status": "similar_exists", "similar": similar,
                "hint": "A similarly named project exists. Ask the user whether to use it; "
                        "call again with force=true only if they confirm it is a different project."}
    aliases = _clean_aliases(aliases, name)
    conflicts = _alias_conflicts(aliases, _projects() or [])
    if conflicts:
        return {"status": "error", "conflicts": conflicts,
                "hint": "These aliases already point to other projects; drop or change them."}

    payload = {"name": name, "description": description, "created_by": source}
    if aliases:
        payload["aliases"] = aliases
    try:
        row = db.table("projects").insert(payload).execute().data[0]
    except APIError as e:
        if aliases and e.code in _MISSING_COLUMN:
            return {"status": "error",
                    "hint": "The aliases column does not exist yet: run migrations/006_project_aliases.sql "
                            "in Supabase, or register without aliases."}
        raise
    _project_cache["rows"] = None
    return {"status": "added", "project": _project_row(row)}


@mcp.tool()
def add_alias(project: str, alias: str) -> dict:
    """Record another name the user uses for a registered project, so that name works as
    `project` in the other tools from now on (for every assistant, not just this one).
    Call it when the user refers to a project by a nickname you had to resolve yourself,
    e.g. they say '心力球' and you worked out they mean 'ai-pendant'.

    project: the project's registered name or an existing alias.
    alias: the new name, kept as written (matching is case-insensitive).
    """
    name = _resolve_project(project)
    rows = _projects() or []
    current = next((r["aliases"] for r in rows if r["name"] == name), [])
    new = _clean_aliases([alias], name)
    if not new:
        return {"status": "error", "hint": "alias is empty or identical to the project name."}
    alias = new[0]
    if any(_norm(a) == _norm(alias) for a in current):
        return {"status": "exists", "project": name, "aliases": current}
    conflicts = _alias_conflicts([alias], rows, own=name)
    if conflicts:
        return {"status": "error", "conflicts": conflicts,
                "hint": "This name already points to another project."}
    try:
        row = (db.table("projects").update({"aliases": current + [alias]})
                 .eq("name", name).execute().data)
    except APIError as e:
        if e.code in _MISSING_COLUMN:
            return {"status": "error",
                    "hint": "The aliases column does not exist yet: run migrations/006_project_aliases.sql."}
        raise
    _project_cache["rows"] = None
    if not row:
        return {"status": "not_found", "project": name}
    return {"status": "added", "project": _project_row(row[0])}


def _similar_names(name: str, names: list[str]) -> list[str]:
    """Catch spelling variants such as 'scai-lab' vs 'scai' or 'shared-brian' vs 'shared-brain'."""
    squash = name.replace("-", "")
    out = []
    for n in names:
        m = n.replace("-", "")
        contains = min(len(squash), len(m)) >= 3 and (squash in m or m in squash)
        if contains or difflib.SequenceMatcher(None, squash, m).ratio() >= 0.8:
            out.append(n)
    return out


# 只读网页查看界面 /viewer（见 viewer.py；不设 VIEWER_TOKEN 就不开启）
viewer.register(mcp, db, embed)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
