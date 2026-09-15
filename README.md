# codex-conversation-archiver

A [Codex CLI](https://developers.openai.com/codex/cli) plugin that reports the
Codex session's **execution status** and **title** to
[GenTerminal](https://genterminal.ai) over OSC 9999 (`genterm-notify`) — the
Codex counterpart of [cc-conversation-archiver](https://github.com/genspark-ai/cc-conversation-archiver)'s
status channel for Claude Code.

This directory is the marketplace root, published as
[genspark-ai/codex-conversation-archiver](https://github.com/genspark-ai/codex-conversation-archiver)
(the tmux-session bootstrap installs it with
`codex plugin marketplace add genspark-ai/codex-conversation-archiver`) —
this tree is the origin copy; keep the two in sync when touching the plugin:

```
.claude-plugin/marketplace.json                       marketplace manifest (Codex reads the Claude layout)
plugins/conversation-archiver/.codex-plugin/plugin.json
plugins/conversation-archiver/hooks/hooks.json         lifecycle hooks (SessionStart / UserPromptSubmit / PostToolUse / Stop / SessionEnd)
plugins/conversation-archiver/scripts/report.py        status + title reporting (hook-driven)
plugins/conversation-archiver/scripts/title_watch.py   detached per-session title watcher (the /rename immediacy fix)
plugins/conversation-archiver/scripts/notify.py        OSC 9999 emitter + tmux context (wire contract of GenTerminal's utils/osc.ts)
plugins/conversation-archiver/tests/selftest.py       stdlib-only selftest (also run from CI via the Jest wrapper in the terminal repo)
```

## What it reports

Every event pushes at most ONE notification
`{v, magic:"genterm-notify", source:"codex-conversation-archiver",
sourceId:<session id>, event:"TitleChanged", title, body, tmux}`:

| Hook            | `body`                                | Notes |
|-----------------|---------------------------------------|-------|
| SessionStart    | `Session started` / `Session resumed` | heal path: an unchanged real title is re-reported so a restarted app / stale other-device record converges |
| SessionStart (`clear`) | `Session cleared`               | resets the turn counter; the new conversation's first title takes over on the next prompt |
| UserPromptSubmit| `Turn N started`                      | counts turns per session |
| PostToolUse     | only on a title change                | a mid-turn Rename chat or the first derived title; plain tool activity never emits (no inbox spam) |
| Stop            | `Turn complete · N turns`             | |
| SessionEnd      | `Session ended`                       | suppressed for an abandoned thread (`/new`, `/clear`) — it would carry the dead conversation's stale title |
| title_watch daemon | `Session renamed`                  | an IDLE Rename chat, within ~1.5 s of the write; see below |

## Title resolution (same order as `codex resume`)

1. the user's explicit **Rename chat** name — `$CODEX_HOME/session_index.jsonl`,
   append-only, latest entry for the thread id wins (the same rule as
   codex-rs `find_thread_name_by_id`);
2. the transcript's **first real user message** — codex's own fallback-title
   derivation: strip control wrappers (`<environment_context>`,
   `<user_instructions>`, …), first non-empty line, 120-char cap. On
   `UserPromptSubmit` of the FIRST turn the prompt is not in the rollout
   yet (observed against 0.154.0), so the hook payload's own `prompt`
   field is used — the real title reaches the sidebar immediately instead
   of one turn boundary late;
3. the fixed placeholder **`Codex`** — GenTerminal filters placeholder titles,
   so a fresh session never clobbers the user's chosen record name.

GenTerminal's sidebar Sessions section joins on
`payload.tmux.session == record.tmux_name` (consent: the record was created
with "Launch Codex") and renames the record + its live tab to the reported
title; every report also lights the unread dot and refreshes last-activity
sorting when the tab is in the background.

## Properties

- **Never blocks the agent**: hooks are python3-gated, `report.py` prints
  NOTHING on stdout (hook stdout becomes model context on several events),
  always exits 0, every failure degrades to silence. Short per-handler
  timeouts; PostToolUse runs async.
- **stdlib-only Python 3** — no dependencies to install.
- **tmux-aware**: the sequence rides tmux's DCS passthrough, so it works in
  the panes GenTerminal manages (that is the primary deployment).
- Opt out with `CODEX_ARCHIVER_NO_NOTIFY=1`; opt out of the watcher daemon
  alone with `CODEX_ARCHIVER_NO_WATCHER=1`.

## The `/rename` watcher (why a daemon exists)

`/rename` (and `/clear` / `/new`) fire NO hook — verified against codex-rs
(`HookEventName` has no rename variant) and live against codex-cli 0.154.0
(`session_index.jsonl` gains the new name with zero hook invocations). So the
hook-driven reporter can only deliver an idle rename on the NEXT hook event,
i.e. the next prompt — which is exactly when a human renames.

`report.ensure_watcher()` therefore spawns one detached `title_watch.py` per
session from the ordinary hook runs (SessionStart / UserPromptSubmit / Stop).
It polls `session_index.jsonl` every 1.5 s and pushes a name change through the
same `notify.emit` + `<session>.title` marker path the hook reporter uses, so
the two never double-report. POSIX only (`kill(0)` liveness); on Windows the
hook cadence stays the only reporter.

Because the hook that spawned it will not run again when the session ends, the
daemon is handed every signal it needs to stop itself, and it exits on the
first one that fires:

| anchor | exits when | measured |
|--------|-----------|----------|
| SessionEnd | codex ends its main thread (`/quit`) → the hook SIGTERMs the daemon | instant |
| tmux pane | the pane is gone (tab closed, session destroyed) | ~4 s |
| codex process | pid **+ start time** probe fails 4 polls in a row — the start time is what stops a RECYCLED pid from faking liveness | ~22 s |
| superseded | another session became current for this pane (`/new`, `/clear`) | ~1.5 s |
| lifetime | backstop: 7 days with an identified codex process or pane, **1 hour** without either (no liveness signal at all, and any later hook run respawns it) | — |

Every spawn and every exit appends a line to
`<session>.watch.log` in the plugin data dir (`start pid:… codex_pid:…
lifetime:…` / `exit reason:…`) — the only way to tell a daemon that died from
one that never started, since everything else in this plugin degrades to
silence.

`/clear` and `/new` still cannot be reflected the instant they run: codex
writes neither a rollout file nor an index entry until the new conversation's
first turn, so there is no signal to watch. Both update the record at that
first turn (the new prompt's title), and the previous thread's later
SessionEnd is suppressed so it cannot rename the record back.

## Hook trust

Codex requires hook definitions to be trusted interactively (`/hooks`).
GenTerminal's tmux bootstrap launches codex with
`--dangerously-bypass-hook-trust` — the documented escape hatch for
automation that vets its hook sources outside Codex; here the vetting
authority is the app itself, which installs this plugin from a pinned
marketplace.

## Testing

```
python3 plugins/conversation-archiver/tests/selftest.py
```

Covers title resolution, event semantics/turn counting, the watcher daemon
(index-name report, de-duplication with the hook reporter, supersede /
generation / liveness exits, spawn-stop lifecycle), the OSC 9999 wire contract
captured from a real pty (the detached-hook shape), and hostile inputs. The
terminal repo runs the same selftest from Jest
(`__tests__/codexArchiverPlugin.test.ts`).
