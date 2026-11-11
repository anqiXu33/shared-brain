"""A private copy of the shared memory for one scenario run.

Loads a scenario's projects and memories into memdb.MemDB, points server.py at it, and lets the
model call the real MCP tools (through FastMCP's own argument validation, as a real client would).
Each call returns a record of what was asked and answered, which the runner keeps for grading.
"""
import asyncio
import json
from datetime import UTC, datetime, timedelta

import memdb
from mcp.server.fastmcp.exceptions import ToolError

import server


def _ts(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def tool_specs() -> list[dict]:
    """The server's tools exactly as an MCP client sees them: name, description, JSON schema."""
    return [{"name": t.name, "description": t.description, "schema": t.inputSchema}
            for t in asyncio.run(server.mcp.list_tools())]


class Sandbox:
    def __init__(self, world: dict):
        """world: {"projects": [{name, description, aliases}], "memories": [{key, project, kind, content,
        days_ago, tags?}]}. Seeded memories are addressed by their key in checks."""
        self.db = memdb.MemDB(server._now)
        for p in world["projects"]:
            self.db.tables["projects"].append({"name": p["name"], "description": p["description"],
                                               "aliases": list(p.get("aliases", [])),
                                               "created_by": "seed", "created_at": _ts(90)})
        self.seed_ids: dict[str, str] = {}
        for m in world["memories"]:
            ts = _ts(m["days_ago"])
            row = self.db.table("memories").insert({
                "content": m["content"], "project": m["project"], "kind": m["kind"], "source": "seed",
                "tags": m.get("tags", []), "embedding": m["content"], "embedding_model": "sandbox",
            }).execute().data[0]
            stored = next(r for r in self.db.tables["memories"] if r["id"] == row["id"])
            stored["created_at"] = stored["updated_at"] = ts
            self.seed_ids[m["key"]] = row["id"]

    def __enter__(self):
        self._saved = (server.db, server.embed, server._project_cache)
        server.db, server.embed = self.db, memdb.embed
        server._project_cache = {"rows": None, "at": 0.0}
        return self

    def __exit__(self, *exc):
        server.db, server.embed, server._project_cache = self._saved

    def call(self, name: str, args: dict) -> dict:
        """Run one tool. Returns {"name", "input", "output"} or {"name", "input", "error"};
        `text(record)` is what a client hands back to the model."""
        try:
            content = asyncio.run(server.mcp.call_tool(name, args))
        except ToolError as e:  # what an MCP client receives as an error result
            return {"name": name, "input": args, "error": str(e)}
        if isinstance(content, tuple):  # list results also come as structured output {"result": [...]}
            return {"name": name, "input": args, "output": content[1]["result"]}
        return {"name": name, "input": args, "output": json.loads(content[0].text)}

    @staticmethod
    def text(record: dict) -> str:
        if "error" in record:
            return json.dumps({"error": record["error"]}, ensure_ascii=False)
        return json.dumps(record["output"], ensure_ascii=False)

    def memory(self, key: str) -> dict | None:
        """Final state of a seeded memory (None if it was deleted)."""
        mid = self.seed_ids[key]
        return next((r for r in self.db.tables["memories"] if r["id"] == mid), None)

    def project_of(self, name: str) -> str | None:
        """Which registered project a name or alias refers to, as the server would resolve it."""
        return server._match_project(name, [{"name": p["name"], "aliases": p["aliases"]}
                                            for p in self.db.tables["projects"]])
