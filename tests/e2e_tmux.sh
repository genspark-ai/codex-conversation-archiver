#!/usr/bin/env bash
# Manual smoke test for the codex conversation-archiver plugin's tmux path:
# run report.py inside a REAL tmux pane and capture what an attached client
# receives on its stdout. notify.py wraps the OSC 9999 sequence in tmux's DCS
# passthrough envelope (ESC doubled inside) because tmux would otherwise drop
# the unknown sequence; tmux then UNWRAPS the envelope and forwards the inner
# OSC 9999 to the attached client — which is what a GenTerminal tab's xterm
# parser sees. This test pins both ends of that chain.
#
# ISOLATION: everything runs on a PRIVATE tmux socket (-L) — its own server
# process, its own socket under /tmp/tmux-$(id -u)/. The default tmux server
# (which on dev machines holds real work sessions) is never contacted, and
# the cleanup kill-server only ever sees this test's own server.
#
# No dev build needed; requires tmux + python3 + script(1). Run from the repo root:
#   bash plugins/codex-conversation-archiver/tests/e2e_tmux.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PLUGIN="$REPO_ROOT/plugins/codex-conversation-archiver/plugins/conversation-archiver"
WORK="$(mktemp -d /tmp/codex-archiver-e2e-XXXX)"
SESSION="gt-e2e-$$"
SOCKET="codex-archiver-e2e-$$"
tmux() { command tmux -L "$SOCKET" "$@"; }
trap 'tmux kill-server 2>/dev/null || true; rm -rf "$WORK"' EXIT

# Fixture CODEX_HOME: a custom thread name (Rename chat) + a rollout whose
# first real user message is ignored because the custom name outranks it.
mkdir -p "$WORK/home/sessions/2026/09/12"
cat > "$WORK/home/sessions/2026/09/12/rollout-test.jsonl" <<'EOF'
{"type":"session_meta","payload":{"session_id":"e2e-sess","cwd":"/srv"}}
{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Fix the login bug"}]}}
EOF
cat > "$WORK/home/session_index.jsonl" <<'EOF'
{"id":"e2e-sess","thread_name":"E2E renamed chat","updated_at":"t1"}
EOF

PAYLOAD='{"session_id":"e2e-sess","transcript_path":"'"$WORK"'/home/sessions/2026/09/12/rollout-test.jsonl","cwd":"/srv","hook_event_name":"UserPromptSubmit"}'

# 1. Detached session on the PRIVATE socket. The pane WAITS first: tmux drops
#    passthrough written while no client is attached, so the client (step 2)
#    must already be receiving when report.py emits.
tmux new-session -d -s "$SESSION" \
  "sleep 3; echo '$PAYLOAD' | CODEX_HOME='$WORK/home' PLUGIN_DATA='$WORK/data' python3 '$PLUGIN/scripts/report.py'; sleep 5"

# 2. Attach through a pty (script(1)) with the client's stdout piped to a
#    file — tmux attach refuses to run without a terminal, and the forwarded
#    sequence must land in the CLIENT's output, which is exactly what a
#    GenTerminal tab holds.
script -q -e -c "command tmux -L '$SOCKET' attach-session -t '$SESSION'" /dev/null > "$WORK/client.out" 2>/dev/null &
CLIENT=$!
wait "$CLIENT" 2>/dev/null || true
tmux kill-server 2>/dev/null || true

# 3. Decode from the raw client bytes. tmux unwraps the DCS passthrough, so
#    the client stream carries the inner OSC 9999 directly.
python3 - "$WORK/client.out" "$SESSION" <<'PYEOF'
import base64, json, re, sys

raw = open(sys.argv[1], "rb").read().decode("utf-8", errors="replace")
session = sys.argv[2]

# The client must see the unwrapped OSC 9999 (the whole point of the DCS
# envelope the emitter wraps it in).
o = re.search(r"\033\]9999;([A-Za-z0-9+/=]+)\033\\", raw)
assert o, "no OSC 9999 sequence in the attached client's output:\n" + repr(raw[:400])
payload = json.loads(base64.b64decode(o.group(1)))

assert payload["magic"] == "genterm-notify", payload
assert payload["source"] == "codex-conversation-archiver", payload
assert payload["sourceId"] == "e2e-sess", payload
assert payload["title"] == "E2E renamed chat", payload
assert payload["body"] == "Turn 1 started", payload
tmux_ctx = payload.get("tmux") or {}
assert tmux_ctx.get("session") == session, payload

print("e2e ok: OSC 9999 survived the tmux passthrough to the client")
print("  title: " + payload["title"])
print("  body:  " + payload["body"])
print("  tmux session: " + str(tmux_ctx.get("session")))
PYEOF
