"""
MCP Client — connects to stdio-based MCP servers, discovers and forwards tool calls.
Uses raw JSON-RPC over stdio (no SDK dependency for simplicity).

Config is read from .claude/settings.json and ~/.claude/settings.json:
  { "mcpServers": { "name": { "command": "...", "args": [...], "env": {...} } } }

Each MCP tool is exposed with a "mcp__serverName__toolName" prefix to avoid conflicts.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any


# ─── Single MCP connection (one per server) ──────────────────


class McpConnection:
    """Manages a single MCP server process and JSON-RPC communication."""

    def __init__(self, server_name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None):
        self.server_name = server_name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._events: list[dict] = []

    async def connect(self) -> None:
        """Spawn the server process."""
        merged_env = {**os.environ, **self.env}
        self._process = await asyncio.create_subprocess_exec(
            self.command, *self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
        )
        # Start reading stdout lines in background
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """Read newline-delimited JSON-RPC responses from stdout."""
        assert self._process and self._process.stdout
        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._pending:
                fut = self._pending.pop(msg_id)
                if "error" in msg:
                    e = msg["error"]
                    fut.set_exception(
                        RuntimeError(f"MCP error {e.get('code')}: {e.get('message')}")
                    )
                else:
                    fut.set_result(msg.get("result"))
            elif msg.get("method"):
                self._events.append({
                    "server": self.server_name,
                    "method": msg.get("method"),
                    "params": msg.get("params") or {},
                })

    async def _send_request(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON-RPC request and wait for response."""
        assert self._process and self._process.stdin
        req_id = self._next_id
        self._next_id += 1
        msg = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        self._process.stdin.write((msg + "\n").encode())
        await self._process.stdin.drain()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        return await fut

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self._process or not self._process.stdin:
            return
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        self._process.stdin.write((msg + "\n").encode())

    async def initialize(self) -> None:
        """Perform MCP initialize handshake."""
        await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "resources": {"subscribe": True},
            },
            "clientInfo": {"name": "forgecc", "version": "1.0.0"},
        })
        self._send_notification("notifications/initialized")

    async def list_tools(self) -> list[dict]:
        """Discover available tools from this server."""
        result = await self._send_request("tools/list")
        if not result or not isinstance(result.get("tools"), list):
            return []
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema"),
                "serverName": self.server_name,
            }
            for t in result["tools"]
        ]

    async def call_tool(self, name: str, args: dict) -> str:
        """Call a tool and return the text result."""
        result = await self._send_request("tools/call", {"name": name, "arguments": args})
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            return "\n".join(
                c["text"] for c in result["content"] if c.get("type") == "text"
            )
        return json.dumps(result)

    async def list_resources(self) -> list[dict]:
        result = await self._send_request("resources/list")
        resources = result.get("resources", []) if isinstance(result, dict) else []
        if not isinstance(resources, list):
            return []
        return resources

    async def read_resource(self, uri: str) -> str:
        result = await self._send_request("resources/read", {"uri": uri})
        if not isinstance(result, dict):
            return json.dumps(result)
        contents = result.get("contents")
        if not isinstance(contents, list):
            return json.dumps(result)
        parts: list[str] = []
        for item in contents:
            if not isinstance(item, dict):
                continue
            label = item.get("uri") or uri
            if "text" in item:
                parts.append(f"Resource: {label}\n{item['text']}")
            elif "blob" in item:
                parts.append(f"Resource: {label}\n[blob content omitted]")
        return "\n\n".join(parts) or json.dumps(result)

    async def subscribe_resource(self, uri: str) -> str:
        result = await self._send_request("resources/subscribe", {"uri": uri})
        return json.dumps(result or {"subscribed": uri})

    async def unsubscribe_resource(self, uri: str) -> str:
        result = await self._send_request("resources/unsubscribe", {"uri": uri})
        return json.dumps(result or {"unsubscribed": uri})

    def poll_events(self, max_events: int = 20) -> list[dict]:
        events = self._events[:max_events]
        del self._events[:max_events]
        return events

    def close(self) -> None:
        """Kill the server process."""
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._process:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
            self._process = None
        # Reject pending requests
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(f"MCP server '{self.server_name}' closed"))
        self._pending.clear()


# ─── MCP Manager ─────────────────────────────────────────────


class McpManager:
    """Manages all MCP server connections. Call load_and_connect() once, then
    use get_tool_definitions() and call_tool() to integrate with the agent."""

    def __init__(self):
        self._connections: dict[str, McpConnection] = {}
        self._tools: list[dict] = []
        self._configs: dict[str, dict] = {}
        self._connected = False

    async def load_and_connect(self) -> None:
        """Read settings, connect to all configured MCP servers, discover tools."""
        if self._connected:
            return
        self._connected = True

        configs = self._load_configs()
        self._configs = configs
        if not configs:
            return

        timeout = 15.0

        for name, cfg in configs.items():
            conn = McpConnection(
                name,
                cfg["command"],
                cfg.get("args"),
                cfg.get("env"),
            )
            try:
                await conn.connect()
                await asyncio.wait_for(conn.initialize(), timeout=timeout)
                server_tools = await asyncio.wait_for(conn.list_tools(), timeout=timeout)
                self._connections[name] = conn
                self._tools.extend(server_tools)
                print(f"[mcp] Connected to '{name}' — {len(server_tools)} tools", flush=True)
            except Exception as e:
                print(f"[mcp] Failed to connect to '{name}': {e}", flush=True)
                conn.close()

    def get_tool_definitions(self) -> list[dict]:
        """Return internal tool definitions with an mcp__ prefix."""
        return [
            {
                "name": f"mcp__{t['serverName']}__{t['name']}",
                "description": t.get("description") or f"MCP tool {t['name']} from {t['serverName']}",
                "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for t in self._tools
        ]

    def is_mcp_tool(self, name: str) -> bool:
        """Check if a tool name is an MCP-prefixed tool."""
        return name.startswith("mcp__")

    async def call_tool(self, prefixed_name: str, args: dict) -> str:
        """Route a prefixed tool call to the correct server."""
        parts = prefixed_name.split("__")
        if len(parts) < 3:
            raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
        server_name = parts[1]
        tool_name = "__".join(parts[2:])  # tool name might contain __
        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP server '{server_name}' not connected")
        return await conn.call_tool(tool_name, args)

    async def call_runtime_tool(self, name: str, inp: dict) -> str:
        server = inp.get("server")
        if name == "mcp_list_resources":
            return await self._list_resources(server)
        if name == "mcp_read_resource":
            return await self._read_resource(inp.get("server") or "", inp.get("uri") or "")
        if name == "mcp_subscribe_resource":
            return await self._subscribe_resource(inp.get("server") or "", inp.get("uri") or "")
        if name == "mcp_unsubscribe_resource":
            return await self._unsubscribe_resource(inp.get("server") or "", inp.get("uri") or "")
        if name == "mcp_poll":
            return self._poll(server, int(inp.get("max_events") or 20))
        if name == "mcp_oauth_status":
            return self._oauth_status(server)
        return f"Unknown MCP runtime tool: {name}"

    async def _list_resources(self, server: str | None = None) -> str:
        conns = self._select_connections(server)
        if not conns:
            return "No connected MCP servers."
        lines: list[str] = []
        for name, conn in conns.items():
            try:
                resources = await conn.list_resources()
            except Exception as exc:
                lines.append(f"{name}: resource listing failed: {exc}")
                continue
            if not resources:
                lines.append(f"{name}: no resources")
                continue
            lines.append(f"{name}:")
            for res in resources:
                uri = res.get("uri", "")
                title = res.get("name") or res.get("title") or uri
                mime = f" ({res.get('mimeType')})" if res.get("mimeType") else ""
                lines.append(f"- {uri}{mime} — {title}")
        return "\n".join(lines)

    async def _read_resource(self, server: str, uri: str) -> str:
        conn = self._connections.get(server)
        if not conn:
            return f"MCP server '{server}' not connected"
        if not uri:
            return "Error: uri is required."
        return await conn.read_resource(uri)

    async def _subscribe_resource(self, server: str, uri: str) -> str:
        conn = self._connections.get(server)
        if not conn:
            return f"MCP server '{server}' not connected"
        if not uri:
            return "Error: uri is required."
        try:
            return await conn.subscribe_resource(uri)
        except Exception as exc:
            return f"Subscription failed: {exc}"

    async def _unsubscribe_resource(self, server: str, uri: str) -> str:
        conn = self._connections.get(server)
        if not conn:
            return f"MCP server '{server}' not connected"
        if not uri:
            return "Error: uri is required."
        try:
            return await conn.unsubscribe_resource(uri)
        except Exception as exc:
            return f"Unsubscribe failed: {exc}"

    def _poll(self, server: str | None, max_events: int) -> str:
        conns = self._select_connections(server)
        events: list[dict] = []
        for conn in conns.values():
            events.extend(conn.poll_events(max_events=max(0, max_events - len(events))))
            if len(events) >= max_events:
                break
        if not events:
            return "No MCP events."
        return json.dumps(events, indent=2)

    def _oauth_status(self, server: str | None = None) -> str:
        names = [server] if server else sorted(self._configs)
        if not names:
            return "No MCP servers configured."
        lines: list[str] = []
        for name in names:
            cfg = self._configs.get(name)
            if not cfg:
                lines.append(f"{name}: not configured")
                continue
            oauth = cfg.get("oauth") or cfg.get("authorization") or {}
            env = cfg.get("env") or {}
            token_vars = []
            for key, value in env.items():
                if "TOKEN" in key.upper() or "API_KEY" in key.upper():
                    token_vars.append((key, bool(os.environ.get(key) or value)))
            if not oauth and not token_vars:
                lines.append(f"{name}: no OAuth/token metadata configured")
                continue
            oauth_bits = ", ".join(f"{k}={v}" for k, v in oauth.items()) if isinstance(oauth, dict) else str(oauth)
            token_bits = ", ".join(f"{key}:{'set' if present else 'missing'}" for key, present in token_vars)
            lines.append(f"{name}: oauth={oauth_bits or '(none)'} tokens={token_bits or '(none)'}")
        return "\n".join(lines)

    def _select_connections(self, server: str | None) -> dict[str, McpConnection]:
        if server:
            conn = self._connections.get(server)
            return {server: conn} if conn else {}
        return self._connections

    async def disconnect_all(self) -> None:
        """Disconnect all servers."""
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()
        self._tools.clear()
        self._connected = False

    # ─── Config loading ──────────────────────────────────────

    def _load_configs(self) -> dict[str, dict]:
        merged: dict[str, dict] = {}

        # 1. Global: ~/.claude/settings.json
        global_path = Path.home() / ".claude" / "settings.json"
        self._merge_config_file(global_path, merged)

        # 2. Project: .claude/settings.json (cwd)
        project_path = Path.cwd() / ".claude" / "settings.json"
        self._merge_config_file(project_path, merged)

        # 3. Also check .mcp.json (Claude Code convention)
        mcp_json_path = Path.cwd() / ".mcp.json"
        self._merge_config_file(mcp_json_path, merged)

        return merged

    def _merge_config_file(self, path: Path, target: dict[str, dict]) -> None:
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text())
            servers = raw.get("mcpServers", raw)
            for name, config in servers.items():
                if isinstance(config, dict) and "command" in config:
                    target[name] = config
        except Exception:
            pass  # skip malformed config
