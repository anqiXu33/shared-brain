"""Unit tests for server.py: argument coercion and the save/supersede/retire logic, with a fake database."""
import asyncio
import json
import uuid

import pytest

import server


class FakeResult:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class FakeQuery:
    """Just enough of the supabase-py query builder for server.py."""

    def __init__(self, db, op, payload=None):
        self.db, self.op, self.payload, self.filters = db, op, payload, []

    def select(self, _columns):
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def is_(self, col, _null):
        self.filters.append(lambda r: r.get(col) is None)
        return self

    def execute(self):
        rows = [r for r in self.db.rows if all(f(r) for f in self.filters)]
        if self.op == "insert":
            row = {"id": str(uuid.uuid4()), "superseded_by": None, "valid_until": None,
                   "created_at": "t", "updated_at": "t", **self.payload}
            self.db.rows.append(row)
            return FakeResult([row])
        if self.op == "update":
            for r in rows:
                r.update(self.payload)
            return FakeResult(rows)
        if self.op == "delete":
            self.db.rows = [r for r in self.db.rows if r not in rows]
            return FakeResult(rows)
        return FakeResult(rows)


class FakeTable:
    def __init__(self, db):
        self.db = db

    def select(self, columns):
        return FakeQuery(self.db, "select")

    def insert(self, payload):
        return FakeQuery(self.db, "insert", payload)

    def update(self, payload):
        return FakeQuery(self.db, "update", payload)

    def delete(self):
        return FakeQuery(self.db, "delete")


class FakeDB:
    def __init__(self):
        self.rows = []
        self.nearest = []  # what match_memories returns

    def table(self, _name):
        return FakeTable(self)

    def rpc(self, name, _params):
        assert name == "match_memories"
        return FakeResult(self.nearest)


@pytest.fixture
def fake(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(server, "db", db)
    monkeypatch.setattr(server, "embed", lambda text: [0.0] * 3)
    monkeypatch.setattr(server, "ALLOWED_PROJECTS", [])
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


def test_project_whitelist(monkeypatch):
    monkeypatch.setattr(server, "ALLOWED_PROJECTS", ["scai", "portfolio"])
    server._check_project("scai")
    with pytest.raises(ValueError, match="scai, portfolio"):
        server._check_project("Scai")


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
                     "recent_memories", "delete_memory"}


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
