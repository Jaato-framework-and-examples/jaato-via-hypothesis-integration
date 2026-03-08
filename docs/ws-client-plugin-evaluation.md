# Evaluation: Simplifying with Jaato's WebSocket Client Plugin

## Current Architecture

The current `annotation_agent.py` (345 lines of custom Python) does two jobs:

1. **WebSocket client** — connects to the Hypothesis WebSocket, subscribes with
   a JSON filter, reads annotation events, reconnects on drops, and decouples
   reading from processing via an `asyncio.Queue`.

2. **Integration glue** — discovers source repos from `<link rel="vcs-git">`,
   builds prompts, posts replies via the Hypothesis REST API, and bridges
   Jaato's permission/clarification events back through annotation threads.

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

### What a profile-only approach would look like

```jsonc
// .jaato/profiles/hypothesis-annotation-agent.json
{
  "name": "hypothesis-annotation-agent",
  "mode": "daemon",
  "model": "claude-sonnet-4-6",
  "provider": "anthropic",
  "plugins": ["ws_client(preload)", "cli", "file_edit", "web_fetch"],
  "plugin_configs": {
    "ws_client": {
      "connections": {
        "hypothesis": {
          "url": "ws://${H_HOST}:5000/ws",
          "headers": {
            "Authorization": "Bearer ${H_API_TOKEN}"
          },
          "reconnect": true,
          "reconnect_max_attempts": -1,
          "ping_interval": 30
        }
      }
    }
  },
  "system_instructions": "... (see below)"
}
```

On session start, the agent would call `ws_connect(name="hypothesis")`, receive
the initial server hello, and then call `ws_send` with the subscription filter.
From that point, incoming annotation events arrive automatically via
`inject_prompt()`.

## What Still Needs Custom Code

Even with the ws_client plugin handling the WebSocket lifecycle, several pieces
of domain logic cannot be expressed purely through a profile + prompt:

### 1. Interactive Permission/Clarification Loop

When Jaato enters `PermissionInputModeEvent` or `ClarificationInputModeEvent`,
the current agent:

1. Posts intermediate output as a Hypothesis reply
2. Waits for the user to reply via a new annotation
3. Feeds that reply text back to Jaato

This is a **bidirectional bridge between two async systems** — the agent must
correlate Jaato SDK events with incoming Hypothesis WebSocket messages that
arrive on the same connection. A pure daemon profile would receive the
permission/clarification events, but has no built-in mechanism to:

- Post to the Hypothesis REST API (needs `requests` or the `web_fetch` plugin)
- Wait specifically for a reply annotation on that thread
- Feed the user's text back as a permission/clarification response

**Verdict:** This could potentially be handled by the agent (LLM) itself if:
- The `web_fetch` plugin or `cli` tool can POST to the Hypothesis API
- The agent's system prompt instructs it to post a reply, then wait for the
  next incoming WebSocket message that references the annotation ID
- The agent uses `ws_send` or Jaato SDK calls to respond to permission requests

This is the hardest part to get right without custom code. The LLM would need
to reliably execute a multi-step protocol (post reply → wait → respond) every
time. It is feasible but fragile compared to deterministic Python code.

### 2. Repo Discovery

Fetching the annotated page's HTML and parsing `<link rel="vcs-git">` to find
the backing repository. This could be handled by:

- The `web_fetch` plugin (fetch the page, extract the link)
- System prompt instructions telling the agent to look for it

**Verdict:** Fully replaceable by prompt + web_fetch plugin.

### 3. Annotation Filtering

The current code filters out:
- Annotations tagged `jaato-annotation-agent` (the agent's own replies)
- Delete actions
- Reply annotations (routed to `notify_reply` instead of processing)

**Verdict:** Expressible in the system prompt. The agent receives the raw JSON
and can be instructed to ignore its own messages and route replies.

### 4. REST API Calls (Posting Replies)

Currently uses `requests.Session` to POST annotation replies.

**Verdict:** Replaceable by `web_fetch` plugin (if it supports POST) or `cli`
tool (`curl`). Alternatively, the `service_connector` plugin could be
configured with the Hypothesis API spec.

## Comparison

| Approach                        | Lines of code | Reliability | Complexity | Dependency on ws_client |
|---------------------------------|---------------|-------------|------------|-------------------------|
| Current `annotation_agent.py`   | ~345          | High        | Medium     | None (self-contained)   |
| ws_client plugin + thin adapter | ~120          | High        | Low        | Plugin must be available |
| ws_client plugin + prompt only  | ~0 (config)   | Medium      | Low        | Plugin must be available |

## Recommendation

**Use the ws_client plugin with a thin Python adapter** (~120 lines).

The plugin eliminates all WebSocket boilerplate (connect, subscribe, reconnect,
reader thread, message queue). What remains is a small adapter that:

1. Handles the permission/clarification bridging deterministically — including
   the interactive loop where user replies arrive as `ExternalEvent` instances
   correlated by annotation ID
2. Posts replies via the Hypothesis REST API
3. Filters/routes incoming annotation messages

This gives the best tradeoff: the WebSocket lifecycle is fully managed by the
plugin, while the domain-specific interactive flows remain reliable Python code
rather than prompt-dependent LLM behavior.

A **pure profile + prompt approach** (zero custom code) is theoretically
possible but risky — the permission/clarification handshake is a strict
protocol that the LLM must execute perfectly every time. One missed step and the
conversation stalls. For a daemon that runs unattended, deterministic code for
this loop is strongly preferred.

### Migration Path

1. **Now**: Keep `annotation_agent.py` as the production implementation.
2. **When ws_client ships**: Switch to `annotation_agent_simplified.py`, which
   is already a working implementation of the thin adapter approach.
3. **Optional**: If operational experience shows the interactive
   permission/clarification loop is rarely triggered, consider the prompt-only
   approach as a further simplification.

## Sketch: Simplified Agent

See `annotation_agent_simplified.py` for a working implementation of the thin
adapter approach. It handles the full lifecycle including interactive
clarification via `ExternalEvent` correlation — the one piece the evaluation
document initially flagged as incomplete.
