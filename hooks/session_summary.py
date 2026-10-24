#!/usr/bin/env python3
"""Claude Code Stop hook: keep a session summary in shared-brain without being asked.

Every time Claude finishes a reply, Claude Code runs this script. Most of the time it does
nothing. Every few turns (or after a long stretch of work) it tells Claude to keep going for
one more step: save or update this session's `session-summary` in shared memory, via the
shared-brain MCP tools Claude already has. The model that did the work writes the summary,
so no extra API key or LLM call is needed here.

Only runs in repos whose CLAUDE.md names a shared-brain project, e.g. a line such as
    本项目在共享记忆（shared-brain MCP）中的 project 名是 shared-brain。
    The shared-memory project name is my-site.
or when SHARED_BRAIN_PROJECT is set.

Install (once per machine):
    mkdir -p ~/.claude/hooks && cp hooks/session_summary.py ~/.claude/hooks/
and add to ~/.claude/settings.json:
    {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                    "command": "python3 ~/.claude/hooks/session_summary.py"}]}]}}

Settings (environment variables, all optional):
    SHARED_BRAIN_SUMMARY=off              turn the hook off
    SHARED_BRAIN_PROJECT=<name>           project name, instead of reading CLAUDE.md
    SHARED_BRAIN_SUMMARY_EVERY_TURNS=10   ask after this many replies since the last summary ...
    SHARED_BRAIN_SUMMARY_EVERY_MINUTES=45 ... or after this long, if at least
    SHARED_BRAIN_SUMMARY_MIN_TURNS=3      ... this many replies happened
    SHARED_BRAIN_HOOK_STATE=<dir>         where per-session counters live (default ~/.claude/shared-brain-hook)

Uses only the standard library. Any error means "do nothing": a broken hook must never get
in the way of a session.
"""
from __future__ import annotations  # macOS ships Python 3.9; keeps `str | None` working there

import json
import os
import re
import sys
import time
from pathlib import Path

PATTERNS = [
    re.compile(r"project\s*名(?:字|称)?\s*(?:是|为|[:：])\s*[`'\"“]?([a-z0-9][a-z0-9-]{1,39})"),
    re.compile(r"(?:shared[- ]?memory|shared-brain)\s+project\s+name\s+is\s+[`'\"]?([a-z0-9][a-z0-9-]{1,39})",
               re.IGNORECASE),
]
KEEP_STATE_DAYS = 14


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def find_project(cwd: str) -> str | None:
    """The shared-brain project named in the nearest CLAUDE.md at or above cwd (not ~/.claude)."""
    env = os.environ.get("SHARED_BRAIN_PROJECT", "").strip()
    if env:
        return env
    home = Path.home().resolve()
    d = Path(cwd or ".").resolve()
    while True:
        for f in (d / "CLAUDE.md", d / ".claude" / "CLAUDE.md", d / "CLAUDE.local.md"):
            if f.is_file() and f.parent != home / ".claude":
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for p in PATTERNS:
                    m = p.search(text)
                    if m:
                        return m.group(1).lower()
        if d == home or d.parent == d:
            return None
        d = d.parent


def reason(project: str, today: str) -> str:
    return (
        f"[shared-brain session-summary hook] Before stopping, update this session's summary in shared "
        f"memory (project \"{project}\"). Call save_memory with project=\"{project}\", "
        "kind=\"session-summary\", source=\"claude-code\". Content: one self-contained paragraph in the "
        f"language this project's memories use, starting with \"{project} 会话小结（{today}，Claude Code）：\", "
        "covering what was done, decisions made and why, what is still open, and the next step. "
        "If you already saved a session-summary earlier in this session, pass supersedes=<its id> so the "
        "session keeps one summary that grows. Also save any decision or change of state from this session "
        "that is not in shared memory yet (kind decision, or state with supersedes). If nothing worth "
        "recording happened since the last summary, save nothing. Never include secrets, keys or "
        "personal data. Afterwards stop; tell the user in at most one short line."
    )


def decide(state: dict, now: float, every_turns: int, every_minutes: int, min_turns: int) -> bool:
    """Count this reply and say whether to ask for a summary now. Mutates state."""
    state["turns"] = state.get("turns", 0) + 1
    state.setdefault("last_turn", 0)
    state.setdefault("last_time", now)
    if state.pop("pending", False):
        # we asked at the previous stop; this stop ends that summary step
        state["last_turn"], state["last_time"] = state["turns"], now
        return False
    since = state["turns"] - state["last_turn"]
    minutes = (now - state["last_time"]) / 60
    if since >= every_turns or (since >= min_turns and minutes >= every_minutes):
        state["pending"] = True
        return True
    return False


def _cleanup(state_dir: Path, now: float) -> None:
    for f in state_dir.glob("*.json"):
        try:
            if now - f.stat().st_mtime > KEEP_STATE_DAYS * 86400:
                f.unlink()
        except OSError:
            pass


def main(stdin: str) -> str:
    """Returns what to print: empty (let Claude stop) or a JSON decision to continue."""
    if os.environ.get("SHARED_BRAIN_SUMMARY", "").lower() in ("off", "0", "false", "no"):
        return ""
    data = json.loads(stdin or "{}")
    project = find_project(data.get("cwd") or os.getcwd())
    session = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("session_id", "")))[:100]
    if not project or not session:
        return ""

    state_dir = Path(os.environ.get("SHARED_BRAIN_HOOK_STATE", Path.home() / ".claude" / "shared-brain-hook"))
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"{session}.json"
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    now = time.time()
    if data.get("stop_hook_active"):
        # Claude is already continuing because of a Stop hook (ours or another one): never ask twice.
        # If it was ours, the summary step just ended, so restart the count from here.
        if state.pop("pending", False):
            state["last_turn"], state["last_time"] = state.get("turns", 0), now
            path.write_text(json.dumps(state))
        return ""
    ask = decide(state, now,
                 _int_env("SHARED_BRAIN_SUMMARY_EVERY_TURNS", 10),
                 _int_env("SHARED_BRAIN_SUMMARY_EVERY_MINUTES", 45),
                 _int_env("SHARED_BRAIN_SUMMARY_MIN_TURNS", 3))
    path.write_text(json.dumps(state))
    _cleanup(state_dir, now)
    if not ask:
        return ""
    return json.dumps({"decision": "block", "reason": reason(project, time.strftime("%Y-%m-%d"))},
                      ensure_ascii=False)


if __name__ == "__main__":
    try:
        out = main(sys.stdin.read())
    except Exception:
        out = ""
    if out:
        sys.stdout.buffer.write(out.encode("utf-8") + b"\n")
    sys.exit(0)
