"""共享记忆 MCP server

架构：Claude 网页版 / ChatGPT / Claude Code / 其他 MCP 客户端 --MCP--> 本 server --> Supabase (pgvector + pg_trgm)
                                                                           +--> OpenAI embedding

需要数据库已运行 schema.sql、migrations/001、002；项目注册表需要 005。

环境变量（写在 .env，或托管平台后台）：
  SUPABASE_URL               Supabase 项目地址
  SUPABASE_SERVICE_ROLE_KEY  service_role 密钥，只能放在 server 端
  OPENAI_API_KEY             OpenAI API 密钥（只用来算 embedding）
  MCP_SECRET_PATH            一串长随机字符，作为 URL 路径，相当于密码
  DEDUP_THRESHOLD            去重阈值，默认 0.9
  ALLOWED_PROJECTS           已被 projects 表取代（迁移 005）；只在该表还不存在时作为后备白名单
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


# 项目名清单：读 projects 表，短暂缓存，避免每次调用都多查一次数据库
_project_cache: dict = {"names": None, "at": 0.0}
_MISSING_TABLE = {"PGRST205", "42P01"}  # projects 表不存在（迁移 005 还没运行）


def _project_names(refresh: bool = False) -> list[str] | None:
    """已登记的项目名；projects 表不存在时返回 None。"""
    if not refresh and _project_cache["names"] is not None \
            and time.monotonic() - _project_cache["at"] < PROJECT_CACHE_SECONDS:
        return _project_cache["names"]
    try:
        rows = db.table("projects").select("name").execute().data
    except APIError as e:
        if e.code in _MISSING_TABLE:
            return None
        raise
    _project_cache.update(names=sorted(r["name"] for r in rows), at=time.monotonic())
    return _project_cache["names"]


def _check_project(project: str) -> None:
    names = _project_names()
    if names is None:  # 迁移 005 之前：退回环境变量白名单
        if ALLOWED_PROJECTS and project not in ALLOWED_PROJECTS:
            raise ValueError(f"未知项目 '{project}'，允许的项目：{', '.join(ALLOWED_PROJECTS)}")
        return
    if project not in names:
        names = _project_names(refresh=True)  # 可能是别的客户端刚登记的
    if project not in names:
        raise ValueError(
            f"Unknown project '{project}'. Registered projects: {', '.join(names)}. "
            "If this belongs to one of them, use that name (add a tag for the sub-topic). "
            "Only if the user wants a genuinely new project, register it with add_project."
        )


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
    project: a registered project name (see list_projects; new ones only via add_project).
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
    _check_project(project)
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

    kinds: optional filter, e.g. ["decision"] or ["state", "blocker"].
    include_history: also return superseded / retired memories (they carry superseded_by or
      valid_until). Use when the user asks how something evolved or what was decided before.
    """
    _check_project(project)
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
    (no ranking). Good for "what happened this week". Same filters as search_memory."""
    _check_project(project)
    q = db.table("memories").select(COLUMNS).eq("project", project)
    if kinds:
        q = q.in_("kind", kinds)
    if not include_history:
        q = q.is_("superseded_by", "null").or_(f"valid_until.is.null,valid_until.gt.{_now()}")
    res = q.order("updated_at", desc=True).limit(n).execute()
    return [_row(r) for r in res.data]


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
    """List the registered projects with their one-line descriptions. Use it when unsure which
    project name a memory belongs to, or when a project name was rejected as unknown."""
    try:
        rows = (db.table("projects").select("name, description, created_by, created_at")
                  .order("name").execute().data)
    except APIError as e:
        if e.code in _MISSING_TABLE:
            return [{"name": n, "description": ""} for n in ALLOWED_PROJECTS]
        raise
    return rows


@mcp.tool()
def add_project(name: str, description: str, source: str, force: bool = False) -> dict:
    """Register a new project so memories can be saved under it.
    Only call this when the user explicitly wants to start tracking a new project. A sub-topic of
    an existing project (e.g. one app inside 'portfolio') is NOT a new project: save it under the
    existing project and add a tag instead.

    name: lowercase letters, digits and hyphens, 2-40 characters, e.g. 'ai-pendant'.
    description: one sentence on what the project is, so assistants can tell where things belong.
    source: who is registering it, e.g. 'claude-web', 'chatgpt', 'claude-code'.
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

    row = db.table("projects").insert({
        "name": name, "description": description, "created_by": source,
    }).execute().data[0]
    _project_cache["names"] = None
    return {"status": "added", "project": row}


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


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
