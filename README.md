# codex-conversation-archiver

A [Codex CLI](https://developers.openai.com/codex/cli) plugin that reports the
Codex session's **execution status** and **title** to
[GenTerminal](https://genterminal.ai) over OSC 9999 (`genterm-notify`) — the
Codex counterpart of [cc-conversation-archiver](https://github.com/genspark-ai/cc-conversation-archiver)'s
status channel for Claude Code.

This directory is the marketplace root, laid out to be pushed as-is to a
public repo (the tmux-session bootstrap installs it with
`codex plugin marketplace add genspark-ai/codex-conversation-archiver`):

```
.claude-plugin/marketplace.json                       marketplace manifest (Codex reads the Claude layout)
plugins/conversation-archiver/.codex-plugin/plugin.json
plugins/conversation-archiver/hooks/hooks.json         lifecycle hooks (SessionStart / UserPromptSubmit / PostToolUse / Stop / SessionEnd)
plugins/conversation-archiver/scripts/report.py        status + title reporting
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
| SessionEnd      | `Session ended`                       | |

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

## Known gap (v1)

Codex fires NO hook on Rename chat, so a rename made while the session is
IDLE reaches GenTerminal on the next hook event (next prompt / turn
boundary), not within seconds. The Claude plugin closes this gap with a
polling watcher daemon; a codex twin needs a process-liveness signal the
hook payload does not carry yet.

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

Covers title resolution, event semantics/turn counting, the OSC 9999 wire
contract captured from a real pty (the detached-hook shape), and hostile
inputs. The terminal repo runs the same selftest from Jest
(`__tests__/codexArchiverPlugin.test.ts`).
