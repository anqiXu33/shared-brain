"""Round-trip check against the real, deployed server: remember, recall, correct, withdraw.

    python tests/roundtrip_live.py https://<your-service>.onrender.com/<MCP_SECRET_PATH>

or set SHARED_BRAIN_MCP_URL and run it without an argument. It can also be started from GitHub:
Actions > "Live round trip" > Run workflow (needs the repository secret SHARED_BRAIN_MCP_URL).

The unit and database tests check each piece on its own. This checks the whole path a real
assistant uses (MCP over HTTP -> server on Render -> OpenAI embedding -> Supabase search),
in the order memory is actually used:

    save a state -> find it by search and in project_brief -> saving it again is caught as a duplicate
    -> supersede it -> only the new one shows, the old one only with history -> correct the wording
    -> a blocker appears in the brief -> retire it -> it is gone -> clean up

Everything happens in its own project, `roundtrip-check` (registered on the first run, never used
for real memories), and every memory it creates is deleted at the end. A failed run leaves its
memories for you to look at; the next run deletes them first.

Uses only the standard library. Costs a few OpenAI embedding calls (a fraction of a cent).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

PROJECT = "roundtrip-check"
SOURCE = "roundtrip-check"
DESCRIPTION = ("Automated round-trip check (tests/roundtrip_live.py in the shared-brain repo). "
               "Not a real project: never save memories here.")


class Client:
    def __init__(self, url):
        self.url = url
        self.next_id = 0

    def rpc(self, method, params):
        self.next_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode()
        # the server answers as a server-sent event: "event: message\ndata: {...}"
        data = json.loads(next((line[5:] for line in text.splitlines() if line.startswith("data:")), text))
        if "error" in data:
            fail(f"{method} failed: {data['error']}")
        return data["result"]

    def connect(self, wait):
        """Render's free tier sleeps when idle and takes up to a minute to wake up."""
        deadline = time.time() + wait
        while True:
            try:
                return self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                               "clientInfo": {"name": "roundtrip-check", "version": "1"}})
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if time.time() > deadline:
                    raise
                time.sleep(3)

    def call(self, tool, **args):
        result = self.rpc("tools/call", {"name": tool, "arguments": args})
        if result.get("isError"):
            fail(f"{tool} returned an error: {result['content'][0]['text']}")
        # tools returning a list send it as structured output {"result": [...]}; dicts come as JSON text
        if "structuredContent" in result:
            return result["structuredContent"]["result"]
        return json.loads(result["content"][0]["text"])


def fail(message):
    sys.exit(f"FAILED: {message}")


def check(condition, step, detail=""):
    if not condition:
        fail(f"{step}{': ' + detail if detail else ''}")
    print(f"  ok  {step}")


def ids(rows):
    return [r["id"] for r in rows]


def clean(c):
    rows = c.call("recent_memories", project=PROJECT, n=100, include_history=True)
    for r in rows:
        c.call("delete_memory", memory_id=r["id"])
    return len(rows)


def run(url):
    c = Client(url)
    c.connect(wait=120)
    print("connected")

    added = c.call("add_project", name=PROJECT, description=DESCRIPTION, source=SOURCE)
    check(added["status"] in {"added", "exists"}, f"project {PROJECT} is registered", added["status"])
    leftovers = clean(c)
    if leftovers:
        print(f"  --  deleted {leftovers} memories left by an earlier run")

    tag = uuid.uuid4().hex[:8]
    first = f"Round-trip check {tag}: the parser module is finished and merged into main."
    second = f"Round-trip check {tag}: parser finished; the exporter is about half done."
    second_fixed = f"Round-trip check {tag}: parser finished; the exporter is about two thirds done."
    blocker = (f"Round-trip check {tag} blocker: waiting for the database administrator "
               "to grant write access to the staging schema.")

    print("remember and recall")
    a = c.call("save_memory", content=first, project=PROJECT, source=SOURCE, kind="state", tags=["roundtrip"])
    check(a["status"] == "saved", "save a state", a["status"])
    a_id = a["memory"]["id"]
    found = c.call("search_memory", query=f"Round-trip check {tag} parser", project=PROJECT)
    check(a_id in ids(found), "search finds it")
    brief = c.call("project_brief", project=PROJECT)
    check(ids(brief["state"]) == [a_id], "project_brief shows it as the current state")
    check(brief["state"][0]["days_since_update"] == 0 and "possibly_stale" not in brief["state"][0]
          and brief["notes"] == [], "a state saved just now is not flagged as stale")
    dup = c.call("save_memory", content=first, project=PROJECT, source=SOURCE, kind="state")
    check(dup["status"] == "duplicate" and dup["existing"]["id"] == a_id, "saving it again is caught as a duplicate")

    print("supersede")
    b = c.call("save_memory", content=second, project=PROJECT, source=SOURCE, kind="state", supersedes=a_id)
    check(b["status"] == "saved" and b.get("superseded") == a_id, "a new state supersedes the old one", b["status"])
    b_id = b["memory"]["id"]
    brief = c.call("project_brief", project=PROJECT)
    check(ids(brief["state"]) == [b_id], "project_brief shows only the new state")
    found = c.call("search_memory", query=f"Round-trip check {tag} parser", project=PROJECT)
    check(b_id in ids(found) and a_id not in ids(found), "search shows the new state and hides the old one")
    old = {r["id"]: r for r in c.call("search_memory", query=f"Round-trip check {tag} parser", project=PROJECT,
                                      include_history=True)}
    check(old.get(a_id, {}).get("superseded_by") == b_id, "with history on, the old state points to its successor")

    print("correct")
    fixed = c.call("update_memory", memory_id=b_id, new_content=second_fixed)
    check(fixed["status"] == "updated", "fix the wording in place")
    found = {r["id"]: r for r in c.call("search_memory", query=f"Round-trip check {tag} exporter", project=PROJECT)}
    check(found.get(b_id, {}).get("content") == second_fixed, "search returns the corrected wording")

    print("withdraw")
    blk = c.call("save_memory", content=blocker, project=PROJECT, source=SOURCE, kind="blocker")
    check(blk["status"] == "saved", "save a blocker", blk["status"])
    c_id = blk["memory"]["id"]
    check(ids(c.call("project_brief", project=PROJECT)["blockers"]) == [c_id], "project_brief lists the blocker")
    check(c.call("retire_memory", memory_id=c_id)["status"] == "retired", "retire it once it is resolved")
    check(c.call("project_brief", project=PROJECT)["blockers"] == [], "project_brief no longer lists it")
    check(c.call("retire_memory", memory_id=c_id)["status"] == "already_inactive", "retiring it twice is harmless")

    print("aliases and clean-up")
    check(len(c.call("recent_memories", project=PROJECT.upper())) == 1,
          "the project name works in any case, and only one memory is still active")
    clean(c)
    check(c.call("recent_memories", project=PROJECT, include_history=True) == [], "every test memory is deleted")
    print("round trip passed")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SHARED_BRAIN_MCP_URL", "")
    if not target:
        sys.exit(__doc__)
    run(target)
