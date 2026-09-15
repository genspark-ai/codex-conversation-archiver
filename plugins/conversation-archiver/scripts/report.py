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

Codex fires NO hook on Rename chat (or on /clear and /new), so an idle rename
reaches consumers on the next hook event (next prompt / next turn boundary)
rather than within seconds. `ensure_watcher` below closes that gap with the
same detached per-session daemon the cc-conversation-archiver uses for Claude
Code (`title_watch.py`): every hook run keeps one alive, it polls
`$CODEX_HOME/session_index.jsonl` and pushes a name change through this
module's notify.emit / title-marker path. The codex liveness signal the hook
payload lacks is resolved here instead — the codex CLI pid is the first
`codex` ancestor of this hook process.

Never blocks the agent: prints NOTHING on stdout (hook stdout becomes model
context on several events), always exits 0, every failure degrades to
silence. Best-effort by design.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
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


# ── title watcher (`/rename` immediacy) ────────────────────────────────────

def _watch_pidfile(session_id: str) -> Path:
    """Pidfile of the session's title watcher daemon (title_watch.py). Written
    ONLY by ensure_watcher below; the daemon reads it and exits when a newer
    watcher's pid replaces its own (spawn races converge to newest-wins)."""
    return _data_dir() / f"{session_id}.watch"


def _watch_ttyfile(session_id: str) -> Path:
    """Freshest resolvable target tty for the session's watcher daemon.

    The daemon cannot resolve a tty outside tmux (start_new_session: no
    controlling terminal, reparented so no useful ancestor chain), and
    resolution CAN fail in the hook that happens to spawn the watcher (an
    early SessionStart). So the tty is decoupled from spawn time: EVERY hook
    run refreshes this file when it can resolve a tty, and the daemon re-reads
    it before each emit."""
    return _data_dir() / f"{session_id}.tty"


def _watch_logfile(session_id: str) -> Path:
    """One-line-per-life log of the session's watcher daemon: the spawn
    parameters, then a single exit line naming WHY it stopped. Written by the
    daemon (title_watch.py); truncated at spawn. Exists because every other
    failure mode in this file degrades to silence, and a watcher that dies
    silently is indistinguishable from one that was never spawned."""
    return _data_dir() / f"{session_id}.watch.log"


def _watch_current_file(key: str) -> Path:
    """Which codex session is the CURRENT one for a watch key (one managed
    tmux pane / record).

    /new and /clear start a NEW codex session id in the SAME pane, and nothing
    tells the previous session's watcher to stop (codex fires SessionEnd for
    the abandoned thread only much later, if at all). This file — rewritten by
    ensure_watcher on every hook run — is that signal: the daemon exits as
    soon as it names a different session, so one pane holds at most one
    watcher instead of one per abandoned thread."""
    return _data_dir() / f"current-{key}"


def _watch_log_append(session_id: str, message: str) -> None:
    """Append one timestamped line to the session's watcher log.

    Single writer implementation shared by ensure_watcher / stop_watcher here
    and the daemon (title_watch.Watch.log) — best-effort, because logging must
    never itself become a failure."""
    try:
        with _watch_logfile(session_id).open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def _tmux_identity() -> tuple[str, str]:
    """`(session name, pane id)` from ONE tmux call, or `("", "")`.

    The session name is the pane's identity for the generation file (the
    managed record's `tmux_name`, the same join key the app uses); the pane id
    is the daemon's tmux-side liveness anchor — when the pane is gone (tab
    closed, session destroyed) the watcher must go too, and that signal works
    even when no codex pid could be resolved. Deliberately not
    `notify.tmux_context()`: that value is the notification payload's wire
    contract, and this only needs two format fields."""
    if not os.environ.get("TMUX"):
        return ("", "")
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_id}\t#S"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return ("", "")
    pane, _, name = out.partition("\t")
    return (name.strip(), pane.strip())


def _watch_key(session_name: str) -> str:
    """Stable per-pane identity for the current-session file (empty when not
    under tmux: the per-session watcher then relies on its process/lifetime
    bounds alone)."""
    if not session_name:
        return ""
    return "tmux-" + hashlib.sha1(session_name.encode("utf-8")).hexdigest()[:16]


def process_start(pid: int) -> str:
    """The process's start time (`ps -o lstart=`), or "" when unknown.

    The daemon's liveness probe pairs this with the pid so a RECYCLED pid — a
    dead codex whose number a later process took — cannot keep a watcher alive
    past its session. "" means "could not tell" (no ps, unsupported flag) and
    must be read as "still alive", never as "gone": a probe outage must not
    kill a healthy watcher."""
    if not pid:
        return ""
    try:
        return subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""


def tmux_pane_alive(pane: str) -> bool:
    """Whether the tmux pane the emitter ran in still exists.

    Unknown (no tmux in the env, or a tmux failure) is True: this is a
    best-effort extra bound on a detached daemon, never a reason to exit."""
    if not pane or not os.environ.get("TMUX"):
        return True
    try:
        res = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return True
    return res.returncode == 0 and bool(res.stdout.strip())


def is_current_generation(session_id: str) -> bool:
    """Whether `session_id` is the CURRENT codex session for this pane.

    A `/new` or `/clear` abandons the previous thread but codex still fires
    that thread's SessionEnd (at /quit, or when the TUI finalizes it). The
    abandoned session's hook payload carries the SAME tmux context, so without
    this guard its "Session ended" would rename the record to a conversation
    that no longer exists — the exact stale-name class this file's generation
    tracking exists to prevent. Unknown (non-tmux, or no file yet) is True:
    the previous behaviour, and the only sane default there."""
    key = _watch_key(_tmux_identity()[0])
    if not key:
        return True
    try:
        current = _watch_current_file(key).read_text(encoding="utf-8").strip()
    except OSError:
        return True
    return not current or current == session_id


def _refresh_watch_tty(session_id: str) -> None:
    """Best-effort: record the currently-resolvable target tty for the
    daemon. A failure (no tty this run, unwritable state dir) leaves the
    previous value in place — never raise into the hook."""
    try:
        tty = notify._target_tty()
        if not tty:
            return
        tty_file = _watch_ttyfile(session_id)
        try:
            if tty_file.read_text(encoding="utf-8").strip() == tty:
                return
        except OSError:
            pass
        _atomic_write(tty_file, tty)
    except Exception:  # noqa: BLE001  (never disrupt the session)
        pass


def codex_pid() -> int | None:
    """Pid of the codex CLI process this hook runs under, or None.

    The hook is a descendant of codex (codex -> sh -c -> python3), so walk the
    ancestor chain and return the first process whose executable name is
    codex. Used only as the watcher daemon's liveness probe; None just means
    the watcher falls back to its lifetime backstop."""
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            return None
        try:
            comm = subprocess.run(
                ["ps", "-o", "comm=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        if comm and Path(comm).name.startswith("codex"):
            return pid
        try:
            ppid = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            pid = int(ppid or "1")
        except (FileNotFoundError, subprocess.SubprocessError, ValueError):
            return None
    return None


def _spawn_watcher(argv: list[str]) -> int | None:
    """Start the watcher fully detached (own session, no inherited stdio) and
    return its pid. Split out so tests can stub the process launch."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return proc.pid


def _pidfile_watcher_alive(session_id: str) -> bool:
    """Whether the session's pidfile names a LIVE watcher for that session.

    A bare `kill(pid, 0)` is not enough: pids get recycled, and a stale
    pidfile whose number a later process took would make ensure_watcher skip
    the spawn forever — leaving the session with no watcher and, because this
    file degrades to silence, no trace of why. So confirm the process really
    is a title_watch.py for this session id; an unverifiable probe (no ps, an
    unsupported flag) trusts the live pid rather than risk a duplicate."""
    try:
        pid = int(_watch_pidfile(session_id).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        args = subprocess.run(
            ["ps", "-ww", "-o", "args=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return True
    return "title_watch.py" in args and session_id in args


def ensure_watcher(session_id: str, codex_home: Path) -> bool:
    """Keep one title_watch.py daemon alive for the session; returns True when
    a new one was spawned.

    /rename fires no hook, so report() below only delivers a manual rename on
    the NEXT hook event — the next prompt, when the session is idle. The
    daemon polls the session index and closes that gap; every hook run
    re-ensures it (cheap pidfile + liveness probe) so a crashed or
    lifetime-expired watcher heals on the session's next activity.

    The daemon is handed everything it needs to stop itself when the session
    ends, because this hook will not run again to do it: the codex process
    identity (pid + start time, so a recycled pid cannot fake liveness) and
    the tmux pane id (so a closed tab stops it even without a pid). The target
    tty travels through _watch_ttyfile, refreshed on EVERY hook run — not just
    at spawn — because the run that spawns the watcher may be unable to
    resolve one (early SessionStart) while a later run can."""
    if os.name != "posix":
        # Liveness probing is kill(0)-based; on Windows that terminates the
        # probed process. The hook cadence remains the only reporter there.
        return False
    if os.environ.get("CODEX_ARCHIVER_NO_WATCHER"):
        # Test/automation opt-out: without it an e2e run leaves a poller
        # behind pointing at a temp dir that is about to be deleted.
        return False
    _refresh_watch_tty(session_id)
    session_name, pane = _tmux_identity()
    key = _watch_key(session_name)
    if key:
        # Announce the current generation BEFORE spawning: an abandoned
        # thread's watcher (same pane, older session id) exits on its next
        # tick. Written every run so it heals a clobbered/deleted file.
        _atomic_write(_watch_current_file(key), session_id)
    if _pidfile_watcher_alive(session_id):
        return False  # a watcher is running
    try:
        owner = codex_pid()
        argv = [
            sys.executable,
            str(Path(__file__).resolve().parent / "title_watch.py"),
            "--session-id", session_id,
            "--codex-home", str(codex_home),
            "--codex-pid", str(owner or 0),
            "--codex-start", process_start(owner) if owner else "",
            "--key", key,
            "--pane", pane,
        ]
        pid = _spawn_watcher(argv)
        if pid is None:
            return False
        _atomic_write(_watch_pidfile(session_id), str(pid))
        return True
    except Exception:  # noqa: BLE001  (never disrupt the session)
        return False


def stop_watcher(session_id: str) -> None:
    """Kill the session's watcher daemon and drop its pidfile (SessionEnd).

    Without this an abandoned thread's watcher (`/new`, `/clear`) would linger
    until its lifetime backstop: its session id never gets another hook run to
    supersede it. Best-effort — a missing pidfile or an already-dead pid is
    normal."""
    pidfile = _watch_pidfile(session_id)
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    _watch_log_append(session_id, f"stop requested by SessionEnd pid:{pid}")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        pidfile.unlink()
    except OSError:
        pass


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

    # Keep the /rename watcher daemon alive across the turn's hook runs (a
    # rename fires no hook at all, so only a poller can deliver it while the
    # session is idle). SessionEnd is the one event that must NOT spawn — it
    # tears the watcher down instead, so an abandoned thread (/new, /clear)
    # does not leave a daemon polling a name that can never change.
    if event == "SessionEnd":
        stop_watcher(session_id)
    elif event in ("SessionStart", "UserPromptSubmit", "Stop"):
        ensure_watcher(session_id, home)

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
        # record back to a conversation that no longer exists. The watcher
        # teardown above still happens for it.
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
