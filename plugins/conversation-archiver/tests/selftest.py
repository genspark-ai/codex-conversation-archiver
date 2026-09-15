#!/usr/bin/env python3
"""Self-contained selftest for the conversation-archiver plugin.

Run directly:

    python3 plugins/codex-conversation-archiver/plugins/conversation-archiver/tests/selftest.py

or through the Jest wrapper (__tests__/codexArchiverPlugin.test.ts), which
runs it on CI hosts with python3 and skips otherwise. Pure stdlib, no pytest.

Layers:
  1. Title resolution units — custom thread name (latest entry wins, garbage
     tolerated), derived title (control wrappers stripped, environment blocks
     skipped, first non-empty line, 120-char cap), placeholder fallback.
  2. Event semantics — bodies per event, turn counting, clear reset,
     PostToolUse only-on-change, SessionEnd, no-notify env opt-out.
  3. Session generation guard — an abandoned thread's SessionEnd (`/new`,
     `/clear`) must not rename the record, and must be inert without tmux.
  4. End-to-end emission — report.py as a subprocess with a real pty as its
     controlling tty (setsid + TIOCSCTTY, the same detached shape a codex
     hook has), capturing the raw OSC 9999 bytes from the pty master and
     validating them against GenTerminal's wire contract (utils/osc.ts):
     ESC ] 9999 ; base64(JSON{v, magic, source, sourceId, event, title,
     body, tmux?}) ST. Also never-raises and exit 0 on hostile inputs.
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import notify  # noqa: E402
import report  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "ok" if cond else "FAIL"
    print(f"{status}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ── fixtures ───────────────────────────────────────────────────────────────

def make_home(root: Path, session_id: str) -> tuple[Path, str]:
    """A synthetic CODEX_HOME: a rollout transcript and a session index with
    two entries for the session (the LATER one must win)."""
    home = root / "codexhome"
    sessions = home / "sessions" / "2026" / "09" / "12"
    sessions.mkdir(parents=True, exist_ok=True)
    rollout = sessions / f"rollout-2026-09-12T16-57-21-{session_id}.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"session_id": session_id, "cwd": str(root / "cwd")},
                    }
                ),
                # Injected environment block arrives as a role=user item —
                # the derived title must skip it.
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "<environment_context>\n  <cwd>/srv</cwd>\n</environment_context>",
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "assistant noise, never a title"}
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "<user_instructions>ignore me</user_instructions>\nFix the login bug\nsecond line ignored",
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (home / "session_index.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {"id": "other-session", "thread_name": "not mine", "updated_at": "t0"}
                ),
                json.dumps({"id": session_id, "thread_name": "Old name", "updated_at": "t1"}),
                json.dumps({"id": session_id, "thread_name": "My renamed chat", "updated_at": "t2"}),
                "garbage line {",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return home, str(rollout)


def make_data(root: Path) -> Path:
    data = root / "plugindata" / "state"
    data.mkdir(parents=True)
    return data


def payload_for(session_id: str, rollout: str, event: str, **over: object) -> dict:
    base: dict = {
        "session_id": session_id,
        "transcript_path": rollout,
        "cwd": "/srv",
        "hook_event_name": event,
        "model": "gpt-6-astra",
        "permission_mode": "bypassPermissions",
    }
    base.update(over)
    return base


# ── 1. title resolution ────────────────────────────────────────────────────

def test_title_resolution(root: Path) -> tuple[Path, str]:
    home, rollout = make_home(root, "sess-1")
    custom = report.custom_thread_name(home, "sess-1")
    check("custom thread name: latest index entry wins", custom == "My renamed chat", repr(custom))
    check(
        "custom thread name: missing session degrades to None",
        report.custom_thread_name(home, "nope") is None,
    )

    # Wrong-shape index lines must degrade to None, never raise and never
    # surface as the literal string "None" (Bugbot on PR #607).
    hostile_home = root / "codexhome-hostile"
    hostile_home.mkdir()
    (hostile_home / "session_index.jsonl").write_text(
        "\n".join(
            [
                "null",
                "[]",
                json.dumps({"id": "sess-1", "thread_name": None, "updated_at": "t0"}),
                json.dumps({"id": "sess-1", "thread_name": 42, "updated_at": "t0"}),
                json.dumps({"id": "sess-1", "thread_name": "  ", "updated_at": "t0"}),
                json.dumps({"id": "sess-1", "thread_name": "Valid at last", "updated_at": "t9"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    check(
        "custom thread name: hostile shapes skipped, latest valid wins",
        report.custom_thread_name(hostile_home, "sess-1") == "Valid at last",
        repr(report.custom_thread_name(hostile_home, "sess-1")),
    )
    check(
        "custom thread name: hostile shapes with no valid entry degrade to None",
        report.custom_thread_name(hostile_home, "other") is None,
    )

    derived = report.derived_title(rollout)
    check("derived title skips environment + instruction wrappers, first line",
          derived == "Fix the login bug", repr(derived))
    check("derived title: no transcript → None", report.derived_title(None) is None)

    # Strip the custom name: the derived title must surface.
    (home / "session_index.jsonl").unlink()
    title, is_real = report.resolve_title(
        payload_for("sess-1", rollout, "SessionStart"), home
    )
    check("resolve_title falls back to derived (real)", title == "Fix the login bug" and is_real)

    # No user message at all: placeholder, flagged not-real.
    empty_home = root / "codexhome-empty"
    sessions = empty_home / "sessions" / "2026" / "09" / "12"
    sessions.mkdir(parents=True)
    empty_rollout = sessions / "rollout-empty.jsonl"
    empty_rollout.write_text("", encoding="utf-8")
    title, is_real = report.resolve_title(
        payload_for("sess-2", str(empty_rollout), "SessionStart"), empty_home
    )
    check("resolve_title placeholder when nothing real exists",
          title == report.PLACEHOLDER_TITLE and not is_real)

    # 120-char cap, matching codex's own SESSION_TITLE_MAX_LEN.
    long_home = root / "codexhome-long"
    lsessions = long_home / "sessions" / "2026" / "09" / "12"
    lsessions.mkdir(parents=True)
    long_rollout = lsessions / "rollout-long.jsonl"
    long_prompt = "x" * 500
    long_rollout.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": long_prompt}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    check("derived title capped at 120 chars",
          report.derived_title(str(long_rollout)) == "x" * 120)

    return home, rollout


# ── 2. event semantics (with a fake tty: emit "succeeds" into a file) ───────

class FakeTty:
    """Redirect notify.emit's device resolution to a temp file so report()
    can be driven in-process. The pty layer below covers the real tty path."""

    def __init__(self, tmp: Path) -> None:
        self.file = tmp / "fake.tty"
        self.file.write_text("")
        self.original = notify._target_tty
        notify._target_tty = lambda: str(self.file)

    def read_all(self) -> str:
        return self.file.read_text(encoding="utf-8", errors="replace")

    def restore(self) -> None:
        notify._target_tty = self.original


def decode_osc(raw: str) -> list[dict]:
    payloads = []
    while "]9999;" in raw:
        start = raw.index("]9999;") + len("]9999;")
        end = raw.find("\033\\", start)
        if end == -1:
            break
        try:
            blob = base64.b64decode(raw[start:end])
            payloads.append(json.loads(blob.decode("utf-8")))
        except Exception:  # noqa: BLE001
            pass
        raw = raw[end + 2:]
    return payloads


def test_events(root: Path) -> None:
    # Fresh fixture: test_title_resolution deletes the session index above, so
    # the event tests build their own home (same shape, own session id).
    ev_root = root / "events"
    ev_root.mkdir()
    home, rollout = make_home(ev_root, "sess-ev")
    data = make_data(root)
    fake = FakeTty(root)
    os.environ["CODEX_HOME"] = str(home)
    os.environ["PLUGIN_DATA"] = str(data)
    os.environ.pop("TMUX", None)
    os.environ.pop("CODEX_ARCHIVER_NO_NOTIFY", None)

    sid = "sess-ev"

    def step(payload: dict) -> tuple[bool, list[dict]]:
        """One report() pass, returning (emitted?, events of THIS step). The
        fake tty is a plain file (a real tty is a stream), so each write
        restarts at offset 0 — drain after every step instead of reading
        cumulatively."""
        ok = report.report(payload)
        events = decode_osc(fake.read_all())
        fake.file.write_text("")
        return ok, events

    # SessionStart startup: heal path re-reports even when unchanged (the
    # app may have restarted); here nothing reported yet.
    ok, events = step(payload_for(sid, rollout, "SessionStart", source="startup"))
    check("SessionStart startup emits once", ok and len(events) == 1)
    check("SessionStart startup title is the custom name",
          events and events[0]["title"] == "My renamed chat")
    check("SessionStart startup body", events and events[0]["body"] == "Session started")
    check("payload source", events and events[0]["source"] == report.SOURCE)
    check("payload magic", events and events[0].get("magic") == notify.MAGIC)

    # Resume heal: an unchanged REAL title is re-reported anyway (the app may
    # have restarted with a stale record).
    ok, events = step(payload_for(sid, rollout, "SessionStart", source="resume"))
    check("SessionStart resume re-reports (heal)", ok and len(events) == 1)
    check("resume body", bool(events) and events[0]["body"] == "Session resumed")

    # UserPromptSubmit: turn counter increments per session.
    ok1, ev1 = step(payload_for(sid, rollout, "UserPromptSubmit", prompt="do it"))
    ok2, ev2 = step(payload_for(sid, rollout, "UserPromptSubmit", prompt="again"))
    check("prompts emit", ok1 and ok2)
    check("turn counting", bool(ev1) and bool(ev2)
          and ev1[0]["body"] == "Turn 1 started"
          and ev2[0]["body"] == "Turn 2 started")

    # First prompt on a FRESH session (no custom name, EMPTY rollout): the
    # hook payload's own prompt becomes the title immediately — the rollout
    # does not contain the prompt yet when the hook fires (observed 0.154.0).
    fresh_root = root / "fresh"
    fresh_home = fresh_root / "codexhome"
    fresh_sessions = fresh_home / "sessions" / "2026" / "09" / "12"
    fresh_sessions.mkdir(parents=True, exist_ok=True)
    fresh_rollout = fresh_sessions / "rollout-fresh.jsonl"
    fresh_rollout.write_text("", encoding="utf-8")
    os.environ["CODEX_HOME"] = str(fresh_home)
    ok, ev1 = step(
        payload_for("sess-fresh", str(fresh_rollout), "UserPromptSubmit",
                   prompt="Fix the login bug")
    )
    check("first prompt title comes from the payload prompt, not the empty rollout",
          bool(ev1) and ev1[0]["title"] == "Fix the login bug"
          and ev1[0]["body"] == "Turn 1 started")

    # A wrapped first prompt still yields a clean title.
    ok, ev2 = step(
        payload_for("sess-fresh2", str(fresh_rollout), "UserPromptSubmit",
                    prompt="<environment_context>\nnoise\n</environment_context>\nSecond prompt here")
    )
    check("control wrappers stripped from a prompt-derived title",
          bool(ev2) and ev2[0]["title"] == "Second prompt here")
    os.environ["CODEX_HOME"] = str(home)

    # PostToolUse with an unchanged title emits nothing (no inbox spam).
    ok, events = step(payload_for(sid, rollout, "PostToolUse"))
    check("PostToolUse silent when title unchanged", ok is False and not events)

    # PostToolUse with a mid-turn rename (a new latest index entry) emits.
    with (home / "session_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": sid, "thread_name": "Renamed mid-turn", "updated_at": "t3"}) + "\n")
    ok, events = step(payload_for(sid, rollout, "PostToolUse"))
    check("PostToolUse reports a mid-turn rename", ok and len(events) == 1)
    check("mid-turn rename title", bool(events) and events[0]["title"] == "Renamed mid-turn")

    # Stop: turn-complete body with the count.
    ok, events = step(payload_for(sid, rollout, "Stop"))
    check("Stop body counts turns", ok and bool(events)
          and events[0]["body"] == "Turn complete · 2 turns")

    # SessionEnd.
    ok, events = step(payload_for(sid, rollout, "SessionEnd", reason="other"))
    check("SessionEnd emitted", ok and bool(events) and events[0]["body"] == "Session ended")

    # Clear: state reset (turn counter back to 0) and placeholder title.
    ok, events = step(payload_for(sid, rollout, "SessionStart", source="clear"))
    check("clear reports with placeholder title", ok and bool(events)
          and events[0]["title"] == report.PLACEHOLDER_TITLE)
    step(payload_for(sid, rollout, "UserPromptSubmit", prompt="fresh"))
    ok, events = step(payload_for(sid, rollout, "Stop"))
    check("clear resets the turn counter", bool(events)
          and events[0]["body"] == "Turn complete · 1 turn")

    # Compact mid-session: changed-title path only, never a resume body.
    ok, events = step(payload_for(sid, rollout, "SessionStart", source="compact"))
    check("compact takes the changed-title path", ok is False and not events)

    # Opt-out env: no notification at all.
    os.environ["CODEX_ARCHIVER_NO_NOTIFY"] = "1"
    ok, events = step(payload_for(sid, rollout, "UserPromptSubmit", prompt="quiet"))
    check("CODEX_ARCHIVER_NO_NOTIFY suppresses emission", ok is False and not events)
    os.environ.pop("CODEX_ARCHIVER_NO_NOTIFY", None)

    fake.restore()


# ── 3. session generation guard (abandoned threads must not rename) ────────

def test_generation_guard(root: Path) -> None:
    """`/new` and `/clear` abandon a thread, yet codex still fires that
    thread's SessionEnd later — carrying the same tmux context and resolving
    to the DEAD conversation's title. Each turn records its session id per
    pane, and a non-current generation's SessionEnd is dropped."""
    g_root = root / "generation"
    g_root.mkdir()
    home, rollout = make_home(g_root, "sess-gen")
    data = g_root / "plugindata" / "state"
    data.mkdir(parents=True)
    fake = FakeTty(g_root)
    os.environ["CODEX_HOME"] = str(home)
    os.environ["PLUGIN_DATA"] = str(data)
    os.environ.pop("TMUX", None)
    os.environ.pop("CODEX_ARCHIVER_NO_NOTIFY", None)

    def emit_once(session_id: str, event: str, **extra: object) -> list[dict]:
        fake.file.write_text("")
        report.report(payload_for(session_id, str(rollout), event, **extra))
        return decode_osc(fake.read_all())

    # No tmux (the selftest has none): the guard is inert, the pre-guard
    # behaviour, and inert must mean "allow", never "block".
    check("without a tmux pane the guard is inert",
          report.is_current_generation("anything") is True)

    real_name = report._tmux_session_name
    report._tmux_session_name = lambda: "gt-gen"  # type: ignore[assignment]
    try:
        key = report._generation_key("gt-gen")
        check("nothing recorded yet: the guard is inert",
              report.is_current_generation("sess-1") is True)

        report.mark_current_generation("sess-1")
        check("the marking session is current",
              report.is_current_generation("sess-1") is True)
        check("anything else is not current",
              report.is_current_generation("sess-0") is False)

        # A turn marks the generation through report() itself.
        report._atomic_write(report._current_session_file(key), "old")
        report.report(payload_for("sess-new", str(rollout), "UserPromptSubmit", prompt="hi"))
        check("a turn records its session as the pane's current one",
              report.is_current_generation("sess-new") is True
              and report.is_current_generation("old") is False)

        # The dead thread's SessionEnd must stay silent; the live one must not.
        check("an abandoned thread's SessionEnd emits nothing",
              not emit_once("old", "SessionEnd", reason="other"))
        check("the current session's SessionEnd still emits",
              bool(emit_once("sess-new", "SessionEnd", reason="other")))

        # SessionEnd must never MARK, or a stale thread could make itself
        # current and rename the record on its way out.
        report.report(payload_for("stale", str(rollout), "SessionEnd", reason="other"))
        check("SessionEnd never marks a generation",
              report.is_current_generation("sess-new") is True
              and report.is_current_generation("stale") is False)
    finally:
        report._tmux_session_name = real_name  # type: ignore[assignment]
        fake.restore()


# ── 4. end-to-end through a real pty (the detached-hook shape) ─────────────

def test_pty_e2e(root: Path) -> None:
    e2e_root = root / "e2e"
    e2e_root.mkdir()
    home, rollout = make_home(e2e_root, "sess-e2e")
    master_fd, slave_fd = os.openpty()
    # Raw mode: the pty must not mangle or swallow the escape sequence.
    attrs = termios.tcgetattr(slave_fd)
    attrs[3] &= ~termios.ECHO  # lflag: no echo — we assert exact bytes
    termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
    env = dict(os.environ)
    env.update(
        {
            "CODEX_HOME": str(home),
            "PLUGIN_DATA": str(e2e_root / "plugindata" / "state"),
        }
    )
    env.pop("TMUX", None)
    env.pop("CODEX_ARCHIVER_NO_NOTIFY", None)
    payload = json.dumps(
        payload_for("sess-e2e", rollout, "UserPromptSubmit", prompt="via pty")
    ).encode()
    # Drain the master concurrently. The hook writes the sequence to its
    # controlling tty, and a pty whose master nobody reads blocks that write
    # once the output queue fills — which on macOS is well under one OSC
    # sequence's worth of bytes, hanging report.py until the timeout.
    buf = bytearray()

    def drain_master() -> None:
        while b"\033\\" not in buf:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            buf.extend(chunk)

    reader = threading.Thread(target=drain_master, daemon=True)
    reader.start()
    try:
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "report.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            preexec_fn=lambda: (os.setsid(), fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)),
            pass_fds=(slave_fd,),
            close_fds=True,
        )
        out, err = proc.communicate(payload, timeout=20)
    except (AttributeError, OSError):
        # TIOCSCTTY is not portable everywhere; skip rather than fail.
        os.close(master_fd)
        os.close(slave_fd)
        print("skip  pty e2e (no TIOCSCTTY on this platform)")
        return
    check("report.py exits 0", proc.returncode == 0, f"rc={proc.returncode} err={err!r}")
    check("report.py prints nothing on stdout (hook stdout is model context)", out == b"", repr(out[:120]))
    os.close(slave_fd)  # EOF for the reader; the child is already gone
    reader.join(timeout=5)
    os.close(master_fd)
    raw = bytes(buf).decode("utf-8", errors="replace")
    events = decode_osc(raw)
    check("pty e2e: one OSC 9999 sequence captured", len(events) == 1, repr(raw[:200]))
    ev = events[0] if events else {}
    check("pty e2e: contract fields", ev.get("source") == report.SOURCE
          and ev.get("sourceId") == "sess-e2e" and ev.get("event") == "TitleChanged"
          and ev.get("title") == "My renamed chat" and ev.get("v") == 1
          and ev.get("magic") == notify.MAGIC, json.dumps(ev)[:200])

    # Hostile inputs never raise: garbage stdin, missing session id, and a
    # payload whose transcript_path does not exist.
    for hostile in [b"", b"not json", b"{}", json.dumps({"session_id": "x", "hook_event_name": "Stop"}).encode()]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "report.py")],
            input=hostile,
            capture_output=True,
            env=env,
            timeout=20,
        )
        check(f"hostile input {hostile[:20]!r} exits 0",
              proc.returncode == 0 and proc.stdout == b"")

    # tmux passthrough wrapper: ESC doubled inside the DCS envelope.
    seq = notify.OSC_PREFIX + "AAA" + notify.ST
    wrapped = notify._wrap_for_tmux(seq)
    check("tmux wrap: DCS envelope with doubled ESC",
          wrapped.startswith("\033Ptmux;") and "\033\033]9999;" in wrapped and wrapped.endswith("\033\\"))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="codex-archiver-selftest-") as tmp:
        root = Path(tmp)
        test_title_resolution(root)
        test_events(root)
        test_generation_guard(root)
        test_pty_e2e(root)
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s):")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
