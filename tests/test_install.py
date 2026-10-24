"""Tests for hooks/install.py: merging into an existing ~/.claude/settings.json safely."""
import json
import os
import subprocess
import sys
from pathlib import Path

INSTALL = Path(__file__).resolve().parent.parent / "hooks" / "install.py"


def run(home, *args, stdin=None):
    env = {**os.environ, "HOME": str(home)}
    env.pop("CLAUDE_CONFIG_DIR", None)
    cmd = [sys.executable, "-", *args] if stdin else [sys.executable, str(INSTALL), *args]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env, timeout=30)


def settings(home):
    return json.loads((home / ".claude" / "settings.json").read_text())


def ours(data):
    return [h for g in data["hooks"]["Stop"] for h in g["hooks"] if "session_summary.py" in h["command"]]


def test_fresh_install(tmp_path):
    p = run(tmp_path)
    assert p.returncode == 0, p.stderr
    assert (tmp_path / ".claude" / "hooks" / "session_summary.py").is_file()
    [hook] = ours(settings(tmp_path))
    assert hook["type"] == "command"
    interpreter = hook["command"].split('"')[1]
    assert Path(interpreter).is_file()  # an absolute interpreter path that exists, never a venv-only one


def test_keeps_existing_settings_and_is_idempotent(tmp_path):
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    existing = {"model": "opus", "permissions": {"allow": ["Bash(ls)"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "~/.claude/stop-hook-git-check.sh"}]}],
                          "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}]}}
    (cfg / "settings.json").write_text(json.dumps(existing))
    for _ in range(3):
        assert run(tmp_path).returncode == 0
    data = settings(tmp_path)
    assert data["model"] == "opus" and data["permissions"] == existing["permissions"]
    assert data["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
    commands = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert "~/.claude/stop-hook-git-check.sh" in commands
    assert len(ours(data)) == 1
    assert list(cfg.glob("settings.json.bak-*"))


def test_replaces_a_hand_written_entry(tmp_path):
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    (cfg / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "python3 ~/.claude/hooks/session_summary.py"}]}]}}))
    run(tmp_path)
    [hook] = ours(settings(tmp_path))
    assert hook["command"] != "python3 ~/.claude/hooks/session_summary.py"


def test_invalid_json_is_left_alone(tmp_path):
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    (cfg / "settings.json").write_text('{"model": "opus",}')
    p = run(tmp_path)
    assert p.returncode != 0 and "not valid JSON" in p.stderr
    assert (cfg / "settings.json").read_text() == '{"model": "opus",}'


def test_uninstall_restores(tmp_path):
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    (cfg / "settings.json").write_text(json.dumps({"model": "opus"}))
    run(tmp_path)
    assert run(tmp_path, "--uninstall").returncode == 0
    assert settings(tmp_path) == {"model": "opus"}
    assert not (cfg / "hooks" / "session_summary.py").exists()


def test_installed_hook_runs(tmp_path):
    run(tmp_path)
    hook = tmp_path / ".claude" / "hooks" / "session_summary.py"
    p = subprocess.run([sys.executable, str(hook)], input="{}", capture_output=True, text=True,
                       env={**os.environ, "HOME": str(tmp_path)}, timeout=20)
    assert p.returncode == 0 and p.stdout == ""
