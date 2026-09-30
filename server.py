"""共享记忆 MCP server（阶段 1 版本）

架构：Claude 网页版 / ChatGPT / Claude Code --MCP--> 本 server --> Supabase (pgvector)
                                                          +--> OpenAI embedding

环境变量（写在 .env，或托管平台后台）：
  SUPABASE_URL               Supabase 项目地址
  SUPABASE_SERVICE_ROLE_KEY  service_role 密钥，只能放在 server 端
  OPENAI_API_KEY             OpenAI API 密钥（只用来算 embedding）
  MCP_SECRET_PATH            一串长随机字符，作为 URL 路径，相当于密码
  DEDUP_THRESHOLD            去重阈值，默认 0.9
  ALLOWED_PROJECTS           可选，逗号分隔的项目名清单；设置后只接受清单内的项目
  PORT                       可选，默认 8000
"""
import json
import os
from datetime import datetime, timezone
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from openai import OpenAI
from pydantic import BeforeValidator
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()  # 读取同目录下的 .env 文件（如果存在）

# ---------- 配置 ----------
db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
oai = OpenAI()
EMBED_MODEL = "text-embedding-3-small"
DEDUP_THRESHOLD = float(os.environ.get("DEDUP_THRESHOLD", "0.9"))
ALLOWED_PROJECTS = [p.strip() for p in os.environ.get("ALLOWED_PROJECTS", "").split(",") if p.strip()]

Source = Literal["claude-web", "chatgpt", "claude-code"]

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


def embed(text: str) -> list[float]:
    return oai.embeddings.create(model=EMBED_MODEL, input=text).data[0].embedding


def _check_project(project: str) -> None:
    if ALLOWED_PROJECTS and project not in ALLOWED_PROJECTS:
        raise ValueError(f"未知项目 '{project}'，允许的项目：{', '.join(ALLOWED_PROJECTS)}")


def _row(r: dict) -> dict:
    out = {k: r[k] for k in ("id", "content", "project", "tags", "source", "created_at", "updated_at") if k in r}
    if "similarity" in r:
        out["similarity"] = round(r["similarity"], 3)
    return out


def _similar(vec: list[float], project: str, k: int) -> list[dict]:
    res = db.rpc("match_memories", {
        "query_embedding": vec, "filter_project": project, "match_count": k,
    }).execute()
    return [_row(r) for r in res.data]


# ---------- MCP 工具 ----------
@mcp.tool()
def save_memory(content: str, project: str, source: Source, tags: TagList = None) -> dict:
    """Save one durable fact, decision or status update to the shared memory.
    Use after the user makes a decision, changes a plan, or reaches a milestone.

    content: ONE self-contained statement, e.g. "portfolio: decided on Astro because content is static".
    project: the project this belongs to (use the user's fixed project list).
    source: which assistant is writing: 'claude-web', 'chatgpt' or 'claude-code'.
    tags: optional finer categories, e.g. ["framework", "decision"].

    If a very similar memory already exists, nothing is saved and the existing one is
    returned with status "duplicate": decide whether to call update_memory on it,
    or call save_memory again with a clearer, more specific content.
    """
    _check_project(project)
    vec = embed(content)
    near = _similar(vec, project, 1)
    if near and near[0]["similarity"] >= DEDUP_THRESHOLD:
        return {"status": "duplicate", "existing": near[0],
                "hint": "Similar memory exists. Use update_memory(id, new_content) to revise it, "
                        "or save again with more specific wording if it is truly new."}
    row = db.table("memories").insert({
        "content": content, "project": project, "source": source,
        "tags": tags or [], "embedding": vec, "embedding_model": EMBED_MODEL,
    }).execute().data[0]
    return {"status": "saved", "memory": _row(row)}


@mcp.tool()
def update_memory(memory_id: str, new_content: str) -> dict:
    """Replace the content of an existing memory (e.g. a decision changed or a status moved on).
    Get the id from search_memory, recent_memories, or a duplicate result of save_memory."""
    row = db.table("memories").update({
        "content": new_content, "embedding": embed(new_content),
        "embedding_model": EMBED_MODEL,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", memory_id).execute().data
    if not row:
        return {"status": "not_found", "id": memory_id}
    return {"status": "updated", "memory": _row(row[0])}


@mcp.tool()
def search_memory(query: str, project: str, k: int = 5) -> list[dict]:
    """Semantic search within one project's memories. Call this BEFORE answering when the user
    mentions a project, a past decision, or asks about progress or "what did we decide"."""
    _check_project(project)
    return _similar(embed(query), project, k)


@mcp.tool()
def recent_memories(project: str, n: int = 10) -> list[dict]:
    """List the n most recently saved memories of a project, newest first (no semantic search)."""
    _check_project(project)
    res = (db.table("memories")
             .select("id, content, project, tags, source, created_at, updated_at")
             .eq("project", project)
             .order("created_at", desc=True).limit(n).execute())
    return [_row(r) for r in res.data]


@mcp.tool()
def delete_memory(memory_id: str) -> dict:
    """Delete one memory by id. Use only for wrong or clearly obsolete entries."""
    db.table("memories").delete().eq("id", memory_id).execute()
    return {"status": "deleted", "id": memory_id}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
