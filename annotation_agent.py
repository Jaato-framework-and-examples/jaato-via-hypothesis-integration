"""
Hypothesis Annotation Agent — listens for annotations via WebSocket and replies via jaato-sdk.

Usage:
    1. Ensure the Hypothesis stack is running (docker compose up)
    2. Ensure the jaato server is running (jaato listens on /tmp/jaato.sock)
    3. Run: python annotation_agent.py
"""

import argparse
import asyncio
import json
import logging
import os
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests
import websockets
from dotenv import load_dotenv
from jaato_sdk import IPCRecoveryClient
from jaato_sdk.events import (
    AgentOutputEvent,
    ClarificationInputModeEvent,
    ErrorEvent,
    PermissionInputModeEvent,
    TurnCompletedEvent,
)

load_dotenv()

log = logging.getLogger("annotation-agent")


# ---------------------------------------------------------------------------
# Hypothesis API Client
# ---------------------------------------------------------------------------

class HypothesisClient:
    def __init__(self, api_url: str, token: str, user: str, authority: str):
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.token = token
        self.user = f"acct:{user}@{authority}"
        # Reply waiters: annotation_id -> asyncio.Event + stored reply text
        self._reply_waiters: dict[str, asyncio.Event] = {}
        self._reply_texts: dict[str, str] = {}

    def ws_url(self) -> str:
        """Derive the WebSocket URL from the API URL."""
        return self.api_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"

    def notify_reply(self, ann: dict):
        """Called by the WebSocket listener when a user reply arrives."""
        refs = ann.get("references", [])
        if not refs:
            return
        parent_id = refs[0]
        if parent_id in self._reply_waiters:
            self._reply_texts[parent_id] = ann.get("text", "")
            self._reply_waiters[parent_id].set()

    async def wait_for_reply(self, annotation_id: str) -> str:
        """Block until a non-agent reply to *annotation_id* arrives via WebSocket."""
        event = asyncio.Event()
        self._reply_waiters[annotation_id] = event
        try:
            await event.wait()
            return self._reply_texts.pop(annotation_id, "")
        finally:
            self._reply_waiters.pop(annotation_id, None)

    def create_reply(self, parent: dict, text: str) -> dict:
        """Post *text* as a threaded reply to *parent* annotation."""
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
# Jaato Agent (async)
# ---------------------------------------------------------------------------

class JaatoAgent:
    def __init__(self):
        self.client = IPCRecoveryClient()

    async def connect(self):
        await self.client.connect()
        session_id = await self.client.create_session("annotation-agent")
        log.info("Connected to jaato server, session %s", session_id)

    async def close(self):
        log.info("Closing jaato session")
        await self.client.close()

    async def process(self, instruction: str, context: str,
                       h_client: HypothesisClient, parent_ann: dict,
                       repo_url: str | None = None, page_path: str | None = None) -> str:
        """Send a message and handle permissions/clarifications interactively."""
        parts_prompt: list[str] = []
        if repo_url:
            parts_prompt.append(f"Source repository: {repo_url}")
        else:
            parts_prompt.append("Source repository: unknown (no <link rel=\"vcs-git\"> found on the page)")
        if page_path:
            parts_prompt.append(f"Page path: {page_path}")
        parts_prompt.append(f"Context (selected text from the page):\n{context}")
        parts_prompt.append(f"Instruction:\n{instruction}")
        prompt = "\n".join(parts_prompt)
        await self.client.send_message(prompt)

        parts: list[str] = []

        async for event in self.client.events():
            log.debug("Event: %s", type(event).__name__)

            if isinstance(event, AgentOutputEvent):
                log.debug("Output chunk: %s", event.text[:120])
                parts.append(event.text)

            elif isinstance(event, PermissionInputModeEvent):
                log.info("Permission requested (request_id=%s, tool=%s)",
                         event.request_id, event.tool_name)

                text = "".join(parts)
                # Ensure a blank line separates agent text from the tool-call description
                text = re.sub(r"\n*Tool: ", "\n\nTool: ", text, count=1)
                if text:
                    h_client.create_reply(parent_ann, text)
                    log.info("Posted intermediate output (%d chars)", len(text))
                    parts.clear()

                user_reply = await h_client.wait_for_reply(parent_ann["id"])
                log.info("Got user reply for permission: %s", user_reply[:120])
                await self.client.respond_to_permission(event.request_id, "comment")

            elif isinstance(event, ClarificationInputModeEvent):
                log.info("Clarification requested (request_id=%s, tool=%s)",
                         event.request_id, event.tool_name)

                text = "".join(parts)
                if text:
                    h_client.create_reply(parent_ann, text)
                    log.info("Posted intermediate output (%d chars)", len(text))
                    parts.clear()

                user_reply = await h_client.wait_for_reply(parent_ann["id"])
                log.info("Got user reply for clarification: %s", user_reply[:120])
                await self.client.respond_to_clarification(event.request_id, user_reply)

            elif isinstance(event, ErrorEvent):
                log.error("Agent error: %s", event)

            elif isinstance(event, TurnCompletedEvent):
                break

        response = "".join(parts)
        log.debug("Full response (%d chars): %s", len(response), response[:200])
        return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _VcsLinkParser(HTMLParser):
    """Extract href from <link rel="vcs-git" href="...">."""

    def __init__(self):
        super().__init__()
        self.href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag != "link" or self.href is not None:
            return
        attr_map = dict(attrs)
        if attr_map.get("rel") == "vcs-git" and attr_map.get("href"):
            self.href = attr_map["href"]


_repo_cache: dict[str, str | None] = {}  # domain -> repo URL or None


def discover_repo(uri: str) -> str | None:
    """Fetch *uri* and return the <link rel="vcs-git"> href, cached per domain."""
    domain = urlparse(uri).netloc
    if domain in _repo_cache:
        return _repo_cache[domain]

    try:
        resp = requests.get(uri, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Failed to fetch %s for repo discovery: %s", uri, exc)
        _repo_cache[domain] = None
        return None

    parser = _VcsLinkParser()
    parser.feed(resp.text)
    _repo_cache[domain] = parser.href
    if parser.href:
        log.info("Discovered repo %s for domain %s", parser.href, domain)
    else:
        log.info("No <link rel=\"vcs-git\"> found on %s", uri)
    return parser.href


def extract_quote(annotation: dict) -> str:
    """Pull the highlighted text from the annotation's selectors."""
    for target in annotation.get("target", []):
        for selector in target.get("selector", []):
            if selector.get("type") == "TextQuoteSelector":
                return selector.get("exact", "")
    return ""


# ---------------------------------------------------------------------------
# WebSocket listener
# ---------------------------------------------------------------------------

SUBSCRIBE_FILTER = json.dumps({
    "filter": {
        "match_policy": "include_any",
        "clauses": [
            {"field": "/group", "operator": "one_of", "value": ["__world__"]},
        ],
        "actions": {"create": True, "update": True, "delete": True},
    },
})


async def process_annotation(ann: dict, h: HypothesisClient, agent: JaatoAgent):
    """Process a single annotation: discover repo, run through jaato, post reply."""
    ann_id = ann["id"]
    quote = extract_quote(ann)
    instruction = ann.get("text", "")
    log.info("New annotation %s: %s", ann_id, instruction[:80])

    ann_uri = ann["uri"]
    repo_url = discover_repo(ann_uri)
    page_path = urlparse(ann_uri).path.lstrip("/") or None

    response = await agent.process(instruction, quote, h, ann,
                                   repo_url=repo_url, page_path=page_path)
    h.create_reply(ann, response)
    log.info("Replied to %s (%d chars)", ann_id, len(response))


async def listen(h: HypothesisClient, agent: JaatoAgent):
    """Connect to the Hypothesis WebSocket and process annotation events."""
    ws_url = h.ws_url()
    headers = {"Authorization": f"Bearer {h.token}"}
    log.info("Connecting to %s as %s", ws_url, h.user)

    # Queue decouples WebSocket reading from annotation processing, so the
    # reader can keep delivering reply notifications while an annotation is
    # being processed (e.g. during permission/clarification waits).
    work_queue: asyncio.Queue[dict] = asyncio.Queue()

    async def reader(ws):
        await ws.send(SUBSCRIBE_FILTER)
        log.info("Subscribed to group __world__")

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "annotation-notification":
                continue

            action = msg.get("options", {}).get("action")
            for ann in msg.get("payload", []):
                if "jaato-annotation-agent" in ann.get("tags", []):
                    continue

                if action == "delete":
                    continue

                if ann.get("references"):
                    h.notify_reply(ann)
                    continue

                await work_queue.put(ann)

    async def worker():
        while True:
            ann = await work_queue.get()
            try:
                await process_annotation(ann, h, agent)
            except Exception:
                log.exception("Error processing annotation %s", ann.get("id"))

    async for ws in websockets.connect(ws_url, additional_headers=headers):
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(reader(ws))
                tg.create_task(worker())
        except* websockets.ConnectionClosed:
            log.warning("WebSocket connection closed, reconnecting...")


async def main():
    api_url = os.environ["H_API_URL"]
    token = os.environ["H_API_TOKEN"]
    user = os.environ["ANNOTATION_AGENT_USER"]
    authority = os.environ["ANNOTATION_AGENT_AUTHORITY"]

    h = HypothesisClient(api_url, token, user, authority)
    agent = JaatoAgent()
    await agent.connect()

    try:
        await listen(h, agent)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down")
    finally:
        await agent.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hypothesis annotation agent")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    trace_log = os.environ.get("JAATO_TRACE_LOG")

    if trace_log:
        os.makedirs(os.path.dirname(trace_log) or ".", exist_ok=True)
        logging.basicConfig(level=level, format=fmt, filename=trace_log)
        print(f"Annotation agent started. Logging to {trace_log}")
    else:
        logging.basicConfig(level=level, format=fmt)

    asyncio.run(main())
