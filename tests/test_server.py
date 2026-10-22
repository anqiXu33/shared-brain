"""Unit tests for server.py: argument coercion and the save/supersede/retire logic, with a fake database."""
import asyncio
import json
import uuid

import pytest
from postgrest.exceptions import APIError

import server


class FakeResult:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class FakeQuery:
    """Just enough of the supabase-py query builder for server.py."""

    def __init__(self, db, table, op, payload=None):
        self.db, self.table, self.op, self.payload, self.filters = db, table, op, payload, []

    def select(self, _columns):
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def is_(self, col, _null):
        self.filters.append(lambda r: r.get(col) is None)
        return self

    def order(self, col, desc=False):
        self.sort = (col, desc)
        return self

    def execute(self):
        if self.table == "projects" and self.db.no_projects_table:
            raise APIError({"code": "PGRST205", "message": "Could not find the table 'public.projects'"})
        table = self.db.tables[self.table]
        rows = [r for r in table if all(f(r) for f in self.filters)]
        if self.op == "insert":
            if self.table == "projects":
                row = {"created_at": "t", **self.payload}
            else:
                assert self.payload["project"] in {p["name"] for p in self.db.projects}, "foreign key"
                row = {"id": str(uuid.uuid4()), "superseded_by": None, "valid_until": None,
                       "created_at": "t", "updated_at": "t", **self.payload}
            table.append(row)
            return FakeResult([row])
        if self.op == "update":
            for r in rows:
                r.update(self.payload)
            return FakeResult(rows)
        if self.op == "delete":
            table[:] = [r for r in table if r not in rows]
            return FakeResult(rows)
        if hasattr(self, "sort"):
            rows.sort(key=lambda r: r[self.sort[0]], reverse=self.sort[1])
        return FakeResult(rows)


class FakeTable:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def select(self, columns):
        return FakeQuery(self.db, self.name, "select")

    def insert(self, payload):
        return FakeQuery(self.db, self.name, "insert", payload)

    def update(self, payload):
        return FakeQuery(self.db, self.name, "update", payload)

    def delete(self):
        return FakeQuery(self.db, self.name, "delete")


class FakeDB:
    def __init__(self):
        self.tables = {"memories": [], "projects": [{"name": n, "description": ""} for n in ("p", "other")]}
        self.nearest = []  # what match_memories returns
        self.no_projects_table = False
        self.project_reads = 0

    @property
    def rows(self):
        return self.tables["memories"]

    @property
    def projects(self):
        return self.tables["projects"]

    def table(self, name):
        if name == "projects":
            self.project_reads += 1
        return FakeTable(self, name)

    def rpc(self, name, _params):
        assert name == "match_memories"
        return FakeResult(self.nearest)


@pytest.fixture
def fake(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(server, "db", db)
    monkeypatch.setattr(server, "embed", lambda text: [0.0] * 3)
    monkeypatch.setattr(server, "ALLOWED_PROJECTS", [])
    monkeypatch.setattr(server, "_project_cache", {"names": None, "at": 0.0})
    return db


# ---------- argument handling ----------
@pytest.mark.parametrize("raw, expected", [
    (None, None),
    (["a", "b"], ["a", "b"]),
    ('["a", "b"]', ["a", "b"]),     # Claude Code sometimes sends lists as JSON strings
    ("a, b", ["a", "b"]),
    ("[not json", ["[not json"]),
])
def test_coerce_list(raw, expected):
    assert server._coerce_list(raw) == expected


def test_clean_source():
    assert server._clean_source("  Claude-Web ") == "claude-web"
    with pytest.raises(ValueError):
        server._clean_source("   ")


def test_env_whitelist_is_fallback_before_migration_005(fake, monkeypatch):
    fake.no_projects_table = True
    monkeypatch.setattr(server, "ALLOWED_PROJECTS", ["scai", "portfolio"])
    server._check_project("scai")
    with pytest.raises(ValueError, match="scai, portfolio"):
        server._check_project("Scai")
    assert [p["name"] for p in server.list_projects()] == ["scai", "portfolio"]
    assert server.add_project("new-one", "x", "test")["status"] == "error"


def test_row_rounds_scores_and_hides_empty_validity():
    out = server._row({"id": "1", "content": "c", "similarity": 0.123456, "keyword_score": None,
                       "superseded_by": None, "valid_until": None, "embedding": [1.0]})
    assert out == {"id": "1", "content": "c", "similarity": 0.123}


# ---------- save / supersede / retire ----------
def test_save_rejects_near_duplicate(fake):
    fake.nearest = [{"id": "x", "content": "same", "similarity": 0.95}]
    res = server.save_memory("same thing", "p", "test")
    assert res["status"] == "duplicate" and res["existing"]["id"] == "x"
    assert fake.rows == []


def call_tool(name, args):
    """Go through the MCP layer, so argument validation and coercion run as they do for real clients."""
    content = asyncio.run(server.mcp.call_tool(name, args))
    if isinstance(content, tuple):  # newer mcp versions also return structured output
        content = content[0]
    return json.loads(content[0].text)


def test_save_below_threshold_is_saved(fake):
    fake.nearest = [{"id": "x", "content": "other", "similarity": 0.5}]
    res = call_tool("save_memory", {"content": "new thing", "project": "p", "source": "test",
                                    "kind": "decision", "tags": '["a", "b"]'})
    assert res["status"] == "saved"
    assert fake.rows[0]["kind"] == "decision" and fake.rows[0]["tags"] == ["a", "b"]


def test_mcp_layer_rejects_unknown_kind(fake):
    with pytest.raises(Exception, match="kind"):
        call_tool("save_memory", {"content": "x", "project": "p", "source": "test", "kind": "opinion"})
    assert fake.rows == []


def test_all_tools_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {"save_memory", "update_memory", "retire_memory", "search_memory",
                     "recent_memories", "delete_memory", "list_projects", "add_project"}


def test_supersede_marks_old_row_and_ignores_it_for_dedup(fake):
    old = server.save_memory("status: step one", "p", "test", kind="state")["memory"]["id"]
    # the most similar active memory is the one being replaced: that must not block the save
    fake.nearest = [{"id": old, "content": "status: step one", "similarity": 0.97}]
    res = server.save_memory("status: step two", "p", "test", kind="state", supersedes=old)
    assert res["status"] == "saved" and res["superseded"] == old
    old_row = next(r for r in fake.rows if r["id"] == old)
    assert old_row["superseded_by"] == res["memory"]["id"] and old_row["valid_until"]


@pytest.mark.parametrize("setup, status", [
    (lambda db: None, "not_found"),
    (lambda db: db.rows.append({"id": "o", "project": "other", "superseded_by": None}), "error"),
    (lambda db: db.rows.append({"id": "o", "project": "p", "superseded_by": "z"}), "error"),
])
def test_supersede_guards(fake, setup, status):
    setup(fake)
    res = server.save_memory("x", "p", "test", supersedes="o")
    assert res["status"] == status
    assert not any(r.get("content") == "x" for r in fake.rows)


def test_retire_is_idempotent(fake):
    mid = server.save_memory("blocker: no access", "p", "test", kind="blocker")["memory"]["id"]
    assert server.retire_memory(mid)["status"] == "retired"
    assert server.retire_memory(mid)["status"] == "already_inactive"
    assert server.retire_memory("missing")["status"] == "not_found"


def test_update_requires_a_change(fake):
    assert server.update_memory("any")["status"] == "error"


# ---------- project registry ----------
def test_unknown_project_is_rejected_with_the_list(fake):
    with pytest.raises(ValueError, match="Registered projects: other, p"):
        server.save_memory("x", "wikilens", "test")
    assert fake.rows == []


def test_project_names_are_cached(fake):
    server._check_project("p")
    server._check_project("other")
    server._check_project("p")
    assert fake.project_reads == 1


def test_project_added_elsewhere_is_seen_without_waiting_for_cache(fake):
    server._check_project("p")  # cache now holds p, other
    fake.projects.append({"name": "fresh", "description": ""})  # e.g. ChatGPT registered it
    server._check_project("fresh")


def test_add_project_then_save(fake):
    res = call_tool("add_project", {"name": " AI-Pendant ", "description": "wearable recorder",
                                    "source": "claude-web"})
    assert res["status"] == "added" and res["project"]["name"] == "ai-pendant"
    assert res["project"]["created_by"] == "claude-web"
    assert server.save_memory("first note", "ai-pendant", "test")["status"] == "saved"
    assert [p["name"] for p in server.list_projects()] == ["ai-pendant", "other", "p"]


@pytest.mark.parametrize("name", ["a", "has space", "under_score", "-leading", "x" * 41, "naïve"])
def test_add_project_rejects_bad_names(fake, name):
    assert server.add_project(name, "d", "test")["status"] == "error"
    assert len(fake.projects) == 2


def test_add_project_requires_description(fake):
    assert server.add_project("newproj", "  ", "test")["status"] == "error"


def test_add_project_existing(fake):
    assert server.add_project("other", "d", "test")["status"] == "exists"
    assert len(fake.projects) == 2


@pytest.mark.parametrize("existing, new", [
    ("scai", "scai-lab"), ("shared-brain", "shared-brian"), ("shared-brain", "sharedbrain"),
    ("portfolio", "portfolio-v2"),
])
def test_add_project_warns_on_similar_names(fake, existing, new):
    fake.projects.append({"name": existing, "description": ""})
    res = server.add_project(new, "d", "test")
    assert res["status"] == "similar_exists" and existing in res["similar"]
    assert server.add_project(new, "d", "test", force=True)["status"] == "added"


@pytest.mark.parametrize("new", ["ai-pendant", "wikilens", "freelance"])
def test_add_project_unrelated_names_pass(fake, new):
    for n in ("scai", "shared-brain", "portfolio"):
        fake.projects.append({"name": n, "description": ""})
    assert server.add_project(new, "d", "test")["status"] == "added"
