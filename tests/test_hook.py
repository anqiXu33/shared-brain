"""Tests for the Claude Code Stop hook (hooks/session_summary.py), run as a subprocess like Claude Code does."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "session_summary.py"


@pytest.fixture
def repo(tmp_path):
    home = tmp_path / "home"
    proj = home / "code" / "site"
    (proj / "src").mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("本项目在共享记忆（shared-brain MCP）中的 project 名是 my-site。\n")
    return home, proj


def run(home, cwd, session="s1", env=None, **extra):
    e = {k: v for k, v in os.environ.items() if not k.startswith("SHARED_BRAIN")}
    e.update(HOME=str(home), SHARED_BRAIN_SUMMARY_EVERY_TURNS="3", SHARED_BRAIN_SUMMARY_MIN_TURNS="2",
             SHARED_BRAIN_SUMMARY_EVERY_MINUTES="45", **(env or {}))
    payload = {"session_id": session, "cwd": str(cwd), "hook_event_name": "Stop", **extra}
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                       text=True, env=e, timeout=20)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


def test_asks_every_n_turns_then_lets_claude_stop(repo):
    home, proj = repo
    assert run(home, proj) is None
    assert run(home, proj) is None
    out = run(home, proj)  # third reply
    assert out["decision"] == "block"
    assert 'project="my-site"' in out["reason"] and "session-summary" in out["reason"]
    assert run(home, proj, stop_hook_active=True) is None  # Claude stops again after saving the summary
    assert [run(home, proj) for _ in range(2)] == [None, None]
    assert run(home, proj)["decision"] == "block"  # counter restarted after the summary


def test_followup_stop_without_flag_also_ends_the_summary_step(repo):
    home, proj = repo
    for _ in range(2):
        run(home, proj)
    assert run(home, proj)["decision"] == "block"
    assert run(home, proj) is None


def test_finds_project_from_subdirectory(repo):
    home, proj = repo
    for _ in range(2):
        run(home, proj / "src")
    assert "my-site" in run(home, proj / "src")["reason"]


def test_english_line_and_env_override(tmp_path):
    home = tmp_path / "h"
    proj = home / "p"
    proj.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("The shared-memory project name is `portfolio`.\n")
    outs = [run(home, proj) for _ in range(3)]
    assert "portfolio" in outs[-1]["reason"]
    outs = [run(home, proj, session="s2", env={"SHARED_BRAIN_PROJECT": "scai"}) for _ in range(3)]
    assert 'project="scai"' in outs[-1]["reason"]


def test_silent_without_project(tmp_path):
    home = tmp_path / "h"
    (home / ".claude").mkdir(parents=True)
    # the global ~/.claude/CLAUDE.md names no single project and must be ignored
    (home / ".claude" / "CLAUDE.md").write_text("shared-brain project name is everything\n")
    plain = home / "plain"
    plain.mkdir()
    assert all(run(home, plain) is None for _ in range(5))


def test_respects_stop_hook_active_and_off_switch(repo):
    home, proj = repo
    assert all(run(home, proj, stop_hook_active=True) is None for _ in range(5))
    assert all(run(home, proj, session="s9", env={"SHARED_BRAIN_SUMMARY": "off"}) is None for _ in range(5))


def test_sessions_are_counted_separately(repo):
    home, proj = repo
    run(home, proj, session="a")
    run(home, proj, session="a")
    assert run(home, proj, session="b") is None
    assert run(home, proj, session="a")["decision"] == "block"


def test_time_based_trigger():
    sys.path.insert(0, str(HOOK.parent))
    import session_summary as h
    state = {}
    assert h.decide(state, 0, 10, 45, 3) is False
    assert h.decide(state, 60, 10, 45, 3) is False
    assert h.decide(state, 46 * 60, 10, 45, 3) is True  # 3 replies and 46 minutes
    assert h.decide(state, 47 * 60, 10, 45, 3) is False


def test_bad_input_never_breaks_the_session(repo):
    home, proj = repo
    p = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True,
                       env={**os.environ, "HOME": str(home)}, timeout=20)
    assert p.returncode == 0 and p.stdout == ""
