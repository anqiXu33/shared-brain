"""Smoke test for a running server: MCP handshake plus tools/list over streamable HTTP.

    python tests/smoke_http.py http://localhost:8000/<MCP_SECRET_PATH>

Uses only the standard library. Retries for a while so it can run right after `docker run`.
Does not touch the database or OpenAI, so dummy credentials are enough.
"""
import json
import sys
import time
import urllib.error
import urllib.request

EXPECTED = {"save_memory", "update_memory", "retire_memory", "search_memory", "recent_memories", "delete_memory",
            "list_projects", "add_project", "add_alias"}


def rpc(url, method, params=None, id_=1):
    body = json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        text = resp.read().decode()
    # the server answers as a server-sent event: "event: message\ndata: {...}"
    data = next((line[5:] for line in text.splitlines() if line.startswith("data:")), text)
    return json.loads(data)


def main(url, wait=60):
    deadline = time.time() + wait
    while True:
        try:
            init = rpc(url, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                           "clientInfo": {"name": "smoke", "version": "0"}})
            break
        except (urllib.error.URLError, ConnectionError):
            if time.time() > deadline:
                raise
            time.sleep(1)
    print("server:", init["result"]["serverInfo"])
    tools = {t["name"] for t in rpc(url, "tools/list", id_=2)["result"]["tools"]}
    print("tools:", sorted(tools))
    if tools != EXPECTED:
        sys.exit(f"unexpected tools: {sorted(tools ^ EXPECTED)}")


if __name__ == "__main__":
    main(sys.argv[1])
