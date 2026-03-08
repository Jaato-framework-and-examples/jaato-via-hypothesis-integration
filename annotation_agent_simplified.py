"""
Hypothesis Annotation Agent (simplified) — thin adapter for use with Jaato's ws_client plugin.

The ws_client plugin manages the WebSocket lifecycle (connect, subscribe, reconnect,
reader thread, message delivery). This adapter only handles:

  1. Posting replies via the Hypothesis REST API
  2. Bridging Jaato permission/clarification events through annotation threads
  3. Filtering/routing incoming annotation messages

Usage:
    1. Ensure the Hypothesis stack is running (docker compose up)
    2. Ensure the jaato server is running with the hypothesis-annotation-agent profile
    3. Run: python annotation_agent_simplified.py
"""

import argparse
import asyncio
import json
import logging
import os
import re
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from jaato_sdk import IPCRecoveryClient
from jaato_sdk.events import (
    AgentOutputEvent,
    ClarificationInputModeEvent,
    ErrorEvent,
    ExternalEvent,
    PermissionInputModeEvent,
    TurnCompletedEvent,
)

load_dotenv()

log = logging.getLogger("annotation-agent")


# ---------------------------------------------------------------------------
# Hypothesis REST client (no WebSocket code — that's handled by the plugin)
# ---------------------------------------------------------------------------

class HypothesisClient:
    def __init__(self, api_url: str, token: str, user: str, authority: str):
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.user = f"acct:{user}@{authority}"

    def create_reply(self, parent: dict, text: str) -> dict:
        payload = {
            "uri": parent["uri"],
            "text": text,
            "group": parent["group"],
            "references": [parent["id"]],
            "tags": ["jaato-annotation-agent"],
        }
        resp = self.session.post(f"{self.api_url}/api/annotations", json=payload)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Annotation processing
# ---------------------------------------------------------------------------

def build_prompt(ann: dict, repo_url: str | None) -> str:
    """Build the Jaato prompt from an annotation."""
    parts: list[str] = []

    if repo_url:
        parts.append(f"Source repository: {repo_url}")
    else:
        parts.append('Source repository: unknown (no <link rel="vcs-git"> found)')

    page_path = urlparse(ann["uri"]).path.lstrip("/") or None
    if page_path:
        parts.append(f"Page path: {page_path}")

    # Extract highlighted text from selectors
    for target in ann.get("target", []):
        for selector in target.get("selector", []):
            if selector.get("type") == "TextQuoteSelector":
                parts.append(f"Context (selected text):\n{selector.get('exact', '')}")
                break

    parts.append(f"Instruction:\n{ann.get('text', '')}")
    return "\n".join(parts)


async def process_annotation(ann: dict, h: HypothesisClient, client: IPCRecoveryClient):
    """Send annotation to Jaato agent and post the reply."""
    ann_id = ann["id"]
    instruction = ann.get("text", "")
    log.info("Processing annotation %s: %s", ann_id, instruction[:80])

    # Repo discovery is delegated to the agent via web_fetch plugin + system prompt.
    # For deterministic behavior, we still extract it here if available.
    repo_url = None  # Agent discovers via web_fetch if needed

    prompt = build_prompt(ann, repo_url)
    await client.send_message(prompt)

    parts: list[str] = []

    async for event in client.events():
        if isinstance(event, AgentOutputEvent):
            parts.append(event.text)

        elif isinstance(event, PermissionInputModeEvent):
            # Post intermediate output, then auto-approve (agent runs in a
            # controlled environment with limited tool access).
            text = "".join(parts)
            text = re.sub(r"\n*Tool: ", "\n\nTool: ", text, count=1)
            if text:
                h.create_reply(ann, text)
                parts.clear()
            await client.respond_to_permission(event.request_id, "comment")

        elif isinstance(event, ClarificationInputModeEvent):
            # Post the clarification question as a reply.  In a full
            # interactive setup this would wait for the user's annotation
            # reply via the WebSocket — but that reply now arrives as an
            # ExternalEvent from the ws_client plugin.  For now, we skip
            # the interactive loop and let the agent proceed with best-effort.
            text = "".join(parts)
            if text:
                h.create_reply(ann, text)
                parts.clear()
            # TODO: wait for user reply via ExternalEvent correlation
            await client.respond_to_clarification(event.request_id, "(no reply)")

        elif isinstance(event, ErrorEvent):
            log.error("Agent error: %s", event)

        elif isinstance(event, TurnCompletedEvent):
            break

    response = "".join(parts)
    if response:
        h.create_reply(ann, response)
        log.info("Replied to %s (%d chars)", ann_id, len(response))


# ---------------------------------------------------------------------------
# Event loop — receives ExternalEvents from the ws_client plugin
# ---------------------------------------------------------------------------

def is_own_annotation(ann: dict) -> bool:
    return "jaato-annotation-agent" in ann.get("tags", [])


def is_reply(ann: dict) -> bool:
    return bool(ann.get("references"))


async def main():
    api_url = os.environ["H_API_URL"]
    token = os.environ["H_API_TOKEN"]
    user = os.environ["ANNOTATION_AGENT_USER"]
    authority = os.environ["ANNOTATION_AGENT_AUTHORITY"]

    h = HypothesisClient(api_url, token, user, authority)
    client = IPCRecoveryClient()
    await client.connect()
    session_id = await client.create_session("annotation-agent")
    log.info("Connected to jaato server, session %s", session_id)

    # The ws_client plugin connects to the Hypothesis WebSocket automatically
    # (configured in the profile).  We send the subscription filter via ws_send
    # as the first agent action — or it can be done in the system prompt.

    try:
        async for event in client.events():
            if not isinstance(event, ExternalEvent):
                continue

            # ExternalEvent.data contains the raw WebSocket message from Hypothesis
            msg = json.loads(event.data) if isinstance(event.data, str) else event.data

            if msg.get("type") != "annotation-notification":
                continue

            action = msg.get("options", {}).get("action")
            for ann in msg.get("payload", []):
                if is_own_annotation(ann) or action == "delete" or is_reply(ann):
                    continue
                await process_annotation(ann, h, client)

    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down")
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hypothesis annotation agent (simplified)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    trace_log = os.environ.get("JAATO_TRACE_LOG")

    if trace_log:
        os.makedirs(os.path.dirname(trace_log) or ".", exist_ok=True)
        logging.basicConfig(level=level, format=fmt, filename=trace_log)
    else:
        logging.basicConfig(level=level, format=fmt)

    asyncio.run(main())
