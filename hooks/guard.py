#!/usr/bin/env python3
"""Best-effort literal command guards. Not a shell security boundary."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sfc_lib.factory import secret_problem
from sfc_lib.autostart import configured_root, launch


def context(cwd):
    result = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=cwd,
                            capture_output=True, text=True, timeout=5)
    if result.returncode:
        return None
    config = configured_root(cwd) / ".sfc/config.json"
    if not config.exists():
        return None
    return config.parent.parent, json.loads(config.read_text())


def sensitive(path):
    return secret_problem([path], "") is not None


def inside(path, root, cwd):
    value = Path(path).expanduser()
    if not value.is_absolute():
        value = Path(cwd) / value
    return value.resolve().is_relative_to(root.resolve())


def check(event, root, config):
    tool = event.get("tool_name")
    data = event.get("tool_input", {})
    command = data.get("command", data.get("cmd", data.get("patch", ""))) if isinstance(data, dict) else data
    if not isinstance(command, str):
        return None
    cwd = event.get("cwd", str(root))
    if tool == "apply_patch":
        paths = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", command, re.M)
        if any(not inside(p, Path(cwd), cwd) for p in paths):
            return "File edit escapes the selected worktree"
        if any(sensitive(p) for p in paths):
            return "Credential file edit blocked"
        return secret_problem([], command)
    if tool not in ("Bash", "exec_command", "functions.exec_command"):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "Cannot parse shell command for SFC literal guards"
    for token in tokens:
        if sensitive(token) and not token.startswith("-"):
            return "Credential path access blocked; use project-owned key-name checks"
    if "git" in tokens and "push" in tokens:
        at = tokens.index("push")
        args = tokens[at + 1:]
        base = config["base_branch"]
        if any(v in ("--force", "--force-with-lease", "--mirror", "--all", "-f") for v in args):
            return "Force or broad Git push blocked"
        if not args or args == ["origin"] or any(v.split(":")[-1].removeprefix("refs/heads/") in (base, "main", "master") for v in args):
            return "Protected or implicit-base push blocked; use the SFC merge gate"
    if "worktree" in tokens and "add" in tokens:
        at = tokens.index("add") + 1
        args = tokens[at:]
        while args and args[0].startswith("-"):
            option = args.pop(0)
            if option in ("-b", "-B") and args:
                args.pop(0)
        if args and not inside(args[0], root / ".sfc/worktrees", cwd):
            return "SFC worktrees must live under .sfc/worktrees/"
    for index, token in enumerate(tokens):
        if token in ("rm", "rmdir"):
            for operand in tokens[index + 1:]:
                if operand in (";", "&&", "||", "|"):
                    break
                if not operand.startswith("-") and not inside(operand, Path(cwd), cwd):
                    return "Deletion outside the selected worktree blocked"
        if token == "clean" and index and tokens[index - 1] == "git":
            return "git clean requires explicit manual recovery outside factory runs"
    return None


def main():
    event = json.load(sys.stdin)
    if '--attachment' in sys.argv:
        from sfc_lib.session_runtime import request_attachment
        result = request_attachment(event)
        if result['state'] == 'pending':
            print(json.dumps({'hookSpecificOutput': {'hookEventName': event['hook_event_name'],
                  'additionalContext': 'SFC session attachment requested. If MCP is not ready, state is Pending; trusted UserPromptSubmit/PreModelRequest hooks retry. Read session_status for this connection, and local receipts for work authority.'}}))
        return
    if "--autostart" in sys.argv:
        cancelled = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: cancelled.set())
        result = launch(event, cancelled=cancelled.is_set)
        if result['state'] != 'ignored':
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart',
                  'additionalContext': 'SFC auto-start: ' + result['state'] +
                  '. Inspect local autostart-status and watch-status for scope, budgets and recovery.'}}))
        return
    found = context(event.get("cwd", "."))
    if not found:
        return
    root, config = found
    if "--session" in sys.argv:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
            f"SFC is configured at {root}/.sfc/config.json. Read saved status before starting/resuming; use SFC's checked supervisor for board writes and merges. Hooks are literal mistake guards, not a security sandbox."}}))
        return
    reason = check(event, root, config)
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                "permissionDecision": "deny", "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if "--autostart" in sys.argv or "--session" in sys.argv or '--attachment' in sys.argv:
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart',
                  'additionalContext': 'SFC could not validate local session configuration (' + type(exc).__name__ +
                  '); no auto-start authorized. Inspect .sfc/config.json and autostart-status; review hooks in Codex.'}}))
            sys.exit(0)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                "permissionDecisionReason": "SFC guard could not validate this call: " + type(exc).__name__}}))
