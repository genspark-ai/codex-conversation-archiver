#!/usr/bin/env python3
"""Codex session status + title reporter for GenTerminal (OSC 9999).

Registered by the conversation-archiver plugin's hooks on SessionStart,
UserPromptSubmit, PostToolUse, Stop and SessionEnd. Each run reads the hook
payload from stdin and pushes at most ONE `genterm-notify` notification to
the terminal (scripts/notify.py) so GenTerminal's sidebar Sessions section
can:

  * rename the managed record to the session's title — the join is
    `payload.tmux.session == record.tmux_name`, consent is the record's
    Launch Codex flag, and placeholder titles are filtered on the app side;
  * light the unread dot / refresh "last activity" — every report counts;
  * show execution status in the in-app inbox — "Session started",
    "Turn N started", "Turn complete · N turns", "Session ended".

Title resolution (mirrors what `codex resume` displays, in the same order):
  1. the user's explicit Rename chat name, from `$CODEX_HOME/session_index.jsonl`
     (append-only, latest entry for the thread id wins — the same lookup
     codex-rs `find_thread_name_by_id` performs);
  2. the transcript's first real user message — codex's own fallback-title
     derivation: strip recognized control wrappers (`<environment_context>`,
     `<user_instructions>`, …), first non-empty line, 120-char cap;
  3. the fixed placeholder "Codex" — GenTerminal never renames a record to a
     placeholder, so a fresh session cannot clobber the user's chosen name.

Codex fires NO hook on Rename chat — verified against codex-rs (`HookEventName`
has no rename variant) and live against codex-cli 0.154.0, where
`$CODEX_HOME/session_index.jsonl` gains the new name with ZERO hook
invocations. A rename therefore lands on the NEXT hook event: a prompt (this
run resolves custom names first), the running turn's Stop, or SessionEnd —
but not while an idle session is left untouched. `/new` and `/clear` fire no
hook either; they surface as the new conversation's first `SessionStart`
(`source=startup` / `clear`), never at the command.

That gap was once closed by a detached polling daemon (`title_watch.py`,
0.2.0–0.2.2). It was removed again in 0.3.0: the polling could not serve the
case that needs it most — a rename in a conversation that has not had a turn
yet writes an index entry carrying only `{id, thread_name, updated_at}`, with
no rollout file, no cwd and no pane, so nothing can attribute that name to a
record, while a daemon per pane still cost a process, per-hook tmux round
trips and a pile of state. Accepted behaviour instead: a renamed record
updates on that session's next hook event.

Never blocks the agent: prints NOTHING on stdout (hook stdout becomes model
context on several events), always exits 0, every failure degrades to
silence. Best-effort by design.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import notify  # noqa: E402  (sibling module; stdlib-only)

SOURCE = "codex-conversation-archiver"
PLACEHOLDER_TITLE = "Codex"
# codex-rs external-agent-migration SESSION_TITLE_MAX_LEN — the cap codex
# itself puts on fallback titles.
SESSION_TITLE_MAX_LEN = 120

# Control wrappers stripped from a user message before deriving a title.
# codex's fallback-title derivation strips its wrapper set (title.rs
# RECOGNIZED_CONTROL_WRAPPERS); the environment/instruction blocks are added
# because codex injects them as role=user items in the rollout, where a naive
# first-user-message read would pick them up.
CONTROL_WRAPPERS = [
    "command-message",
    "command-name",
    "command-args",
    "local-command-caveat",
    "local-command-stderr",
    "local-command-stdout",
    "task-notification",
    "system-reminder",
    "ide_opened_file",
    "ide_selection",
    "environment_context",
    "user_instructions",
    "skills_instructions",
    "turn_aborted",
    "permissions_update",
]
_WRAPPER_NAME = re.compile(r"<([a-zA-Z0-9_-]+)>")


# ── locations ──────────────────────────────────────────────────────────────

def _codex_home(payload: dict) -> Path:
    """CODEX_HOME from the hook env (codex sets it for plugin hooks), derived
    from transcript_path as a fallback, else ~/.codex."""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env)
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        # <home>/sessions/YYYY/MM/DD/rollout-*.jsonl
        try:
            tpath = Path(transcript)
            home = tpath.parents[4]
            if home.joinpath("sessions").is_dir() or tpath.parents[3].name == "sessions":
                return home
        except IndexError:
            pass
    return Path.home() / ".codex"


def _data_dir() -> Path:
    """Plugin-writable state dir (codex sets PLUGIN_DATA for plugin hooks)."""
    env = os.environ.get("PLUGIN_DATA")
    base = Path(env) if env else Path.home() / ".cache" / "genterminal" / "codex-archiver"
    state = base / "state"
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return state


def _atomic_write(path: Path, text: str) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


# ── session generation (which conversation a pane is on) ───────────────────

def _current_session_file(key: str) -> Path:
    """Which codex session is the CURRENT one for a pane.

    `/new` and `/clear` start a new codex session id in the same pane, and
    codex keeps firing the ABANDONED thread's hooks afterwards — its
    SessionEnd for certain (at /quit, or when the TUI finalizes it). That
    payload carries the SAME tmux context, so without this the record would be
    renamed back to a conversation that no longer exists. Each turn's hook
    records its session id here; a hook whose id does not match is stale."""
    return _data_dir() / f"current-{key}"


def _tmux_session_name() -> str:
    """The pane's tmux session name — the managed record's `tmux_name`, the
    same join key the app uses. Deliberately not `notify.tmux_context()`: that
    value is the notification payload's wire contract."""
    if not os.environ.get("TMUX"):
        return ""
    try:
        return subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""


def _generation_key(session_name: str) -> str:
    """Stable per-pane key for the current-session file. Empty without tmux:
    the guard is then inert, which is the behaviour that preceded it."""
    if not session_name:
        return ""
    return "tmux-" + hashlib.sha1(session_name.encode("utf-8")).hexdigest()[:16]


def mark_current_generation(session_id: str) -> None:
    """Record `session_id` as this pane's current conversation. Best-effort:
    the guard degrades to inert, never to an error, and this sits on the hook
    path where an exception would be noise."""
    try:
        key = _generation_key(_tmux_session_name())
        if key:
            _atomic_write(_current_session_file(key), session_id)
    except Exception:  # noqa: BLE001  (never disrupt the session)
        pass


def is_current_generation(session_id: str) -> bool:
    """Whether `session_id` is the CURRENT codex session for this pane.

    A `/new` or `/clear` abandons the previous thread but codex still fires
    that thread's SessionEnd, whose payload carries the SAME tmux context and
    resolves to ITS OWN dead title — emitting would rename the record to a
    conversation that no longer exists. Unknown (non-tmux, or nothing recorded
    yet) is True: the pre-guard behaviour, and the only sane default there."""
    key = _generation_key(_tmux_session_name())
    if not key:
        return True
    try:
        current = _current_session_file(key).read_text(encoding="utf-8").strip()
    except OSError:
        return True
    return not current or current == session_id


# ── title sources ──────────────────────────────────────────────────────────

def custom_thread_name(codex_home: Path, session_id: str) -> str | None:
    """The user's Rename chat name: `$CODEX_HOME/session_index.jsonl` is
    append-only and the LATEST entry for the thread id wins (the same rule as
    codex-rs find_thread_name_by_id). Missing file / garbage degrades to
    None — including valid-but-wrong-shape lines (null, arrays) and a JSON
    null thread_name (which must never become the string "None")."""
    index = codex_home / "session_index.jsonl"
    try:
        lines = index.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("id") != session_id:
            continue
        name = entry.get("thread_name")
        if not isinstance(name, str):
            continue
        name = name.strip()
        return name or None
    return None


def _strip_leading_control_wrappers(text: str) -> str | None:
    """Strip wrapper elements the way codex's fallback-title derivation does:
    while the text starts with a RECOGNIZED wrapper's opening tag, skip to its
    matching close tag (same-tag nesting counted). Unmatched close → the
    message is malformed; return None so the caller skips it entirely."""
    remainder = text.lstrip()
    while True:
        m = _WRAPPER_NAME.match(remainder)
        if not m or m.group(1) not in CONTROL_WRAPPERS:
            return remainder
        tag = m.group(1)
        close = f"</{tag}>"
        open_tag = f"<{tag}>"
        depth = 1
        cursor = remainder.find(">") + 1
        while depth > 0:
            next_open = remainder.find(open_tag, cursor)
            next_close = remainder.find(close, cursor)
            if next_close == -1:
                return None
            if next_open != -1 and next_open < next_close:
                depth += 1
                cursor = next_open + len(open_tag)
            else:
                depth -= 1
                cursor = next_close + len(close)
        remainder = remainder[cursor:].lstrip()


def derived_title(transcript_path: str | None) -> str | None:
    """The first real user message of the rollout transcript, as a title:
    wrapper-stripped, first non-empty line, capped. Reads forward and stops
    at the first hit — a multi-MB transcript costs one pass at most, and the
    first message sits at its head."""
    if not transcript_path:
        return None
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") != "response_item":
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") != "message" or payload.get("role") != "user":
                    continue
                content = payload.get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "input_text":
                        continue
                    text = item.get("text")
                    if not isinstance(text, str) or not text.strip():
                        continue
                    stripped = _strip_leading_control_wrappers(text)
                    if stripped is None:
                        continue
                    first_line = next(
                        (ln.strip() for ln in stripped.splitlines() if ln.strip()),
                        "",
                    )
                    if first_line:
                        return first_line[:SESSION_TITLE_MAX_LEN]
    except OSError:
        return None
    return None


def resolve_title(payload: dict, codex_home: Path) -> tuple[str, bool]:
    """(title, is_real). A placeholder title is never a rename candidate —
    GenTerminal filters it, so it can only carry status."""
    custom = custom_thread_name(codex_home, str(payload.get("session_id", "")))
    if custom:
        return custom, True
    derived = derived_title(payload.get("transcript_path"))
    if derived:
        return derived, True
    return PLACEHOLDER_TITLE, False


# ── reporting ──────────────────────────────────────────────────────────────

def _turns_file(session_id: str) -> Path:
    return _data_dir() / f"{session_id}.turns"


def _title_file(session_id: str) -> Path:
    return _data_dir() / f"{session_id}.title"


def _read_int(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _read_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def _emit(payload: dict, title: str, body: str, last_title: str | None) -> bool:
    """One notification. Returns True when the OSC sequence was written; the
    caller marks the title as reported only then, so a tty-less failure (an
    early SessionStart) retries on the next hook event instead of going
    silent for the whole session."""
    emitted = notify.emit(
        source=SOURCE,
        source_id=str(payload.get("session_id") or "unknown"),
        event="TitleChanged",
        title=title,
        body=body,
        tmux=notify.tmux_context(),
    )
    if emitted:
        session_id = str(payload.get("session_id") or "")
        if session_id:
            _atomic_write(_title_file(session_id), title)
    elif last_title is not None:
        # Diagnostic only: stderr of a command hook is logged by codex but
        # never shown to the model (stdout is the context channel).
        print(
            f"codex-conversation-archiver: emit failed (no tty) title:{title}",
            file=sys.stderr,
        )
    return emitted


def report(payload: dict) -> bool:
    """One hook pass. Returns True when a notification was emitted."""
    if os.environ.get("CODEX_ARCHIVER_NO_NOTIFY"):
        return False
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return False
    event = str(payload.get("hook_event_name") or "?")
    home = _codex_home(payload)
    title, is_real = resolve_title(payload, home)
    last_title = _read_text(_title_file(session_id))
    turns_path = _turns_file(session_id)

    # Record which conversation this pane is on, so an ABANDONED thread's
    # SessionEnd (same pane, dead title) cannot rename the record.
    if event in ("SessionStart", "UserPromptSubmit"):
        mark_current_generation(session_id)

    # SessionStart "clear" resets the conversation: drop the stale label and
    # the turn count, and report the reset (a placeholder title carries the
    # status; the new conversation's own first title takes over later).
    if event == "SessionStart" and payload.get("source") == "clear":
        _atomic_write(_turns_file(session_id), "0")
        return _emit(payload, PLACEHOLDER_TITLE, "Session cleared", last_title)

    if event == "UserPromptSubmit":
        turns = _read_int(turns_path) + 1
        _atomic_write(turns_path, str(turns))
        # The prompt is not in the rollout YET when this hook fires (observed
        # against 0.154.0), so the first turn's title would otherwise lag one
        # boundary behind. The hook payload carries the prompt itself — use
        # it as the derived-title source when there is no real title yet.
        if not is_real and isinstance(payload.get("prompt"), str):
            stripped = _strip_leading_control_wrappers(payload["prompt"])
            first_line = next(
                (ln.strip() for ln in (stripped or "").splitlines() if ln.strip()),
                "",
            )
            if first_line:
                title = first_line[:SESSION_TITLE_MAX_LEN]
        return _emit(payload, title, f"Turn {turns} started", last_title)

    if event == "Stop":
        turns = _read_int(turns_path)
        unit = "turn" if turns == 1 else "turns"
        return _emit(payload, title, f"Turn complete · {turns} {unit}", last_title)

    if event == "SessionEnd":
        # An abandoned thread (/new, /clear) shares this pane's tmux context
        # and resolves to ITS OWN stale title — emitting would rename the
        # record back to a conversation that no longer exists.
        if not is_current_generation(session_id):
            return False
        return _emit(payload, title, "Session ended", last_title)

    if event == "SessionStart":
        # startup/resume/compact. Re-report even an unchanged REAL title on
        # startup/resume: the receiving app may have restarted or another
        # device may hold a stale record — one heal per launch is cheap.
        # A compaction is a mid-session event; it takes the changed-title
        # path so it never spams "Session resumed" on every auto-compact.
        source = payload.get("source")
        if source in ("startup", "resume") or (last_title is None and source == "compact"):
            body = "Session resumed" if source == "resume" else "Session started"
            return _emit(payload, title, body, last_title)
        if title != last_title:
            return _emit(payload, title, "Context compacted", last_title)
        return False

    if event == "PostToolUse":
        # Mid-turn re-report ONLY on a real title change (a mid-turn Rename
        # chat, or the first derived title arriving seconds into the first
        # turn — UserPromptSubmit already reported it if it existed then).
        # Plain tool activity never emits: every tool call would otherwise
        # spam the inbox, and the turn-level events already carry status.
        if is_real and title != last_title:
            turns = _read_int(turns_path)
            return _emit(payload, title, f"Working · turn {turns}", last_title)
        return False

    return False


def main() -> None:
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    report(payload)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001  (never disrupt the session)
        # Diagnostics only: stderr is logged by codex, never model-visible.
        try:
            print(f"codex-conversation-archiver: {exc}", file=sys.stderr)
        except Exception:
            pass
    sys.exit(0)
