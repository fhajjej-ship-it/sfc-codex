#!/usr/bin/env python3
"""Best-effort literal command guards. Not a shell security boundary."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sfc_lib.factory import secret_problem
from sfc_lib.autostart import launch
from sfc_lib.chat_tracking import project_root

# The host is declared by the hook configuration that runs this script, never
# inferred from event contents. Claude Code's layout passes --host claude.
HOST = 'claude' if '--host=claude' in sys.argv else 'codex'
HOST_NAME = {'codex': 'Codex', 'claude': 'Claude Code'}[HOST]
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
EVENT_NAME = {}


def context(cwd):
    result = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=cwd,
                            capture_output=True, text=True, timeout=5)
    if result.returncode:
        return None
    config = project_root(cwd) / ".sfc/config.json"
    if not config.exists():
        return None
    return config.parent.parent, json.loads(config.read_text())


SEPARATORS = frozenset('();|&')


def segments(command):
    """Simple commands of a shell line, split at ; & && | || and parentheses.

    Redirections (>, 2>&1, &>) contain < or > and stay inside their command,
    so they never end a segment early. Unparseable input yields one segment of
    the whole line, keeping the older conservative behavior.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return [shlex.split(command)]
    result, current = [], []
    for token in tokens:
        if token and set(token) <= SEPARATORS:
            result.append(current)
            current = []
        else:
            current.append(token)
    result.append(current)
    return [part for part in result if part]


HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)\s*\\?(['\"]?)([^\s'\"<>|&;()]+)\2")


def split_heredocs(command):
    """Command lines and heredoc bodies of a shell script, kept apart.

    Bodies are data or, when fed to a shell, code; either way they are not
    part of the command line's quoting. Each `<<WORD`, `<<'WORD'`, `<<"WORD"`
    or `<<-WORD` on a line takes the following lines up to its delimiter
    (tab-indented for `<<-`). Here-strings (`<<<`) are not heredocs. An
    unterminated body runs to the end, as in the shell.
    """
    lines = command.split("\n")
    kept, bodies, index = [], [], 0
    while index < len(lines):
        line = lines[index]
        index += 1
        kept.append(line)
        for match in HEREDOC.finditer(line):
            dash, delimiter, body = match.group(1) == "-", match.group(3), []
            while index < len(lines):
                current = lines[index]
                index += 1
                if (current.lstrip("\t") if dash else current) == delimiter:
                    break
                body.append(current)
            bodies.append(body)
    return "\n".join(kept), bodies


def parse(text, *, strict):
    """Words and simple commands; None when strict text cannot be parsed."""
    try:
        return shlex.split(text), segments(text)
    except ValueError:
        if strict:
            return None
        # A heredoc line is free text: an apostrophe must not hide its words.
        words = text.split()
        return words, [words]


def operands(words):
    """Command arguments without redirections (`> file`, `2>&1`, `< in`).

    Redirection targets are not arguments of the command, so a deletion
    check must not read `/dev/null` in `rm x 2>/dev/null` as an operand.
    """
    result, skip = [], False
    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else ""
        if skip:
            skip = False
        elif word and set(word) <= set("<>&") and ("<" in word or ">" in word):
            skip = True  # the operator's target: a file or an fd number
        elif word.isdigit() and following[:1] in ("<", ">"):
            continue
        else:
            result.append(word)
    return result


def scratch_roots(environ=None):
    """OS temporary folders where Claude file tools may also write.

    Scratch files and drafts belong there, not in the checkout. A root that is
    a filesystem anchor or contains the home folder (TMPDIR=/ or ~) grants
    nothing, so the allowance can never widen to the whole machine.
    """
    environ = os.environ if environ is None else environ
    home = Path.home().resolve()
    roots = []
    for value in (environ.get("TMPDIR"), tempfile.gettempdir(), "/tmp"):
        if not value:
            continue
        root = Path(value).expanduser().resolve()
        if root.is_dir() and root != Path(root.anchor) and not home.is_relative_to(root) and root not in roots:
            roots.append(root)
    return roots


def sensitive(path):
    return secret_problem([path], "") is not None


def inside(path, root, cwd):
    value = Path(path).expanduser()
    if not value.is_absolute():
        value = Path(cwd) / value
    return value.resolve().is_relative_to(root.resolve())


def check(event, root, config, *, chat=False):
    tool = event.get("tool_name")
    data = event.get("tool_input", {})
    if tool in EDIT_TOOLS:
        # Claude Code file tools name their target directly.
        target = data.get("file_path", data.get("notebook_path")) if isinstance(data, dict) else None
        if not isinstance(target, str) or not target:
            return "Cannot identify the edited file for SFC literal guards"
        cwd = event.get("cwd", str(root))
        if not inside(target, Path(cwd), cwd) and not any(inside(target, r, cwd) for r in scratch_roots()):
            return "File edit escapes the selected worktree and the temporary folders"
        if sensitive(target):
            return "Credential file edit blocked"
        edits = data.get("edits") if isinstance(data.get("edits"), list) else []
        text = [data.get("content"), data.get("new_string"), data.get("new_source"),
                *(e.get("new_string") for e in edits if isinstance(e, dict))]
        added = "\n".join(v for v in text if isinstance(v, str))
        return secret_problem([], "".join("+" + line + "\n" for line in added.splitlines()))
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
    text, bodies = split_heredocs(command)
    parsed = parse(text, strict=True)
    if parsed is None:
        return "Cannot parse shell command for SFC literal guards"
    # Heredoc bodies stay checked (a body fed to sh is code), one line at a time.
    for tokens, parts in [parsed, *(parse(line, strict=False) for body in bodies for line in body)]:
        reason = check_words(tokens, parts, root, config, cwd, chat=chat)
        if reason:
            return reason
    return None


def check_words(tokens, parts, root, config, cwd, *, chat=False):
    for token in tokens:
        if sensitive(token) and not token.startswith("-"):
            return "Credential path access blocked; use project-owned key-name checks"
    # Each simple command is checked on its own: words of a chained command
    # (for example `gh pr create --base main`) are not push arguments.
    # Parentheses or operators without spaces hide words from shlex.split.
    for part in parts:
        if "git" not in part or "push" not in part:
            continue
        args = part[part.index("push") + 1:]
        base = config["base_branch"]
        if any(v in ("--force", "--force-with-lease", "--mirror", "--all", "-f") for v in args):
            return "Force or broad Git push blocked"
        if not args or args == ["origin"] or any(v.split(":")[-1].removeprefix("refs/heads/") in (base, "main", "master") for v in args):
            return "Protected or implicit-base push blocked; use a pull request"
    if not chat and "worktree" in tokens and "add" in tokens:
        at = tokens.index("add") + 1
        args = tokens[at:]
        while args and args[0].startswith("-"):
            option = args.pop(0)
            if option in ("-b", "-B") and args:
                args.pop(0)
        if args and not inside(args[0], root / ".sfc/worktrees", cwd):
            return "SFC worktrees must live under .sfc/worktrees/"
    # Deletions are read per simple command, like pushes: a separator attached
    # to a word (`"a.txt";`) must not extend rm into the next command.
    for part in parts:
        for index, word in enumerate(part):
            if word in ("rm", "rmdir"):
                for operand in operands(part[index + 1:]):
                    if not operand.startswith("-") and not inside(operand, Path(cwd), cwd):
                        return "Deletion outside the selected worktree blocked"
    for index, token in enumerate(tokens):
        if token == "clean" and index and tokens[index - 1] == "git":
            return "git clean requires explicit manual recovery outside factory runs"
    return None


def main():
    event = json.load(sys.stdin)
    if isinstance(event, dict) and isinstance(event.get('hook_event_name'), str):
        EVENT_NAME['value'] = event['hook_event_name']
    if '--attachment' in sys.argv:
        from sfc_lib.session_runtime import request_attachment
        result = request_attachment(event, host=HOST)
        if result['state'] == 'tracking':
            plugin = Path(__file__).resolve().parents[1]
            command = shlex.join(['python3', str(plugin / 'scripts/sfc.py'), '--root', result['root'], 'track'])
            slot = result['prefix'] + '-' + result['turn']
            print(json.dumps({'hookSpecificOutput': {'hookEventName': event['hook_event_name'],
                  'additionalContext':
                    'This project uses SFC chat tracking. Work directly in ' + HOST_NAME + ' without starting a separate worker. For an actionable feature, bug fix, '
                    'maintenance, refactor or documentation change, record the task before editing. '
                    f'Use {command} status --prefix {result["prefix"]} to reuse an existing chat task. '
                    f'For new work, use {command} start --task {slot}. Decide delivery from the requested outcome: '
                    '--delivery pr-required is the default for shared-repository changes, including documentation and guidance; '
                    'choose --delivery local-only when the user explicitly requests local delivery. '
                    'Run the relevant tool with --help for its arguments, or read the SFC skill. '
                    'Sync Building before editing, then record actual QA, PR review, blockers and completion. '
                    'Carry authorized shared-repository delivery through a draft PR during QA and merge after checks and review. '
                    'Use existing merge authority and configured auto policy without repeated per-PR approval, preserving '
                    'the configured change-size limit, branch protection and candidate/base pins. Read the SFC skill for the delivery steps. '
                    'Record the returned PR URL with '
                    'track update --pr-url and sync it to the ticket. '
                    'Show the task title, delivery choice and verified issue link in chat once available. Use the latest recorded '
                    'milestone for progress and distinguish pending updates from the last verified board stage. '
                    'Sync at execution checkpoints. If GitHub is unavailable, continue the work and retry the saved task '
                    'at a later checkpoint. Use repository-safe summaries only. Routine questions need no ticket.'}}))
            return
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
    reason = check(event, root, config, chat=True)
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                "permissionDecision": "deny", "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if "--autostart" in sys.argv or "--session" in sys.argv or '--attachment' in sys.argv:
            print(json.dumps({'hookSpecificOutput': {'hookEventName': EVENT_NAME.get('value', 'SessionStart') if HOST == 'claude' else 'SessionStart',
                  'additionalContext': 'SFC could not validate local session configuration (' + type(exc).__name__ +
                  '); no auto-start authorized. Inspect .sfc/config.json and autostart-status; review hooks in ' +
                  HOST_NAME + '.'}}))
            sys.exit(0)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                "permissionDecisionReason": "SFC guard could not validate this call: " + type(exc).__name__}}))
