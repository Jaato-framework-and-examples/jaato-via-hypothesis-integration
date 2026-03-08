# Evaluation: Simplifying with Jaato's WebSocket Client Plugin

## Current Architecture

The current `annotation_agent.py` (345 lines of custom Python) does two jobs:

1. **WebSocket client** — connects to the Hypothesis WebSocket, subscribes with
   a JSON filter, reads annotation events, reconnects on drops, and decouples
   reading from processing via an `asyncio.Queue`.

2. **Integration glue** — discovers source repos from `<link rel="vcs-git">`,
   builds prompts, posts replies via the Hypothesis REST API, and bridges
   Jaato's permission/clarification events back through annotation threads.

## What the ws_client Plugin Replaces

The upcoming `ws_client` plugin (see `docs/design/websocket-client-plugin.md` in
the Jaato repo) handles outbound persistent WebSocket connections declaratively:

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

| Approach                        | Lines of code | Reliability | Complexity |
|---------------------------------|---------------|-------------|------------|
| Current `annotation_agent.py`   | ~345          | High        | Medium     |
| ws_client plugin + thin adapter | ~80-120       | High        | Low        |
| ws_client plugin + prompt only  | ~0 (config)   | Medium      | Low        |

## Recommendation

**Use the ws_client plugin with a thin Python adapter** (~80-120 lines).

The plugin eliminates all WebSocket boilerplate (connect, subscribe, reconnect,
reader thread, message queue). What remains is a small adapter that:

1. Handles the permission/clarification bridging deterministically
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

## Sketch: Simplified Agent

See `annotation_agent_simplified.py` for a working sketch of the thin adapter
approach assuming the ws_client plugin handles the WebSocket layer.
