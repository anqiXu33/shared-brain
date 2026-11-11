"""Runs tests/roundtrip_live.py against the real server app over HTTP, with a fake database.

This keeps the live check honest in CI: if a tool's arguments or the shape of its result change,
this fails here instead of on the deployed server. The fake stands in for Supabase only; how the
real SQL search ranks and filters is covered by test_sql.py.
"""
import socket
import threading
import time

import pytest
import roundtrip_live
import test_server
import uvicorn

import server


class RoundtripDB(test_server.FakeDB):
    """FakeDB plus the two SQL functions the round trip needs."""

    def __init__(self):
        super().__init__()
        self.tables["projects"] = []  # the script registers roundtrip-check itself
        self.last_embedded = None

    def active(self, r):
        return r["superseded_by"] is None and (r["valid_until"] is None or r["valid_until"] > server._now())

    def rpc(self, name, params):
        rows = [r for r in self.rows if r["project"] == params["filter_project"]]
        if name == "match_memories":  # dedup: identical text counts as a duplicate, anything else does not
            out = [dict(r, similarity=1.0 if r["content"] == self.last_embedded else 0.3)
                   for r in rows if self.active(r)]
            return test_server.FakeResult(sorted(out, key=lambda r: -r["similarity"]))
        assert name == "search_memories"
        words = params["query_text"].lower().split()
        out = [dict(r, similarity=0.5, keyword_score=sum(w in r["content"].lower() for w in words) / len(words))
               for r in rows if params["include_inactive"] or self.active(r)]
        return test_server.FakeResult([r for r in out if r["keyword_score"] > 0])


@pytest.fixture(scope="module")
def http_url():
    """The server app on a free local port. Started once: FastMCP's HTTP app can only run once per process."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(server.mcp.streamable_http_app(), port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started:
        assert time.time() < deadline, "test server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True
    thread.join()


@pytest.fixture
def live_url(http_url, monkeypatch):
    db = RoundtripDB()

    def embed(text):
        db.last_embedded = text
        return [0.0]

    monkeypatch.setattr(server, "db", db)
    monkeypatch.setattr(server, "embed", embed)
    monkeypatch.setattr(server, "_project_cache", {"rows": None, "at": 0.0})
    return http_url, db


def test_roundtrip_script_passes_and_cleans_up(live_url, capsys):
    url, db = live_url
    roundtrip_live.run(url)
    assert db.rows == [] and [p["name"] for p in db.projects] == ["roundtrip-check"]
    roundtrip_live.run(url)  # second run: project already exists
    assert "round trip passed" in capsys.readouterr().out


def test_roundtrip_script_reports_a_broken_step(live_url, monkeypatch):
    """If supersede stopped hiding the old state, the script must fail, not print ok."""
    url, db = live_url
    monkeypatch.setattr(db, "active", lambda r: True)
    with pytest.raises(SystemExit, match="FAILED: search shows the new state and hides the old one"):
        roundtrip_live.run(url)
