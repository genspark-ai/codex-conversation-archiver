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
- Opt out with `CODEX_ARCHIVER_NO_NOTIFY=1`.

## When a rename reaches GenTerminal

`/rename` — and `/clear` and `/new` — fire NO hook. Verified against codex-rs
(`HookEventName` has no rename variant) and live against codex-cli 0.154.0,
where `session_index.jsonl` gains the new name with zero hook invocations, and
`/new` produces no rollout file or index entry until the new conversation's
first turn.

Titles resolve custom-first (`custom_thread_name`), so a rename lands on the
session's NEXT hook event:

| what you did | delivered by | when |
|---|---|---|
| renamed, then sent a prompt | `UserPromptSubmit` (after `SessionStart`) | at that prompt |
| renamed while a turn was running | `Stop` | at the end of that turn |
| renamed, then quit codex | `SessionEnd` | at exit |
| renamed an idle session and left it untouched | — | next time the session does anything |

0.2.0–0.2.2 closed the last row with a detached polling daemon
(`title_watch.py`). 0.3.0 removed it: the case that needs it most — a rename
in a conversation that has not had a turn yet — writes an index entry carrying
only `{id, thread_name, updated_at}`, with no rollout file, no cwd and no pane,
so nothing can attribute that name to a managed record, while a daemon per
pane still cost a process, per-hook `tmux` round trips and a pile of state.
The accepted behaviour is the table above.

## Abandoned threads (`/new`, `/clear`)

Codex keeps firing the abandoned thread's hooks after `/new` or `/clear` —
its `SessionEnd` for certain, at `/quit` or when the TUI finalizes it. That
payload carries the SAME tmux context as the live session and resolves to the
dead conversation's own title, so emitting it would rename the managed record
back to a conversation that no longer exists. Each turn records its session id
in `current-<pane key>` (the tmux session name hashed), and a `SessionEnd`
whose id is not the current one is dropped. With no tmux, or nothing recorded
yet, the guard is inert — the behaviour that preceded it.

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

Covers title resolution, event semantics/turn counting, the generation guard
(an abandoned thread's SessionEnd must not rename, and must be inert without
tmux), the OSC 9999 wire contract captured from a real pty (the detached-hook
shape), and hostile inputs. The
terminal repo runs the same selftest from Jest
(`__tests__/codexArchiverPlugin.test.ts`).
