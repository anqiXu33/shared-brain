#!/usr/bin/env python3
"""Install (or remove) the shared-brain session-summary hook for Claude Code, in one command.

From a clone of the repo:
    python3 hooks/install.py
On a machine without the repo:
    curl -fsSL https://raw.githubusercontent.com/anqiXu33/shared-brain/main/hooks/install.py | python3 -
Remove it again:
    python3 hooks/install.py --uninstall

What it does:
  1. copies session_summary.py to ~/.claude/hooks/
  2. adds a Stop hook for it to ~/.claude/settings.json, keeping everything else in that file
     (a backup is saved next to it first). Running it again just updates; it never adds the
     hook twice.

Uses only the standard library and works on Python 3.7+.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

RAW_URL = "https://raw.githubusercontent.com/anqiXu33/shared-brain/main/hooks/session_summary.py"
MARKER = "session_summary.py"


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def hook_source() -> bytes:
    """The hook script: next to this file when run from the repo, otherwise from GitHub."""
    me = globals().get("__file__", "")
    here = Path(me).resolve().parent / MARKER if me.endswith(".py") else None  # "<stdin>" when piped
    if here and here.is_file():
        return here.read_bytes()
    print(f"Downloading the hook from {RAW_URL}")
    with urllib.request.urlopen(RAW_URL, timeout=30) as resp:
        return resp.read()


def load_settings(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        sys.exit(f"{path} is not valid JSON ({e}). Fix it first; nothing was changed.")
    if not isinstance(data, dict):
        sys.exit(f"{path} does not contain a JSON object; nothing was changed.")
    return data


def without_our_hook(stop_groups: list) -> list:
    """Stop hook groups with every entry that runs session_summary.py removed."""
    out = []
    for group in stop_groups:
        if not isinstance(group, dict):
            out.append(group)
            continue
        hooks = [h for h in group.get("hooks", []) if MARKER not in str(h.get("command", ""))]
        if hooks:
            out.append({**group, "hooks": hooks})
        elif not group.get("hooks"):
            out.append(group)  # not ours, leave untouched
    return out


def save_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"Backed up your settings to {backup}")
    tmp = path.with_name(".settings.json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic: Claude Code never sees a half-written file


def python() -> str:
    """The interpreter running this installer: it certainly exists, unlike a bare "python3" on some
    machines. Inside a virtualenv, use the Python the venv was made from, so deleting the venv
    later does not break the hook."""
    if sys.prefix != sys.base_prefix:
        base = getattr(sys, "_base_executable", "")
        if base and Path(base).is_file():
            return base
    return sys.executable


def install() -> None:
    base = claude_dir()
    target = base / "hooks" / MARKER
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(hook_source())
    target.chmod(0o755)

    path = base / "settings.json"
    data = load_settings(path)
    hooks = data.get("hooks")
    if hooks is None:
        hooks = data["hooks"] = {}
    stop = without_our_hook(hooks.get("Stop", []))
    command = f'"{python()}" "{target}"'
    stop.append({"hooks": [{"type": "command", "command": command}]})
    hooks["Stop"] = stop
    save_settings(path, data)

    print(f"Installed. Claude Code will run: {command}")
    print("It takes effect in new Claude Code sessions, in repos whose CLAUDE.md names a shared-brain project.")
    print("Turn it off any time with SHARED_BRAIN_SUMMARY=off, or run this again with --uninstall.")


def uninstall() -> None:
    base = claude_dir()
    path = base / "settings.json"
    data = load_settings(path)
    hooks = data.get("hooks") or {}
    if "Stop" in hooks:
        stop = without_our_hook(hooks["Stop"])
        if stop:
            hooks["Stop"] = stop
        else:
            del hooks["Stop"]
        if not hooks:
            data.pop("hooks", None)
        save_settings(path, data)
    target = base / "hooks" / MARKER
    if target.exists():
        target.unlink()
    print("Removed the shared-brain session-summary hook.")


if __name__ == "__main__":
    if sys.version_info < (3, 7):
        sys.exit("Needs Python 3.7 or newer.")
    uninstall() if "--uninstall" in sys.argv[1:] else install()
