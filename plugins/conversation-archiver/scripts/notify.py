#!/usr/bin/env python3
"""GenTerminal app-notification emitter (OSC 9999 `genterm-notify`).

Writes the escape sequence to the controlling tty (or the tmux pane's tty, or
an ancestor's) so the notification rides the terminal's own data stream back
to the GenTerminal tab that owns it — no network port, token, or reverse
tunnel. Adapted from the cc-conversation-archiver emitter of the same wire
protocol (GenTerminal's utils/osc.ts parses it):

    ESC ] 9999 ; <base64(JSON)> ST

where JSON is {v, magic:"genterm-notify", source, sourceId, event, title,
body, tmux?} and tmux = {socket, session, windowId, windowIndex, windowName}
captured via `tmux display-message` — GenTerminal's sidebar joins the record
on `tmux.session == record.tmux_name`.

Every call is best-effort: with no resolvable tty (or a tmux passthrough that
cannot be arranged) it silently does nothing and never raises. Codex hook
commands run detached from the controlling terminal, so tty resolution goes:
tmux pane tty first (hooks always carry TMUX in the env snapshot when codex
itself runs under tmux), then /dev/tty, then an ancestor's tty.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess

OSC_PREFIX = "\033]9999;"
ST = "\033\\"
MAGIC = "genterm-notify"


def tmux_context() -> dict | None:
    """Capture the current tmux socket/session/window so GenTerminal can
    switch to it on click. Returns None when not running under tmux, the
    binary is missing, or the query fails — the field is then simply
    omitted."""
    if not os.environ.get("TMUX"):
        return None
    fmt = "#{socket_path}\t#S\t#{window_id}\t#{window_index}\t#{window_name}"
    try:
        res = subprocess.run(
            ["tmux", "display-message", "-p", fmt],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    parts = res.stdout.rstrip("\n").split("\t")
    if len(parts) < 5:
        return None
    socket, session, window_id, window_index, window_name = parts[:5]
    ctx: dict = {}
    if socket:
        ctx["socket"] = socket
    if session:
        ctx["session"] = session
    if window_id:
        ctx["windowId"] = window_id
    if window_index.isdigit():
        ctx["windowIndex"] = int(window_index)
    if window_name:
        ctx["windowName"] = window_name
    return ctx or None


def _wrap_for_tmux(seq: str) -> str:
    """Wrap an escape sequence in tmux's DCS passthrough so it reaches the
    outer terminal instead of being swallowed by tmux. Every ESC inside the
    payload must be doubled."""
    inner = seq.replace("\033", "\033\033")
    return "\033Ptmux;" + inner + "\033\\"


def _target_tty() -> str | None:
    """Best terminal device to write the sequence to.

    Resolution order:
      1. tmux: the current pane's tty (``#{pane_tty}``). Writing there feeds
         tmux's pane output, which forwards via passthrough to the attached
         client — works even with no controlling terminal.
      2. /dev/tty, when we actually have a controlling terminal.
      3. the controlling tty of an ancestor process, for a detached process
         in a non-tmux terminal.
    Returns the device path, or None if none could be resolved.
    """
    if os.environ.get("TMUX"):
        try:
            r = subprocess.run(
                ["tmux", "display-message", "-p", "#{pane_tty}"],
                capture_output=True, text=True, timeout=5,
            )
            t = r.stdout.strip()
            if t:
                return t
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
    try:
        fd = os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY)
        os.close(fd)
        return "/dev/tty"
    except OSError:
        pass
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            break
        try:
            tty = subprocess.run(
                ["ps", "-o", "tty=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if tty and tty not in ("??", "?", "-"):
                return tty if tty.startswith("/dev/") else "/dev/" + tty
            pid = int(
                subprocess.run(
                    ["ps", "-o", "ppid=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                or "1"
            )
        except (FileNotFoundError, subprocess.SubprocessError, ValueError):
            break
    return None


def emit(source: str, source_id: str, event: str, title: str,
         body: str = "", tmux: dict | None = None,
         target_tty: str | None = None) -> bool:
    """Emit one notification to the terminal. Returns True if the sequence
    was written, False otherwise (no usable tty, write error). Never raises.

    `target_tty` overrides the device resolution for fully detached callers
    that resolved a tty while they still could."""
    if not source or not source_id or not title:
        return False
    payload: dict = {
        "v": 1,
        "magic": MAGIC,
        "source": source,
        "sourceId": source_id,
        "event": event,
        "title": title,
        "body": body,
    }
    if tmux:
        payload["tmux"] = tmux
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    seq = OSC_PREFIX + base64.b64encode(raw).decode("ascii") + ST

    if os.environ.get("TMUX"):
        # tmux drops unknown OSC sequences unless passthrough is enabled and
        # the sequence is wrapped in its DCS passthrough envelope. Enabling is
        # best-effort (and pane-scoped); the wrap is required.
        try:
            subprocess.run(
                ["tmux", "set", "-p", "allow-passthrough", "on"],
                capture_output=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
        seq = _wrap_for_tmux(seq)

    target = target_tty or _target_tty()
    if not target:
        return False
    try:
        # O_NOCTTY: never let writing to a tty make it our controlling
        # terminal.
        fd = os.open(target, os.O_WRONLY | os.O_NOCTTY)
        try:
            os.write(fd, seq.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except OSError:
        return False
