# Evaluation: Simplifying with Jaato's WebSocket Client Plugin

## Current Architecture

The current `annotation_agent.py` (345 lines of custom Python) does two jobs:

1. **WebSocket client** — connects to the Hypothesis WebSocket, subscribes with
   a JSON filter, reads annotation events, reconnects on drops, and decouples
   reading from processing via an `asyncio.Queue`.

2. **Integration glue** — discovers source repos from `<link rel="vcs-git">`,
   builds prompts, posts replies via the Hypothesis REST API, and auto-approves
   tool permissions in a controlled environment.

The agent runs non-interactively — this is a headless daemon, not a
conversational assistant. The model should never request clarification; it works
with what it has.

## Architectural Feasibility

Jaato's architecture naturally supports a `ws_client` plugin:

- **Server-first design**: The Jaato daemon (`jaato-server`) already uses
  WebSocket as a transport layer (`server/websocket.py` on port 8080). A plugin
  for *outbound* WebSocket connections follows the same pattern — persistent
  connections managed by the server, surviving client disconnects.

- **Plugin auto-discovery**: The plugin registry (`shared/plugins/registry.py`)
  discovers plugins via entry points or directory scanning. A new `ws_client`
  plugin would be auto-wired with no manual registration. The registry supports
  `PARALLEL_INIT = True` for plugins that need network I/O at startup (exactly
  right for establishing WebSocket connections).

- **Event injection**: The `TaskEventBus` already supports `inject_prompt()`,
  which is how incoming WebSocket messages would be delivered to the agent as
  `ExternalEvent` instances. The SDK exposes `ExternalEvent` in
  `jaato_sdk.events` — the integration point is ready.

- **Profile-driven config**: Plugin configs are passed via profile JSON files
  (e.g., `.jaato/profiles/*.json`). The `plugin_configs` section supports
  environment variable substitution (`${H_API_TOKEN}`), connection parameters,
  and reconnect policies — all needed for a WebSocket client.

- **Preload support**: The `(preload)` suffix in the plugin list ensures the
  WebSocket connection is established before the first agent turn, so annotation
  events are captured immediately.

- **Headless mode**: The TUI client's `--headless` flag prevents the model
  from requesting clarification. The agent processes each annotation as a
  self-contained prompt/response turn. This is a client-side flag, not a
  profile setting.

The `ws_client` plugin is a proposed addition (see `docs/design/websocket-client-plugin.md`
in the Jaato repo). The existing plugin infrastructure requires no changes to
support it.

## What the ws_client Plugin Replaces

The `ws_client` plugin handles outbound persistent WebSocket connections
declaratively:

| Current custom code                | ws_client equivalent                         |
|------------------------------------|----------------------------------------------|
| `websockets.connect()` + auth      | `ws_connect` tool / profile config           |
| Subscribe filter (`ws.send(...)`)  | `initial_messages` capture + `ws_send`       |
| Reader loop + `asyncio.Queue`      | Reader thread → TaskEventBus → inject_prompt |
| Reconnect on `ConnectionClosed`    | `reconnect: true` in config                  |
| Message parsing / routing          | Handled in system prompt                     |

## Three Approaches

### Approach 1: Current `annotation_agent.py` (self-contained)

345 lines of Python. Manages everything: WebSocket lifecycle, repo discovery,
prompt building, REST API replies, permission auto-approval. No dependency on
the `ws_client` plugin.

**Pros:** Self-contained, battle-tested, no plugin dependency.
**Cons:** Significant boilerplate for WebSocket connect/subscribe/reconnect.

### Approach 2: ws_client plugin + thin adapter (`annotation_agent_simplified.py`)

~130 lines of Python. The ws_client plugin handles the WebSocket lifecycle.
The adapter handles:

1. Auto-approving tool permissions (controlled environment)
2. Posting replies via the Hypothesis REST API
3. Filtering/routing incoming annotation messages (own replies, deletes, etc.)

The TUI client runs with `--headless`, so no clarification events occur.

```bash
python -m server --profile hypothesis-annotation-agent --daemon
jaato --headless  # or: python annotation_agent_simplified.py
```

**Pros:** 60% less code, WebSocket lifecycle fully managed by plugin.
**Cons:** Requires ws_client plugin, still needs a Python adapter for REST replies.

### Approach 3: Pure profile + prompt via TUI client (zero custom code)

No Python adapter at all. The profile
(`.jaato/profiles/hypothesis-annotation-agent-headless.json`) contains
everything: plugin config, system instructions, and `"headless": true`. The
agent uses `cli` with `curl` to POST replies to the Hypothesis API.

```bash
python -m server --profile hypothesis-annotation-agent-headless --daemon
jaato --headless
```

The TUI client connects to the daemon with `--headless`. All logic lives in the system
prompt: filtering annotations, discovering repos via `web_fetch`, posting
replies via `curl`.

**Pros:** Zero custom code, fully declarative, entire agent is a JSON file.
**Cons:** Reliability depends on the LLM following the prompt protocol
perfectly every time. The `curl` reply format must be correct (JSON escaping,
field mapping). Filtering logic is prompt-dependent rather than deterministic.

## What Still Needs Custom Code (Approach 2 only)

### 1. Annotation Filtering

The adapter filters out:
- Annotations tagged `jaato-annotation-agent` (the agent's own replies)
- Delete actions
- Reply annotations

**Verdict:** Deterministic Python is more reliable than prompt-based filtering.
In Approach 3, this is handled by prompt instructions — workable but less
reliable for edge cases.

### 2. REST API Calls (Posting Replies)

Currently uses `requests.Session` to POST annotation replies.

**Verdict:** In Approach 2, handled by the adapter. In Approach 3, replaced by
`curl` via the `cli` tool — the system prompt includes the exact `curl` command
template.

### 3. Repo Discovery

Fetching the annotated page's HTML and parsing `<link rel="vcs-git">` to find
the backing repository.

**Verdict:** Fully replaceable by `web_fetch` plugin + system prompt in both
Approach 2 and 3.

## Comparison

| Approach                           | Lines of code | Reliability | Complexity | Dependency on ws_client |
|------------------------------------|---------------|-------------|------------|-------------------------|
| 1. Current `annotation_agent.py`   | ~345          | High        | Medium     | None (self-contained)   |
| 2. ws_client + thin adapter        | ~130          | High        | Low        | Plugin must be available |
| 3. Pure profile + prompt (TUI)     | 0 (config)    | Medium      | Low        | Plugin must be available |

## Recommendation

**Approach 2** (ws_client plugin + thin adapter) is the best tradeoff for
production use. The WebSocket lifecycle is fully managed by the plugin, while
filtering and REST replies remain deterministic Python.

**Approach 3** (pure profile + prompt) is provided for comparison and is viable
for low-stakes or experimental use. Its main risk is that `curl`-based reply
posting depends on the LLM correctly escaping JSON every time. For an
unattended daemon, deterministic code for the reply POST is preferred.

Both approaches use the TUI's `--headless` flag — the model never requests
clarification.

### Migration Path

1. **Now**: Keep `annotation_agent.py` (Approach 1) as the production
   implementation.
2. **When ws_client ships**: Switch to `annotation_agent_simplified.py`
   (Approach 2) for a 60% code reduction with the same reliability.
3. **Experimental**: Try the pure TUI approach (Approach 3) using
   `hypothesis-annotation-agent-headless.json` to validate prompt-only
   reliability in a staging environment.

## Files

| File | Approach |
|------|----------|
| `annotation_agent.py` | 1 — self-contained |
| `annotation_agent_simplified.py` | 2 — thin adapter |
| `.jaato/profiles/hypothesis-annotation-agent.json` | Profile for Approach 2 |
| `.jaato/profiles/hypothesis-annotation-agent-headless.json` | Profile for Approach 3 (pure TUI) |
