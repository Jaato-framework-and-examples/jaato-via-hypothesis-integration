# Evaluation: Simplifying with Jaato's WebSocket Client Plugin

## Current Architecture

The current `annotation_agent.py` (345 lines of custom Python) does two jobs:

1. **WebSocket client** — connects to the Hypothesis WebSocket, subscribes with
   a JSON filter, reads annotation events, reconnects on drops, and decouples
   reading from processing via an `asyncio.Queue`.

2. **Integration glue** — discovers source repos from `<link rel="vcs-git">`,
   builds prompts, posts replies via the Hypothesis REST API, and auto-approves
   tool permissions.

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

## Two Approaches

### Approach 1: Current `annotation_agent.py` (self-contained)

345 lines of Python. Manages everything: WebSocket lifecycle, repo discovery,
prompt building, REST API replies, permission auto-approval. No dependency on
the `ws_client` plugin.

```bash
python annotation_agent.py
```

**Pros:** Self-contained, no plugin dependency, deterministic filtering.
**Cons:** Significant boilerplate for WebSocket connect/subscribe/reconnect.

### Approach 2: Profile + prompt via Jaato TUI (zero custom code)

No Python script at all. A single profile
(`.jaato/profiles/hypothesis-annotation-agent.json`) contains everything: the
ws_client plugin config and system instructions. The Jaato TUI client runs the
agent directly — auto-starting the server if needed.

```bash
jaato --headless --profile hypothesis-annotation-agent
```

That's it. The ws_client plugin connects to the Hypothesis WebSocket, delivers
incoming annotation events as prompts, and the system instructions tell the
agent how to filter, process, and reply (via `curl` through the `cli` tool).

**Pros:** Zero custom code, fully declarative, entire agent is a JSON profile +
a one-liner command.
**Cons:** Reliability depends on the LLM following the prompt protocol
perfectly every time — correct JSON escaping in `curl`, correct annotation
filtering, correct field mapping. Prompt-dependent rather than deterministic.

## What the Profile Handles

Everything that was previously custom Python:

### 1. Annotation Filtering

System prompt instructs the agent to ignore:
- Annotations tagged `jaato-annotation-agent` (its own replies)
- `delete` actions
- Reply annotations (non-empty `references` array)

In Approach 1 this is deterministic Python. In Approach 2 it's prompt-dependent
— workable but less reliable for edge cases.

### 2. Repo Discovery

System prompt instructs the agent to fetch the annotated URI via `web_fetch`
and extract the `<link rel="vcs-git">` tag. Fully equivalent to the Python
implementation.

### 3. Posting Replies

System prompt provides a `curl` template. The agent uses `cli` to POST
annotation replies to the Hypothesis API. In Approach 1 this is handled by
`requests.Session` — more reliable for JSON escaping.

## Comparison

| Aspect                    | Approach 1 (custom Python)       | Approach 2 (profile + TUI)         |
|---------------------------|----------------------------------|------------------------------------|
| Lines of custom code      | ~345                             | 0                                  |
| WebSocket lifecycle       | Custom (websockets lib)          | ws_client plugin                   |
| Annotation filtering      | Deterministic Python             | Prompt-dependent                   |
| Reply posting             | `requests.Session`               | `curl` via `cli` tool              |
| Repo discovery            | Custom HTML parser               | `web_fetch` + prompt               |
| Clarification             | Not used                         | Blocked by `--headless`            |
| Plugin dependency         | None                             | ws_client                          |
| Startup command           | `python annotation_agent.py`     | `jaato --headless --profile ...`   |

## Recommendation

**Approach 2 is the target.** Once the `ws_client` plugin ships, the entire
annotation agent reduces to a profile JSON and a one-liner command. The
345-line Python script becomes unnecessary.

The main risk is reliability of prompt-dependent operations (JSON escaping in
`curl`, annotation filtering). This should be validated in a staging
environment before replacing the Python agent in production.

### Migration Path

1. **Now**: Keep `annotation_agent.py` as the production implementation.
2. **When ws_client ships**: Test the profile-only approach
   (`jaato --headless --profile hypothesis-annotation-agent`) against the same
   annotation workload.
3. **When validated**: Drop `annotation_agent.py` entirely.

## Files

| File | Purpose |
|------|---------|
| `annotation_agent.py` | Current self-contained implementation (Approach 1) |
| `.jaato/profiles/hypothesis-annotation-agent.json` | Profile for the TUI-based approach (Approach 2) |
