"""Tests for the read-only web viewer (viewer.py) with a fake database."""
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import viewer

TOKEN = "t" * 32
MID = "11111111-1111-4111-8111-111111111111"
NEW = "22222222-2222-4222-8222-222222222222"


class Q:
    """Minimal supabase-py query builder; records every call so tests can assert reads only."""

    def __init__(self, db, table):
        self.db, self.table, self.filters, self.window = db, table, [], None
        db.calls.append((table, "select"))

    def select(self, _cols):
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, start, end):
        self.window = (start, end)
        return self

    def execute(self):
        rows = [dict(r) for r in self.db.tables[self.table] if all(f(r) for f in self.filters)]
        if self.window:
            rows = rows[self.window[0]:self.window[1] + 1][:self.db.max_rows]
        return type("R", (), {"data": rows})()

    def __getattr__(self, name):  # insert / update / delete / upsert ...
        raise AssertionError(f"viewer must not call {name}")


class FakeDB:
    def __init__(self):
        self.calls = []
        self.max_rows = 1000  # Supabase's "Max rows" setting
        self.tables = {
            "projects": [{"name": "demo", "description": "A demo", "aliases": ["演示"]}],
            "memories": [
                {"id": MID, "content": "old state", "project": "demo", "tags": [], "source": "chatgpt",
                 "kind": "state", "superseded_by": NEW, "valid_until": "2026-10-01T00:00:00Z",
                 "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z"},
                {"id": NEW, "content": "new state", "project": "demo", "tags": ["x"], "source": "claude-web",
                 "kind": "state", "superseded_by": None, "valid_until": None,
                 "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z"},
            ],
            "memory_history": [
                {"id": "h1", "memory_id": NEW, "old_content": "new stat", "old_source": "claude-web",
                 "replaced_at": "2026-10-02T00:00:00Z"},
            ],
        }

    def table(self, name):
        return Q(self, name)

    def rpc(self, name, params):
        assert name == "search_memories"
        self.calls.append(("rpc", name))
        self.last_rpc = params
        return type("R", (), {"execute": lambda _self: type("D", (), {"data": [
            {"id": NEW, "similarity": 0.81, "keyword_score": 0.5}]})()})()


def client(db=None, token=TOKEN, embed=lambda _q: [0.0] * 1536):
    db = db or FakeDB()
    app = Starlette(routes=viewer.make_routes(db, token, embed))
    return TestClient(app, base_url="https://testserver"), db


def login(c):
    r = c.post("/viewer/login", data={"token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303
    return r


def test_disabled_without_token():
    c, _ = client(token=None)
    assert c.get("/viewer").status_code == 404
    assert c.get("/viewer/api/projects").status_code == 404


def test_short_token_keeps_viewer_disabled():
    c, _ = client(token="short")
    assert c.get("/viewer").status_code == 404
    assert c.post("/viewer/login", data={"token": "short"}).status_code == 404


def test_login_page_then_api_needs_cookie():
    c, _ = client()
    r = c.get("/viewer")
    assert r.status_code == 200 and 'name="token"' in r.text
    assert c.get("/viewer/api/projects").status_code == 401


def test_wrong_token_rejected():
    c, _ = client()
    r = c.post("/viewer/login", data={"token": "nope"})
    assert r.status_code == 401
    assert viewer.COOKIE not in c.cookies


def test_login_sets_hardened_cookie_without_the_token():
    c, _ = client()
    cookie = login(c).headers["set-cookie"]
    assert TOKEN not in cookie
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/viewer"):
        assert flag.lower() in cookie.lower()
    r = c.get("/viewer")
    assert "全部记忆" in r.text
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["cache-control"] == "no-store"


def test_nonce_is_fresh_per_response():
    c, _ = client()
    login(c)
    a = c.get("/viewer").headers["content-security-policy"]
    b = c.get("/viewer").headers["content-security-policy"]
    assert a != b


def test_rotating_token_logs_everyone_out():
    db = FakeDB()
    c, _ = client(db)
    login(c)
    other = TestClient(Starlette(routes=viewer.make_routes(db, "u" * 32)), base_url="https://testserver")
    other.cookies.update(c.cookies)
    assert other.get("/viewer/api/projects").status_code == 401


def test_projects_and_project_memories_with_version_counts():
    c, _ = client()
    login(c)
    assert c.get("/viewer/api/projects").json() == [{"name": "demo", "description": "A demo", "aliases": ["演示"]}]
    data = c.get("/viewer/api/projects/demo").json()
    by_id = {m["id"]: m for m in data["memories"]}
    assert set(by_id) == {MID, NEW}
    assert by_id[NEW]["versions"] == 1 and by_id[MID]["versions"] == 0
    assert "embedding" not in by_id[NEW]


def test_pagination_reads_past_1000_rows(monkeypatch):
    monkeypatch.setattr(viewer, "PAGE", 2)
    db = FakeDB()
    for i in range(5):
        db.tables["memories"].append({**db.tables["memories"][1], "id": f"x{i}"})
    c, _ = client(db)
    login(c)
    assert len(c.get("/viewer/api/projects/demo").json()["memories"]) == 7


def test_pagination_survives_a_lower_max_rows_setting():
    db = FakeDB()
    db.max_rows = 1  # server returns fewer rows than asked for
    for i in range(3):
        db.tables["memory_history"].append({**db.tables["memory_history"][0], "id": f"h{i + 2}"})
    c, _ = client(db)
    login(c)
    data = c.get("/viewer/api/projects/demo").json()["memories"]
    assert len(data) == 2
    assert {m["id"]: m["versions"] for m in data}[NEW] == 4


def test_odd_cookie_is_rejected_not_crashing():
    c, _ = client()
    r = c.get("/viewer/api/projects", headers={"cookie": 'sb_viewer="\\303\\251"'})
    assert r.status_code == 401
    r = c.get("/viewer", headers={"cookie": "sb_viewer=é".encode("latin-1")})
    assert r.status_code == 200 and 'name="token"' in r.text


def test_logout_hidden_when_disabled():
    c, _ = client(token=None)
    assert c.post("/viewer/logout", follow_redirects=False).status_code == 404


def test_hsts_on_pages():
    c, _ = client()
    assert "max-age" in c.get("/viewer").headers["strict-transport-security"]


def test_history_endpoint_validates_id():
    c, _ = client()
    login(c)
    assert c.get("/viewer/api/memories/not-a-uuid/history").status_code == 400
    rows = c.get(f"/viewer/api/memories/{NEW}/history").json()
    assert rows[0]["old_content"] == "new stat"


def test_search_uses_hybrid_function():
    c, db = client()
    login(c)
    r = c.get("/viewer/api/search", params={"project": "demo", "q": "状态", "history": "1"})
    assert r.json() == [{"id": NEW, "similarity": 0.81, "keyword_score": 0.5}]
    assert db.last_rpc["include_inactive"] is True and db.last_rpc["query_text"] == "状态"
    assert c.get("/viewer/api/search", params={"project": "demo"}).status_code == 400


def test_database_errors_do_not_leak():
    db = FakeDB()

    def boom(_name):
        raise RuntimeError("secret connection string")
    db.table = boom
    c, _ = client(db)
    login(c)
    r = c.get("/viewer/api/projects")
    assert r.status_code == 502 and "secret" not in r.text


def test_logout_clears_cookie():
    c, _ = client()
    login(c)
    c.post("/viewer/logout", follow_redirects=False)
    assert c.get("/viewer/api/projects").status_code == 401


@pytest.mark.parametrize("path", ["/viewer/api/projects", "/viewer/api/projects/demo",
                                  f"/viewer/api/memories/{NEW}/history"])
def test_only_reads(path):
    c, db = client()
    login(c)
    c.get(path)
    assert all(op in ("select", "search_memories") for _t, op in db.calls)


def test_server_mounts_viewer_routes():
    import server
    routes = {getattr(r, "path", None): r for r in server.mcp.streamable_http_app().routes}
    assert {"/viewer", "/viewer/login", "/viewer/api/projects"} <= set(routes)
    assert "POST" in routes["/viewer/login"].methods
