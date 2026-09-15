#!/usr/bin/env python3
"""Per-session title watcher daemon — the `/rename` immediacy fix for Codex.

`/rename` fires NO hook. Verified two ways: codex-rs `HookEventName` has no
rename variant (`PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`,
`PostCompact`, `SessionStart`, `SessionEnd`, `UserPromptSubmit`,
`SubagentStart`, `SubagentStop`, `Stop`, `Interrupt` only), and a live capture
of codex-cli 0.154.0 during `/rename mychat` showed
`$CODEX_HOME/session_index.jsonl` gaining the new name with ZERO hook
invocations. So the hook-driven reporter (report.py) only delivers a manual
rename on the NEXT hook event — the next prompt or turn boundary — and the
session is idle exactly when humans rename (the same 2026-07-30 report the cc
archiver's watcher closes for Claude Code).

This daemon closes the idle gap. `report.ensure_watcher()` spawns one detached
instance per session from the ordinary hook runs; the daemon polls
`$CODEX_HOME/session_index.jsonl` (codex's append-only thread-name index — the
same source `report.custom_thread_name` reads, and where both a manual
`/rename` and codex's own auto-generated thread name land) and pushes a change
through the same `notify.emit` + `<session>.title` marker path the hook
reporter uses. The two paths therefore never double-report: a lost race
re-emits an identical notification consumers already treat as a no-op.

Cheap by construction: each tick stats one file; only a changed fingerprint
re-reads and rescans the (append-only, reverse-scanned) index.

Exit conditions (all polled, no signals):
  - superseded: the pidfile no longer names this process (a newer watcher won
    a spawn race — newest wins, we bow out);
  - superseded generation: another session id became the current one for this
    pane (`/new`, `/clear`) — that hook run spawned its own watcher, so one
    pane holds one poller instead of one per abandoned thread;
  - SessionEnd: report.py kills the watcher and drops the pidfile when the
    session's main thread ends (the normal `/quit` path);
  - codex exited: the codex process captured at spawn fails PID_MISS_LIMIT
    consecutive probes. The probe is pid + START TIME, so a recycled pid
    cannot fake liveness; an unverifiable probe counts as alive;
  - pane gone: the tmux pane the emitter ran in no longer exists (tab closed,
    session destroyed) — this needs no pid at all;
  - lifetime: a backstop for whatever is left — MAX_LIFETIME when the codex
    process or the pane could be identified, else UNVERIFIED_LIFETIME (an
    hour), because with no liveness signal at all the daemon must not outlive
    the session by much. Expiry is harmless: the next hook run respawns it,
    and a rename issued with no watcher alive still lands at the next prompt.

Every spawn and every exit writes a line to `<session>.watch.log` (see
report._watch_logfile) — the rest of this plugin degrades to silence, so that
line is the only way to tell a dead watcher from one that never started.

POSIX only — the liveness probe is `os.kill(pid, 0)`, which on Windows would
TERMINATE the target instead of probing it. ensure_watcher never spawns on
non-POSIX, where the hook cadence remains the only reporter.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import notify  # noqa: E402  (sibling module; stdlib-only)
import report  # noqa: E402

POLL_SECONDS = 1.5
# Liveness is a kill(0) probe, heavier than the stat fingerprint, so it runs
# every LIVENESS_EVERY ticks (~6s). Exit needs PID_MISS_LIMIT consecutive
# misses (~24s) so a transient probe failure never kills a healthy watcher.
LIVENESS_EVERY = 4
PID_MISS_LIMIT = 4
MAX_LIFETIME_SECONDS = 7 * 24 * 3600
# No codex process identity AND no pane id: nothing left to poll, so the only
# bound is time. Kept short deliberately — any later hook run respawns the
# daemon, so expiring early costs at most the instant delivery of a rename
# issued while none is running (it still lands at the next prompt).
UNVERIFIED_LIFETIME_SECONDS = 3600

BODY = "Session renamed"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _fingerprint(index: Path) -> tuple | None:
    """Change detector for the append-only session index: (mtime_ns, size).
    Stat-only — an unchanged tick parses nothing. Any rename (this session's
    or another's) appends, so both move it."""
    try:
        st = index.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


class Watch:
    """One session's watch state; `tick()` is the whole per-poll step, factored
    off the sleep loop so the selftest can drive it synchronously."""

    def __init__(
        self,
        session_id: str,
        codex_home: Path,
        codex_pid: int = 0,
        key: str = "",
        codex_start: str = "",
        pane: str = "",
    ) -> None:
        self.session_id = session_id
        self.codex_home = codex_home
        self.codex_pid = codex_pid
        self.key = key
        self.codex_start = codex_start
        self.pane = pane
        self.index = codex_home / "session_index.jsonl"
        self.last_fp: tuple | None = None
        self.ticks = 0
        self.pid_misses = 0

    def log(self, message: str) -> None:
        """Append one line to the session's watcher log (report.py owns the
        writer). Best-effort — the log must never make the daemon fail."""
        report._watch_log_append(self.session_id, message)

    def _is_current(self) -> bool:
        """False once ensure_watcher announces a DIFFERENT session for this
        pane (`/new`, `/clear`) — that run spawned its own watcher. A missing
        file keeps us running: the spawner may predate the generation file."""
        if not self.key:
            return True
        try:
            current = report._watch_current_file(self.key).read_text(
                encoding="utf-8").strip()
        except OSError:
            return True
        return not current or current == self.session_id

    def _codex_alive(self) -> bool:
        """Whether the codex process this daemon belongs to is still there.

        pid alone is not enough — pids are recycled, and a dead codex whose
        number a later process took would keep this daemon (and its poll) alive
        indefinitely — so the start time captured at spawn is compared too. An
        UNKNOWN current start time (probe failed) counts as alive: a probe
        outage must never kill a healthy watcher."""
        if not self.codex_pid:
            return True
        if not alive(self.codex_pid):
            return False
        if not self.codex_start:
            return True
        current = report.process_start(self.codex_pid)
        return not current or current == self.codex_start

    def _target_tty(self) -> str | None:
        """Freshest tty recorded by the hook runs (report refreshes
        `<session>.tty` on every invocation). Re-read before each emit — the
        spawn-time value may be missing (an early SessionStart could not
        resolve one) and a later hook run may have filled it in. Under tmux,
        emit()'s own resolution still works from the inherited $TMUX env, so
        None degrades fine; outside tmux this file is the daemon's only
        usable target."""
        try:
            raw = report._watch_ttyfile(self.session_id).read_text(
                encoding="utf-8").strip()
            return raw or None
        except OSError:
            return None

    def _owns_pidfile(self) -> bool:
        try:
            raw = report._watch_pidfile(self.session_id).read_text(
                encoding="utf-8").strip()
            return int(raw) == os.getpid()
        except (OSError, ValueError):
            # Missing/garbled pidfile: keep running — the spawner recreates it
            # and a stale watcher still exits via the liveness check.
            return True

    def _report_once(self) -> None:
        title = report.custom_thread_name(self.codex_home, self.session_id)
        if not title or title == report._read_text(report._title_file(self.session_id)):
            return
        emitted = notify.emit(
            source=report.SOURCE,
            source_id=self.session_id,
            event="TitleChanged",
            title=title,
            body=BODY,
            tmux=notify.tmux_context(),
            target_tty=self._target_tty(),
        )
        if emitted:
            report._atomic_write(report._title_file(self.session_id), title)
        else:
            # No usable tty this tick — clear the fingerprint so the next tick
            # retries instead of waiting for another source change.
            self.last_fp = None

    def tick(self) -> str | None:
        """One poll step. Returns an exit reason, or None to keep running."""
        if not self._owns_pidfile():
            return "superseded"
        if not self._is_current():
            return "superseded-generation"
        self.ticks += 1
        if self.ticks % LIVENESS_EVERY == 0:
            if not self._codex_alive():
                self.pid_misses += 1
                if self.pid_misses >= PID_MISS_LIMIT:
                    return "codex-exited"
            else:
                self.pid_misses = 0
            if not report.tmux_pane_alive(self.pane):
                return "pane-gone"
        fp = _fingerprint(self.index)
        if fp != self.last_fp:
            self.last_fp = fp
            self._report_once()
        return None


def run(
    session_id: str,
    codex_home: Path,
    codex_pid: int,
    key: str,
    codex_start: str = "",
    pane: str = "",
) -> None:
    watch = Watch(session_id, codex_home, codex_pid, key, codex_start, pane)
    # One identified anchor (the codex process, or the pane) is enough for the
    # long backstop — both are polled. With neither, time is the only bound.
    lifetime = (
        MAX_LIFETIME_SECONDS
        if (codex_pid and codex_start) or pane
        else UNVERIFIED_LIFETIME_SECONDS
    )
    report._atomic_write(report._watch_logfile(session_id), "")
    watch.log(
        f"start pid:{os.getpid()} session:{session_id} codex_pid:{codex_pid or '-'} "
        f"codex_start:{codex_start or '-'} key:{key or '-'} pane:{pane or '-'} "
        f"lifetime:{lifetime}s"
    )
    deadline = time.monotonic() + lifetime
    # Only a tick/signal reason lands here; a loop that runs out the deadline
    # must log "lifetime", not the last (None) keep-running tick result.
    reason: str | None = None
    try:
        while time.monotonic() < deadline:
            reason = watch.tick()
            if reason is not None:
                break
            time.sleep(POLL_SECONDS)
    except Exception as exc:  # noqa: BLE001  (never die without a trace)
        reason = f"exception:{exc!r}"
    finally:
        watch.log(f"exit reason:{reason or 'lifetime'}")
        # Best-effort: drop the pidfile only if it is still ours, so a
        # successor's file is never removed.
        try:
            pf = report._watch_pidfile(session_id)
            if int(pf.read_text(encoding="utf-8").strip()) == os.getpid():
                pf.unlink()
        except (OSError, ValueError):
            pass


def main() -> None:
    if os.name != "posix" or os.environ.get("CODEX_ARCHIVER_NO_NOTIFY") \
            or os.environ.get("CODEX_ARCHIVER_NO_WATCHER"):
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--codex-pid", type=int, default=0)
    parser.add_argument("--codex-start", default="")
    parser.add_argument("--key", default="")
    parser.add_argument("--pane", default="")
    args = parser.parse_args()
    run(
        args.session_id, Path(args.codex_home), args.codex_pid, args.key,
        args.codex_start, args.pane,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001  (a daemon never raises into anyone)
        pass
    sys.exit(0)
