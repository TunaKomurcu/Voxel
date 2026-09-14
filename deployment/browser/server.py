#!/usr/bin/env python3
"""Talk to your agent from a browser tab.

    python deployment/browser/server.py

The API key stays in this process; the page only gets 60-second tokens.
"""

import copy
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from lib import (ApiError, _agents_api, aai, load_env, publish_agent, read_agent,  # noqa: E402
                 required, stored_agent_id)
import judge  # noqa: E402


# The four Voxel personas, selectable in the browser before a call starts.
# Overridden entirely by the starter's usual AGENT=<name> env var (single
# agent, e.g. AGENT=http-tools), which still works for testing any other
# agents/*.jsonc file the way the README describes.
# short_name is the persona's own first name, used in the transcript
# ("Priya: ...") — distinct from "name", which resolve_agent() fills in
# from the agent's stored dashboard name ("Voxel Technical Co-founder").
PERSONAS = [
    {"key": "investor", "file": "voxel-investor", "label": "Marcus — Investor", "short_name": "Marcus"},
    {"key": "technical", "file": "technical-cofounder", "label": "Priya — Technical Co-founder", "short_name": "Priya"},
    {"key": "buyer", "file": "non-technical-buyer", "label": "Grace — Non-technical Buyer", "short_name": "Grace"},
    {"key": "enterprise", "file": "impatient-buyer", "label": "Derek — Impatient Enterprise Buyer", "short_name": "Derek"},
]


def resolve_agent(name: str) -> dict:
    """A published id means the agent is managed elsewhere, so use it as it is."""
    known = stored_agent_id(name)
    if known:
        try:
            agent = aai(f"/agents/{known}")
        except ApiError as err:
            sys.exit(f"Could not load agent {known}: {err}")
        return {"id": known, "name": agent.get("name") or "Your agent"}
    agent = read_agent(name)
    try:
        result = publish_agent(agent, name=name, reuse_by_name=True)
    except ApiError as err:
        sys.exit(f"Could not publish agents/{name}.jsonc: {err}")
    verb = "Created" if result["created"] else "Updated"
    print(f'{verb} "{agent["name"]}" from agents/{name}.jsonc')
    return {"id": result["id"], "name": agent["name"]}


def public_agent(agent: dict) -> dict:
    """Read-only view of the stored agent. The API keeps header values and llm
    keys write-only; these deletes hold even if that changes. The system prompt
    is in here, so a public deployment shows it to anyone who opens the page."""
    copied = copy.deepcopy(agent)
    for tool in copied.get("tools", []):
        for header in tool.get("http", {}).get("headers", []):
            header["value"] = "<hidden>"
    for llm in copied.get("llm", []):
        llm.pop("api_key", None)
    return copied


RESOLVED_PERSONAS: list = []
PAGE = ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError as err:
            # BrokenPipeError / ConnectionResetError, usually: the client
            # (browser tab) is gone before the response finished sending —
            # most likely on /judge, whose run_judge_pass can take 15-45s.
            # Not a bug, just a reply with nowhere to go anymore.
            print(f"Client disconnected before response could be sent ({status}): {err}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/judge":
            session_id = (query.get("session_id") or [""])[0]
            if not session_id:
                self._send(400, b'{"error":"missing session_id"}', "application/json")
                return
            try:
                result = judge.run_judge_pass(session_id)
                self._send(200, json.dumps(result).encode(), "application/json")
            except judge.RateLimitedError as err:
                print(f"Judge pass rate-limited for {session_id}: {err}")
                body = json.dumps({"error": str(err)}).encode()
                self._send(503, body, "application/json")
            except Exception as err:
                print(f"Judge pass failed for {session_id}: {err}")
                body = json.dumps({"error": "Could not generate feedback for this call."}).encode()
                self._send(502, body, "application/json")
            return
        if path == "/token":
            try:
                token = aai("/token?product=voice_agent&expires_in_seconds=60")
                # The browser connects the call's websocket directly to
                # AssemblyAI, not through this server, so it needs the same
                # region-pinned host our own REST calls use (AGENTS_API_BASE)
                # — otherwise it can geo-route to a host that doesn't have
                # the agent, a real agent_not_found hit in production.
                token["ws_base"] = _agents_api().replace("https://", "wss://", 1)
                self._send(200, json.dumps(token).encode(), "application/json")
            except ApiError as err:
                print(err)
                self._send(502, b'{"error":"token request failed"}', "application/json")
            return
        if path == "/agent":
            key = (query.get("key") or [""])[0]
            entry = next((p for p in RESOLVED_PERSONAS if p["key"] == key), None)
            if entry is None:
                self._send(400, b'{"error":"unknown persona key"}', "application/json")
                return
            try:
                agent = aai(f"/agents/{entry['id']}")
                self._send(200, json.dumps(public_agent(agent)).encode(), "application/json")
            except ApiError as err:
                print(err)
                self._send(502, b'{"error":"could not load the agent"}', "application/json")
            return
        if path == "/app.js":
            self._send(200, (HERE / "app.js").read_bytes(), "text/javascript")
            return
        self._send(200, PAGE.encode(), "text/html")

    def log_message(self, *args) -> None:  # quiet; errors are printed above
        pass


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        # A client (browser tab) disappearing mid-request, or between
        # keep-alive requests, surfaces here as a raw socket error from the
        # stdlib's read/write loop — outside our own try/except in _send().
        # Most likely: someone closed the tab while /judge's run_judge_pass
        # (15-45s) was still working. Log a line, not a traceback; the
        # server itself (this is one thread of many) is unaffected either way.
        _, exc, _ = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            print(f"Client {client_address} disconnected: {exc}")
            return
        super().handle_error(request, client_address)


def main() -> None:
    global RESOLVED_PERSONAS, PAGE
    load_env()
    required("ASSEMBLYAI_API_KEY", "get one at https://www.assemblyai.com/dashboard/api-keys")

    if os.environ.get("AGENT"):
        # Legacy single-agent mode: AGENT=<name> from the starter's own
        # convention (e.g. AGENT=http-tools), still useful for testing any
        # other agents/*.jsonc file one at a time.
        name = os.environ["AGENT"]
        resolved = resolve_agent(name)
        RESOLVED_PERSONAS = [{"key": "default", "label": None, "short_name": resolved["name"], **resolved}]
        print(f"Agent: {RESOLVED_PERSONAS[0]['id']} (single-agent mode, AGENT={name})")
    else:
        RESOLVED_PERSONAS = [
            {"key": p["key"], "label": p["label"], "short_name": p["short_name"], **resolve_agent(p["file"])}
            for p in PERSONAS
        ]
        for p in RESOLVED_PERSONAS:
            print(f"Persona: {p['label']} -> {p['id']}")

    PAGE = ((HERE / "index.html").read_text(encoding="utf-8")
            .replace("{{PERSONAS_JSON}}", json.dumps(RESOLVED_PERSONAS).replace("<", "\\u003c")))

    # PORT when set, otherwise 3000 and up until one is free.
    fixed = os.environ.get("PORT")
    port = int(fixed) if fixed else 3000
    while True:
        try:
            server = Server(("", port), Handler)
            break
        except OSError:
            if fixed or port >= 3010:
                raise
            port += 1

    print(f"Talk to it: http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
