"""Agent core loop for an OpenAI-compatible backend.

Includes streaming, 4-layer compression, plan mode, sub-agents, and budget
control.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Awaitable

import openai

from .tools import (
    tool_definitions,
    execute_tool,
    check_permission,
    CONCURRENCY_SAFE_TOOLS,
    ORCHESTRATION_TOOLS,
    TASK_TOOLS,
    TEAM_TOOLS,
    TEAM_MEMBER_TOOLS,
    WORKTREE_TOOLS,
    MCP_RUNTIME_TOOLS,
    get_active_tool_definitions,
    ToolDef,
)
from .memory import (
    start_memory_prefetch,
    format_memories_for_injection,
    MemoryPrefetch,
)
from .ui import (
    print_assistant_text,
    print_tool_call,
    print_tool_result,
    print_error,
    print_confirmation,
    print_divider,
    print_cost,
    print_retry,
    print_info,
    print_sub_agent_start,
    print_sub_agent_end,
    start_spinner,
    stop_spinner,
)
from .session import save_session
from .prompt import build_system_prompt
from .subagent import get_sub_agent_config
from .mcp_client import McpManager
from .hooks import run_hooks
from .tasks import TaskManager
from .teams import TeamManager
from .worktrees import WorktreeManager

# ─── Retry with exponential backoff ──────────────────────────


def _is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status in (429, 503, 529):
        return True
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
        return True
    return False


async def _with_retry(fn, max_retries: int = 3): # 指数退避重试机制
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as error:
            if attempt >= max_retries or not _is_retryable(error):
                raise
            delay = min(1000 * (2 ** attempt), 30000) / 1000 + (hash(str(time.time())) % 1000) / 1000
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            reason = f"HTTP {status}" if status else (getattr(error, "code", None) or "network error")
            print_retry(attempt + 1, max_retries, reason)
            await asyncio.sleep(delay)


# ─── Model context windows ──────────────────────────────────

MODEL_CONTEXT = {
    "gpt-4.1": 1_000_000,
    "gpt-4.1-mini": 1_000_000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
}


def _get_context_window(model: str) -> int:
    return MODEL_CONTEXT.get(model, 200000)


def _get_cached_prompt_tokens(usage: Any) -> int:
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    if details is None:
        return 0
    cached = getattr(details, "cached_tokens", None)
    if cached is None and isinstance(details, dict):
        cached = details.get("cached_tokens")
    return int(cached or 0)


def _supports_prompt_cache_params(api_base: str | None) -> bool:
    if not api_base:
        return True
    return "api.openai.com" in api_base


def _build_prompt_cache_extra_body(api_base: str | None, model: str) -> dict | None:
    if not _supports_prompt_cache_params(api_base):
        return None
    workspace_hash = hashlib.sha256(str(Path.cwd()).encode("utf-8")).hexdigest()[:16]
    body = {"prompt_cache_key": f"forgecc:{workspace_hash}:{model}"}
    retention = os.environ.get("CCA_PROMPT_CACHE_RETENTION")
    if retention:
        body["prompt_cache_retention"] = retention
    return body


# ─── Convert tools to OpenAI format ─────────────────────────


def _to_openai_tools(tools: list[ToolDef]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


# ─── Multi-tier compression constants ────────────────────────

SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIP_THRESHOLD = 0.60
MICROCOMPACT_IDLE_S = 5 * 60  # 5 minutes
KEEP_RECENT_RESULTS = 3


# ─── Agent ───────────────────────────────────────────────────


class Agent:
    def __init__(
        self,
        *,
        permission_mode: str = "default",
        model: str = "gpt-4.1-mini",
        api_base: str | None = None,
        api_key: str | None = None,
        max_cost_usd: float | None = None,
        max_turns: int | None = None,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
        custom_system_prompt: str | None = None,
        custom_tools: list[ToolDef] | None = None,
        is_sub_agent: bool = False,
        coordinator_mode: bool = False,
        team_manager: TeamManager | None = None,
        worktree_manager: WorktreeManager | None = None,
        team_id: str | None = None,
        team_member: str | None = None,
        cwd: str | None = None,
    ):
        self.permission_mode = permission_mode
        self.model = model
        self.api_base = api_base
        self.api_key = api_key
        self.is_sub_agent = is_sub_agent
        self.coordinator_mode = coordinator_mode
        self.team_id = team_id
        self.team_member = team_member
        self.cwd = str(Path(cwd).resolve()) if cwd else str(Path.cwd().resolve())
        if custom_tools is not None:
            self.tools = custom_tools
        elif coordinator_mode:
            self.tools = [t for t in tool_definitions if t["name"] in ORCHESTRATION_TOOLS]
        else:
            self.tools = tool_definitions
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.confirm_fn = confirm_fn
        self.effective_window = _get_context_window(model) - 20000
        self.session_id = uuid.uuid4().hex[:8]
        self.session_start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cached_input_tokens = 0
        self.last_input_token_count = 0
        self.last_cached_input_token_count = 0
        self.current_turns = 0
        self.last_api_call_time = 0.0
        self._prompt_cache_extra_body = _build_prompt_cache_extra_body(api_base, model)

        # Abort support
        self._aborted = False
        self._current_task: asyncio.Task | None = None

        # Permission whitelist
        self._confirmed_paths: set[str] = set()

        # Plan mode state
        self._pre_plan_mode: str | None = None
        self._plan_file_path: str | None = None
        self._plan_approval_fn: Callable[[str], Awaitable[dict]] | None = None
        self._context_cleared: bool = False  # Set when plan approval clears context

        # Output buffer (sub-agents capture output)
        self._output_buffer: list[str] | None = None

        # Background sub-agent tasks for coordinator-style multi-agent work
        self._task_manager = TaskManager()

        # Persistent team runtime for swarm-style multi-agent work
        self._team_manager = team_manager or TeamManager()
        self._worktree_manager = worktree_manager or WorktreeManager()

        # Read-before-edit: track file read timestamps (absolutePath → mtime)
        self._read_file_state: dict[str, float] = {}

        # Session-level todo list maintained by todo_write
        self._todos: list[dict] = []

        # MCP integration
        self._mcp_manager = McpManager()
        self._mcp_initialized = False

        # Memory recall state — semantic prefetch per user turn
        self._already_surfaced_memories: set[str] = set()
        self._session_memory_bytes = 0

        self._openai_messages: list[dict] = []

        # Build system prompt
        self._base_system_prompt = custom_system_prompt or build_system_prompt()
        if self.coordinator_mode:
            self._base_system_prompt += self._build_coordinator_prompt()
        if self.permission_mode == "plan":
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt

        # Initialize client
        self._openai_client = openai.AsyncOpenAI(base_url=api_base, api_key=api_key)
        self._openai_messages.append({"role": "system", "content": self._system_prompt})

    @property
    def is_processing(self) -> bool:
        return self._current_task is not None and not self._current_task.done()

    def _build_side_query(self):
        """Build a sideQuery callable for memory recall."""
        client = self._openai_client
        model = self.model

        async def _sq_oai(system: str, user_message: str) -> str:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
            )
            return resp.choices[0].message.content or "" if resp.choices else ""

        return _sq_oai

    def abort(self) -> None:
        """Abort the current processing task."""
        self._aborted = True
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def set_confirm_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        self.confirm_fn = fn

    def set_plan_approval_fn(self, fn: Callable[[str], Awaitable[dict]]) -> None:
        self._plan_approval_fn = fn

    # ─── Plan mode toggle ────────────────────────────────────

    def toggle_plan_mode(self) -> str:
        if self.permission_mode == "plan":
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            if self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            print_info(f"Exited plan mode → {self.permission_mode} mode")
            return self.permission_mode
        else:
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            if self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            print_info(f"Entered plan mode. Plan file: {self._plan_file_path}")
            return "plan"

    def get_token_usage(self) -> dict:
        return {
            "input": self.total_input_tokens,
            "output": self.total_output_tokens,
            "cached_input": self.total_cached_input_tokens,
        }

    # ─── Main entry point ────────────────────────────────────

    async def chat(self, user_message: str) -> None:
        prompt_hook = await self._run_user_prompt_submit_hook(user_message)
        if prompt_hook["blocked"]:
            print_error(prompt_hook["message"])
            return
        user_message = prompt_hook["prompt"]

        # MCP在第一次真正聊天时才懒加载 (只有main agent加载MCP，避免重复加载)
        if not self._mcp_initialized and not self.is_sub_agent and not self.coordinator_mode:
            self._mcp_initialized = True
            try:
                await self._mcp_manager.load_and_connect()
                mcp_defs = self._mcp_manager.get_tool_definitions()
                if mcp_defs:
                    self.tools = self.tools + mcp_defs # 合并MCP工具定义
            except Exception as e:
                print(f"[mcp] Init failed: {e}", flush=True)

        self._aborted = False  # 重置取消标志
        self._current_task = asyncio.current_task()  # 记录当前任务，用于取消时判断
        try:
            await self._chat_openai(user_message)
        except asyncio.CancelledError:
            self._aborted = True
        finally: # 无论正常执行还是被打断，都执行
            self._current_task = None
        if not self.is_sub_agent: # 仅在main agent中自动保存
            print_divider()
            self._auto_save()

    def _hook_base_payload(self) -> dict:
        return {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "permission_mode": self.permission_mode,
            "model": self.model,
            "is_sub_agent": self.is_sub_agent,
        }

    async def _run_user_prompt_submit_hook(self, prompt: str) -> dict:
        hook = await run_hooks(
            "UserPromptSubmit",
            {**self._hook_base_payload(), "prompt": prompt},
        )
        if hook.denied:
            return {
                "blocked": True,
                "message": hook.message or "User prompt blocked by UserPromptSubmit hook.",
                "prompt": prompt,
            }
        if hook.updated_input and isinstance(hook.updated_input.get("prompt"), str):
            prompt = hook.updated_input["prompt"]
        if hook.additional_context:
            prompt = prompt + "\n\n<user-prompt-submit-hook>\n" + hook.additional_context + "\n</user-prompt-submit-hook>"
        return {"blocked": False, "message": "", "prompt": prompt}

    # ─── Sub-agent entry point ────────────────────────────────

    async def run_once(self, prompt: str) -> dict:
        self._output_buffer = []
        prev_in = self.total_input_tokens
        prev_out = self.total_output_tokens
        prev_cached_in = self.total_cached_input_tokens
        await self.chat(prompt)
        text = "".join(self._output_buffer)
        self._output_buffer = None
        return {
            "text": text,
            "tokens": {
                "input": self.total_input_tokens - prev_in,
                "output": self.total_output_tokens - prev_out,
                "cached_input": self.total_cached_input_tokens - prev_cached_in,
            },
        }

    # ─── Output helper ────────────────────────────────────────

    def _emit_text(self, text: str) -> None:
        if self._output_buffer is not None:
            self._output_buffer.append(text)
        else:
            print_assistant_text(text)

    # ─── REPL commands ────────────────────────────────────────

    def clear_history(self) -> None:
        self._openai_messages = []
        self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cached_input_tokens = 0
        self.last_input_token_count = 0
        self.last_cached_input_token_count = 0
        print_info("Conversation cleared.")

    def show_cost(self) -> None:
        total = self._get_current_cost_usd()
        budget_info = f" / ${self.max_cost_usd} budget" if self.max_cost_usd else ""
        turn_info = f" | Turns: {self.current_turns}/{self.max_turns}" if self.max_turns else ""
        cache_info = f" ({self.total_cached_input_tokens} cached)" if self.total_cached_input_tokens else ""
        print_info(f"Tokens: {self.total_input_tokens} in{cache_info} / {self.total_output_tokens} out\n  Estimated cost: ${total:.4f}{budget_info}{turn_info}")

    def show_tasks(self) -> None:
        self._sync_task_usage()
        print_info(self._task_manager.format_status())

    def show_teams(self) -> None:
        print_info(self._team_manager.format_status())

    def show_todos(self) -> None:
        print_info(self._format_todos())

    def _get_current_cost_usd(self) -> float:
        uncached_input = max(0, self.total_input_tokens - self.total_cached_input_tokens)
        discounted_cached_input = self.total_cached_input_tokens * 0.10
        return ((uncached_input + discounted_cached_input) / 1_000_000) * 3 + (self.total_output_tokens / 1_000_000) * 15

    def _check_budget(self) -> dict:
        if self.max_cost_usd is not None and self._get_current_cost_usd() >= self.max_cost_usd:
            return {"exceeded": True, "reason": f"Cost limit reached (${self._get_current_cost_usd():.4f} >= ${self.max_cost_usd})"}
        if self.max_turns is not None and self.current_turns >= self.max_turns:
            return {"exceeded": True, "reason": f"Turn limit reached ({self.current_turns} >= {self.max_turns})"}
        return {"exceeded": False}

    async def compact(self) -> None:
        await self._compact_conversation()

    # ─── Session ──────────────────────────────────────────────

    def restore_session(self, data: dict) -> None:
        if data.get("openaiMessages"):
            self._openai_messages = data["openaiMessages"]
        if isinstance(data.get("todos"), list):
            self._todos = data["todos"]
        print_info(f"Session restored ({self._get_message_count()} messages).")

    def _get_message_count(self) -> int:
        return len(self._openai_messages)

    def _auto_save(self) -> None:
        try:
            save_session(self.session_id, {
                "metadata": {
                    "id": self.session_id,
                    "model": self.model,
                    "cwd": str(Path.cwd()),
                    "startTime": self.session_start_time,
                    "messageCount": self._get_message_count(),
                },
                "openaiMessages": self._openai_messages,
                "todos": self._todos,
            })
        except Exception:
            pass

    # ─── Autocompact ──────────────────────────────────────────

    async def _check_and_compact(self) -> None:
        if self.last_input_token_count > self.effective_window * 0.85:
            print_info("Context window filling up, compacting conversation...")
            await self._compact_conversation()

    async def _compact_conversation(self) -> None:
        await self._compact_openai()
        print_info("Conversation compacted.")

    async def _compact_openai(self) -> None:
        # Invariant: caller must ensure the last message is a plain user-text
        # message (not a `tool` role result). Slicing off a tool result would
        # orphan the preceding assistant's tool_calls.
        if len(self._openai_messages) < 5:
            return
        system_msg = self._openai_messages[0]
        last_user_msg = self._openai_messages[-1]
        summary_resp = await self._openai_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a conversation summarizer. Be concise but preserve important details."},
                *self._openai_messages[1:-1],
                {"role": "user", "content": "Summarize the conversation so far in a concise paragraph, preserving key decisions, file paths, and context needed to continue the work."},
            ],
        )
        summary_text = summary_resp.choices[0].message.content or "No summary available."
        self._openai_messages = [
            system_msg,
            {"role": "user", "content": f"[Previous conversation summary]\n{summary_text}"},
            {"role": "assistant", "content": "Understood. I have the context from our previous conversation. How can I continue helping?"},
        ]
        if last_user_msg.get("role") == "user":
            self._openai_messages.append(last_user_msg)
        self.last_input_token_count = 0
        self.last_cached_input_token_count = 0

    # ─── Multi-tier compression pipeline ──────────────────────

    def _run_compression_pipeline(self) -> None:
        self._budget_tool_results_openai()
        self._snip_stale_results_openai()
        self._microcompact_openai()

    # Tier 1: Budget tool results
    def _budget_tool_results_openai(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < 0.5:
            return
        budget = 15000 if utilization > 0.7 else 30000
        for msg in self._openai_messages:
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and len(msg["content"]) > budget:
                keep = (budget - 80) // 2
                msg["content"] = msg["content"][:keep] + f"\n\n[... budgeted: {len(msg['content']) - keep * 2} chars truncated ...]\n\n" + msg["content"][-keep:]

    # Tier 2: Snip stale results
    def _snip_stale_results_openai(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return
        tool_msgs = []
        for i, msg in enumerate(self._openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] != SNIP_PLACEHOLDER:
                tool_msgs.append(i)
        if len(tool_msgs) <= KEEP_RECENT_RESULTS:
            return
        snip_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(snip_count):
            self._openai_messages[tool_msgs[i]]["content"] = SNIP_PLACEHOLDER

    # Tier 3: Microcompact
    def _microcompact_openai(self) -> None:
        if not self.last_api_call_time or (time.time() - self.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        tool_msgs = []
        for i, msg in enumerate(self._openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                tool_msgs.append(i)
        clear_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            self._openai_messages[tool_msgs[i]]["content"] = "[Old result cleared]"

    # ─── Large result persistence ─────────────────────────────────
    # When a tool result exceeds 30 KB, write it to disk and replace the
    # context entry with a short preview + file path.  The model can use
    # read_file to retrieve the full output later — no information is lost.

    def _persist_large_result(self, tool_name: str, result: str) -> str:
        THRESHOLD = 30 * 1024  # 30 KB
        if len(result.encode()) <= THRESHOLD:
            return result
        d = Path.home() / ".forgecc" / "tool-results"
        d.mkdir(parents=True, exist_ok=True)
        filename = f"{int(time.time() * 1000)}-{tool_name}.txt"
        filepath = d / filename
        filepath.write_text(result, encoding="utf-8")

        lines = result.split("\n")
        preview = "\n".join(lines[:200])
        size_kb = len(result.encode()) / 1024

        return (
            f"[Result too large ({size_kb:.1f} KB, {len(lines)} lines). "
            f"Full output saved to {filepath}. "
            f"You can use read_file to see the full result.]\n\n"
            f"Preview (first 200 lines):\n{preview}"
        )

    # ─── Execute tool (handles agent/skill/plan mode internally) ─────

    async def _execute_tool_call(self, name: str, inp: dict) -> str:
        if name in ("enter_plan_mode", "exit_plan_mode"):
            return await self._execute_plan_mode_tool(name)
        if name == "enter_coordinator_mode":
            return self._execute_enter_coordinator_mode(inp)
        if name == "agent":
            return await self._execute_agent_tool(inp)
        if name in TASK_TOOLS:
            return self._execute_task_tool(name, inp)
        if name in TEAM_TOOLS or name in TEAM_MEMBER_TOOLS:
            return await self._execute_team_tool(name, inp)
        if name in WORKTREE_TOOLS:
            return self._execute_worktree_tool(name, inp)
        if name == "todo_write":
            return self._execute_todo_write(inp)
        if name in MCP_RUNTIME_TOOLS:
            return await self._mcp_manager.call_runtime_tool(name, inp)
        if name == "skill":
            return await self._execute_skill_tool(inp)
        # Route MCP tool calls to the MCP manager
        if self._mcp_manager.is_mcp_tool(name):
            return await self._mcp_manager.call_tool(name, inp)
        return await execute_tool(name, inp, self._read_file_state, self.cwd)

    async def _run_pre_tool_use_hook(self, tool_name: str, inp: dict, tool_use_id: str) -> dict:
        hook = await run_hooks(
            "PreToolUse",
            {
                **self._hook_base_payload(),
                "tool_name": tool_name,
                "tool_input": inp,
                "tool_use_id": tool_use_id,
            },
            matcher_value=tool_name,
        )
        if hook.updated_input:
            inp = hook.updated_input
        if hook.denied:
            return {
                "allowed": False,
                "input": inp,
                "result": f"Action denied by PreToolUse hook: {hook.message or 'blocked by hook'}",
            }
        return {"allowed": True, "input": inp, "result": ""}

    async def _run_permission_request_hook(self, tool_name: str, inp: dict, tool_use_id: str, message: str) -> dict:
        hook = await run_hooks(
            "PermissionRequest",
            {
                **self._hook_base_payload(),
                "tool_name": tool_name,
                "tool_input": inp,
                "tool_use_id": tool_use_id,
                "message": message,
            },
            matcher_value=tool_name,
        )
        if hook.updated_input:
            inp = hook.updated_input
        if hook.allowed:
            return {"decision": "allow", "input": inp, "message": hook.message}
        if hook.denied:
            return {
                "decision": "deny",
                "input": inp,
                "message": hook.message or "Permission denied by PermissionRequest hook.",
            }
        return {"decision": "defer", "input": inp, "message": ""}

    async def _run_post_tool_failure_hook(
        self,
        tool_name: str,
        inp: dict,
        tool_use_id: str,
        error: str,
    ) -> str:
        hook = await run_hooks(
            "PostToolUseFailure",
            {
                **self._hook_base_payload(),
                "tool_name": tool_name,
                "tool_input": inp,
                "tool_use_id": tool_use_id,
                "error": {"message": error},
            },
            matcher_value=tool_name,
        )
        if hook.additional_context:
            return error + "\n\n<post-tool-use-failure-hook>\n" + hook.additional_context + "\n</post-tool-use-failure-hook>"
        if hook.message:
            return error + "\n\nPostToolUseFailure hook: " + hook.message
        return error

    async def _execute_tool_call_with_hooks(self, name: str, inp: dict, tool_use_id: str) -> str:
        try:
            raw = await self._execute_tool_call(name, inp)
        except Exception as exc:
            return await self._run_post_tool_failure_hook(name, inp, tool_use_id, str(exc))

        if self._is_tool_failure(raw):
            return await self._run_post_tool_failure_hook(name, inp, tool_use_id, raw)

        hook = await run_hooks(
            "PostToolUse",
            {
                **self._hook_base_payload(),
                "tool_name": name,
                "tool_input": inp,
                "tool_use_id": tool_use_id,
                "tool_response": raw,
            },
            matcher_value=name,
        )
        if hook.denied:
            message = hook.message or "PostToolUse hook blocked the completed tool result."
            return await self._run_post_tool_failure_hook(name, inp, tool_use_id, message)
        if hook.additional_context:
            raw = raw + "\n\n<post-tool-use-hook>\n" + hook.additional_context + "\n</post-tool-use-hook>"
        return raw

    def _is_tool_failure(self, result: str) -> bool:
        if not isinstance(result, str):
            return False
        failure_prefixes = (
            "Error:",
            "Error ",
            "Unknown tool:",
            "Action denied:",
            "User denied",
            "Warning:",
        )
        return result.startswith(failure_prefixes)

    # ─── Skill fork mode ─────────────────────────────────────

    async def _execute_skill_tool(self, inp: dict) -> str:
        from .skills import execute_skill
        result = execute_skill(inp.get("skill_name", ""), inp.get("args", ""))
        if not result:
            return f"Unknown skill: {inp.get('skill_name', '')}"

        if result["context"] == "fork":
            tools = (
                [t for t in self.tools if t["name"] in result["allowed_tools"]]
                if result.get("allowed_tools")
                else [t for t in self.tools if t["name"] != "agent"]
            )
            print_sub_agent_start("skill-fork", inp.get("skill_name", ""))
            sub_agent = Agent(
                model=self.model,
                api_base=self.api_base,
                api_key=self.api_key,
                custom_system_prompt=result["prompt"],
                custom_tools=tools,
                is_sub_agent=True,
                permission_mode=self.permission_mode,
                confirm_fn=self.confirm_fn,
            )
            try:
                sub_result = await sub_agent.run_once(inp.get("args") or "Execute this skill task.")
                self.total_input_tokens += sub_result["tokens"]["input"]
                self.total_output_tokens += sub_result["tokens"]["output"]
                self.total_cached_input_tokens += sub_result["tokens"].get("cached_input", 0)
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return sub_result["text"] or "(Skill produced no output)"
            except Exception as e:
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return f"Skill fork error: {e}"

        return f'[Skill "{inp.get("skill_name", "")}" activated]\n\n{result["prompt"]}'

    # ─── Plan mode helpers ──────────────────────────────────────

    def _generate_plan_file_path(self) -> str:
        d = Path.home() / ".claude" / "plans"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"plan-{self.session_id}.md")

    def _build_plan_mode_prompt(self) -> str:
        return f"""

# Plan Mode Active

Plan mode is active. You MUST NOT make any edits (except the plan file below), run non-readonly tools, or make any changes to the system.

## Plan File: {self._plan_file_path}
Write your plan incrementally to this file using write_file or edit_file. This is the ONLY file you are allowed to edit.

## Workflow
1. **Explore**: Read code to understand the task. Use read_file, list_files, grep_search.
2. **Design**: Design your implementation approach. Use the agent tool with type="plan" if the task is complex.
3. **Write Plan**: Write a structured plan to the plan file including:
   - **Context**: Why this change is needed
   - **Steps**: Implementation steps with critical file paths
   - **Verification**: How to test the changes
4. **Exit**: Call exit_plan_mode when your plan is ready for user review.

IMPORTANT: When your plan is complete, you MUST call exit_plan_mode. Do NOT ask the user to approve — exit_plan_mode handles that."""

    def _build_coordinator_prompt(self) -> str:
        return """

# Coordinator Mode Active

You are running as a coordinator for multi-agent work. Your job is to decompose
the user's request, launch focused sub-agents, monitor their progress, and
synthesize their findings into one concise final answer.

## Available actions
- Use agent with background=true to start independent workers.
- Use task_status to monitor all running workers.
- Use task_output to read completed worker results.
- Use task_stop only when a worker is clearly no longer useful.
- Use tool_search to load team tools when the user asks for a persistent swarm
  or autonomous agent team.
- Use team_create/team_status/team_message/team_wake/team_stop for persistent
  agent teams with a shared task board and mailbox.
- Use worktree_status/worktree_diff/worktree_commit/worktree_merge to review and
  integrate isolated write-capable team member worktrees.

## Constraints
- Do not try to edit files, read files, or run shell commands yourself.
- Delegate codebase exploration, planning, implementation, and verification to
  sub-agents.
- Keep team sizes small. Prefer 2-4 workers unless the task clearly needs more.
- Do not finish until all workers you spawned have completed, failed, or been
  stopped and you have read their outputs.
- Resolve conflicts between worker reports yourself and present the final
  recommendation or result to the user.
"""

    def _execute_enter_coordinator_mode(self, inp: dict) -> str:
        if self.coordinator_mode:
            return "Already in coordinator mode."
        self.coordinator_mode = True
        self.tools = [t for t in tool_definitions if t["name"] in ORCHESTRATION_TOOLS]
        self._base_system_prompt += self._build_coordinator_prompt()
        self._system_prompt = self._base_system_prompt
        if self.permission_mode == "plan":
            self._system_prompt += self._build_plan_mode_prompt()
        if self._openai_messages:
            self._openai_messages[0]["content"] = self._system_prompt
        reason = inp.get("reason") or "No reason provided."
        print_info(f"Entered coordinator mode: {reason}")
        return (
            "Entered coordinator mode. You now have only orchestration tools: "
            "agent, task_status, task_output, task_stop.\n"
            f"Reason: {reason}\n\n"
            "Decompose the task, launch background sub-agents where useful, "
            "monitor them, read their outputs, and synthesize the final answer."
        )

    async def _execute_plan_mode_tool(self, name: str) -> str:
        if name == "enter_plan_mode":
            if self.permission_mode == "plan":
                return "Already in plan mode."
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            if self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            print_info("Entered plan mode (read-only). Plan file: " + self._plan_file_path)
            return f"Entered plan mode. You are now in read-only mode.\n\nYour plan file: {self._plan_file_path}\nWrite your plan to this file. This is the only file you can edit.\n\nWhen your plan is complete, call exit_plan_mode."

        if name == "exit_plan_mode":
            if self.permission_mode != "plan":
                return "Not in plan mode."
            plan_content = "(No plan file found)"
            if self._plan_file_path and Path(self._plan_file_path).exists():
                plan_content = Path(self._plan_file_path).read_text()

            # Interactive approval flow
            if self._plan_approval_fn:
                result = await self._plan_approval_fn(plan_content)
                choice = result.get("choice", "manual-execute")

                if choice == "keep-planning":
                    feedback = result.get("feedback") or "Please revise the plan."
                    return (
                        f"User rejected the plan and wants to keep planning.\n\n"
                        f"User feedback: {feedback}\n\n"
                        f"Please revise your plan based on this feedback. When done, call exit_plan_mode again."
                    )

                # User approved — determine target mode
                if choice == "clear-and-execute":
                    target_mode = "acceptEdits"
                elif choice == "execute":
                    target_mode = "acceptEdits"
                else:  # manual-execute
                    target_mode = self._pre_plan_mode or "default"

                # Exit plan mode
                self.permission_mode = target_mode
                self._pre_plan_mode = None
                saved_plan_path = self._plan_file_path
                self._plan_file_path = None
                self._system_prompt = self._base_system_prompt
                if self._openai_messages:
                    self._openai_messages[0]["content"] = self._system_prompt

                if choice == "clear-and-execute":
                    self._clear_history_keep_system()
                    self._context_cleared = True
                    print_info(f"Plan approved. Context cleared, executing in {target_mode} mode.")
                    return (
                        f"User approved the plan. Context was cleared. Permission mode: {target_mode}\n\n"
                        f"Plan file: {saved_plan_path}\n\n"
                        f"## Approved Plan:\n{plan_content}\n\n"
                        f"Proceed with implementation."
                    )

                print_info(f"Plan approved. Executing in {target_mode} mode.")
                return (
                    f"User approved the plan. Permission mode: {target_mode}\n\n"
                    f"## Approved Plan:\n{plan_content}\n\n"
                    f"Proceed with implementation."
                )

            # Fallback: no approval function (e.g. sub-agents)
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            if self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            print_info("Exited plan mode. Restored to " + self.permission_mode + " mode.")
            return f"Exited plan mode. Permission mode restored to: {self.permission_mode}\n\n## Your Plan:\n{plan_content}"

        return f"Unknown plan mode tool: {name}"

    def _clear_history_keep_system(self) -> None:
        """Clear history but keep system prompt (used for clear-context plan approval)."""
        self._openai_messages = []
        self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self.last_input_token_count = 0
        self.last_cached_input_token_count = 0

    async def _execute_agent_tool(self, inp: dict) -> str:
        agent_type = inp.get("type", "general")
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")
        background = bool(inp.get("background"))

        print_sub_agent_start(agent_type, description)

        if background:
            async def _runner() -> dict:
                try:
                    return await self._run_sub_agent_once(agent_type, prompt)
                finally:
                    print_sub_agent_end(agent_type, description)

            record = self._task_manager.spawn(
                description=description,
                agent_type=agent_type,
                prompt=prompt,
                runner=_runner,
            )
            return (
                f"Started background sub-agent task.\n"
                f"task_id: {record.id}\n"
                f"status: {record.status}\n"
                f"Use task_status and task_output to monitor it."
            )

        try:
            result = await self._run_sub_agent_once(agent_type, prompt)
            self.total_input_tokens += result["tokens"]["input"]
            self.total_output_tokens += result["tokens"]["output"]
            self.total_cached_input_tokens += result["tokens"].get("cached_input", 0)
            print_sub_agent_end(agent_type, description)
            return result["text"] or "(Sub-agent produced no output)"
        except Exception as e:
            print_sub_agent_end(agent_type, description)
            return f"Sub-agent error: {e}"

    async def _run_sub_agent_once(self, agent_type: str, prompt: str) -> dict:
        config = get_sub_agent_config(agent_type)
        sub_agent = Agent(
            model=self.model,
            api_base=self.api_base,
            api_key=self.api_key,
            custom_system_prompt=config["system_prompt"],
            custom_tools=config["tools"],
            is_sub_agent=True,
            permission_mode=self.permission_mode,
            confirm_fn=self.confirm_fn,
        )
        return await sub_agent.run_once(prompt)

    def _execute_task_tool(self, name: str, inp: dict) -> str:
        self._sync_task_usage()
        task_id = inp.get("task_id")
        if name == "task_status":
            return self._task_manager.format_status(task_id)
        if name == "task_output":
            if not task_id:
                return "Error: task_id is required."
            return self._task_manager.format_output(task_id)
        if name == "task_stop":
            if not task_id:
                return "Error: task_id is required."
            if self._task_manager.stop(task_id):
                return f"Stop requested for task {task_id}."
            return f"Task {task_id} is not running or does not exist."
        return f"Unknown task tool: {name}"

    def _sync_task_usage(self) -> None:
        for record in self._task_manager.unaccounted_completed():
            self.total_input_tokens += record.tokens.get("input", 0)
            self.total_output_tokens += record.tokens.get("output", 0)
            self.total_cached_input_tokens += record.tokens.get("cached_input", 0)
            record.token_accounted = True

    def _execute_todo_write(self, inp: dict) -> str:
        todos = inp.get("todos")
        if not isinstance(todos, list):
            return "Error: todos must be a list."
        normalized: list[dict] = []
        in_progress = 0
        valid_status = {"pending", "in_progress", "completed"}
        valid_priority = {"low", "medium", "high"}
        for idx, item in enumerate(todos, start=1):
            if not isinstance(item, dict):
                return f"Error: todo #{idx} must be an object."
            content = str(item.get("content") or "").strip()
            status = str(item.get("status") or "pending")
            priority = str(item.get("priority") or "medium")
            if not content:
                return f"Error: todo #{idx} content is required."
            if status not in valid_status:
                return f"Error: invalid status for todo #{idx}: {status}"
            if priority not in valid_priority:
                return f"Error: invalid priority for todo #{idx}: {priority}"
            if status == "in_progress":
                in_progress += 1
            normalized.append({
                "id": str(item.get("id") or f"todo-{idx}"),
                "content": content,
                "status": status,
                "priority": priority,
            })
        if in_progress > 1:
            return "Error: only one todo can be in_progress at a time."
        self._todos = normalized
        return "Todo list updated.\n\n" + self._format_todos()

    def _format_todos(self) -> str:
        if not self._todos:
            return "No todos."
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        return "\n".join(
            f"{marker.get(todo['status'], '[ ]')} {todo['id']} ({todo['priority']}): {todo['content']}"
            for todo in self._todos
        )

    async def _execute_team_tool(self, name: str, inp: dict) -> str:
        if name in TEAM_MEMBER_TOOLS:
            return self._execute_team_member_tool(name, inp)

        if name == "team_create":
            team = self._team_manager.create_team(
                name=inp.get("name") or "Swarm Team",
                agents=inp.get("agents") or [],
                initial_tasks=inp.get("initial_tasks") or [],
            )
            self._ensure_team_worktrees(team.id)
            self._ensure_team_loops(team.id)
            for agent_name in team.agents:
                self._team_manager.wake_agent(team.id, agent_name)
            return (
                f"Created team {team.id}: {team.name}\n\n"
                f"{self._team_manager.format_status(team.id)}\n\n"
                "Team members are live and will autonomously read the board, claim tasks, send messages, and idle."
            )

        if name == "team_status":
            return self._team_manager.format_status(inp.get("team_id"))

        if name == "team_add_task":
            team_id = inp.get("team_id")
            task = self._team_manager.add_task(
                team_id,
                title=inp.get("title") or "Untitled task",
                description=inp.get("description") or "",
            )
            if not task:
                return f"Unknown team: {team_id}"
            self._ensure_team_loops(team_id)
            team = self._team_manager.get(team_id)
            if team:
                for agent_name in team.agents:
                    self._team_manager.wake_agent(team_id, agent_name)
            return f"Added task {task.id} to team {team_id}: {task.title}"

        if name == "team_message":
            team_id = inp.get("team_id")
            try:
                msg = self._team_manager.send_message(
                    team_id,
                    sender="coordinator",
                    recipient=inp.get("to") or "all",
                    content=inp.get("content") or "",
                    kind=inp.get("kind") or "REQUEST",
                    thread_id=inp.get("thread_id") or None,
                )
            except ValueError as exc:
                return f"Error: {exc}"
            if not msg:
                return f"Unknown team: {team_id}"
            self._ensure_team_loops(team_id)
            self._team_manager.wake_recipient(team_id, msg.recipient)
            return f"Sent message {msg.id} to {msg.recipient} in team {team_id}."

        if name == "team_wake":
            team_id = inp.get("team_id")
            self._ensure_team_worktrees(team_id)
            self._ensure_team_loops(team_id)
            team = self._team_manager.get(team_id)
            if not team:
                return f"Unknown team: {team_id}"
            target = inp.get("agent")
            if target:
                ok = self._team_manager.wake_agent(team_id, target)
                return f"Woke {target}." if ok else f"Unknown or inactive agent: {target}"
            for agent_name in team.agents:
                self._team_manager.wake_agent(team_id, agent_name)
            return f"Woke {len(team.agents)} agents in team {team_id}."

        if name == "team_stop":
            return self._team_manager.stop_team(inp.get("team_id") or "")

        if name == "team_compact":
            return self._team_manager.compact(inp.get("team_id") or "")

        return f"Unknown team tool: {name}"

    def _execute_team_member_tool(self, name: str, inp: dict) -> str:
        if not self.team_id or not self.team_member:
            return f"Error: {name} is only available inside a team member agent."

        if name == "team_read_board":
            return self._team_manager.format_board(self.team_id)

        if name == "team_read_messages":
            unread_only = inp.get("unread_only", True)
            messages = self._team_manager.read_messages(self.team_id, self.team_member, unread_only=bool(unread_only))
            if not messages:
                return "No messages."
            return "\n".join(
                self._team_manager.format_message(msg)
                for msg in messages
            )

        if name == "team_claim_task":
            task = self._team_manager.claim_task(self.team_id, self.team_member, inp.get("task_id"))
            if not task:
                return "No open task available to claim."
            return f"Claimed task {task.id}: {task.title}\n{task.description}"

        if name == "team_update_task":
            task = self._team_manager.update_task(
                self.team_id,
                self.team_member,
                task_id=inp.get("task_id") or "",
                status=inp.get("status") or "claimed",
                note=inp.get("note") or "",
                result=inp.get("result") or "",
            )
            if not task:
                return f"Unknown task: {inp.get('task_id')}"
            if task.status in {"done", "blocked"}:
                self._team_manager.send_message(
                    self.team_id,
                    sender=self.team_member,
                    recipient="all",
                    content=f"Task {task.id} is {task.status}: {task.title}",
                    kind="BLOCKED" if task.status == "blocked" else "STATUS",
                    thread_id=task.id,
                )
            return f"Updated task {task.id}: {task.status}"

        if name == "team_send_message":
            try:
                msg = self._team_manager.send_message(
                    self.team_id,
                    sender=self.team_member,
                    recipient=inp.get("to") or "all",
                    content=inp.get("content") or "",
                    kind=inp.get("kind") or "STATUS",
                    thread_id=inp.get("thread_id") or None,
                )
            except ValueError as exc:
                return f"Error: {exc}"
            if not msg:
                return f"Unknown team: {self.team_id}"
            return f"Sent message {msg.id} to {msg.recipient}."

        if name == "team_idle":
            reason = inp.get("reason") or "No active work."
            self._team_manager.request_idle(self.team_id, self.team_member)
            self._team_manager.mark_agent(self.team_id, self.team_member, "idle")
            return f"{self.team_member} is idle: {reason}"

        return f"Unknown team member tool: {name}"

    def _execute_worktree_tool(self, name: str, inp: dict) -> str:
        if name == "worktree_create":
            try:
                record = self._worktree_manager.create(
                    agent=inp.get("agent") or "",
                    branch=inp.get("branch") or None,
                    base_ref=inp.get("base_ref") or "HEAD",
                )
                return f"Worktree for {record.agent}\nbranch: {record.branch}\npath: {record.path}"
            except Exception as exc:
                return f"Error creating worktree: {exc}"
        if name == "worktree_status":
            return self._worktree_manager.status(inp.get("agent"))
        if name == "worktree_diff":
            return self._worktree_manager.diff(
                inp.get("agent") or "",
                max_chars=int(inp.get("max_chars") or 20000),
            )
        if name == "worktree_commit":
            return self._worktree_manager.commit(inp.get("agent") or "", inp.get("message") or "swarm worktree changes")
        if name == "worktree_merge":
            return self._worktree_manager.merge(inp.get("agent") or "")
        if name == "worktree_cleanup":
            return self._worktree_manager.cleanup(inp.get("agent") or "", force=bool(inp.get("force")))
        return f"Unknown worktree tool: {name}"

    def _ensure_team_worktrees(self, team_id: str | None) -> None:
        if not team_id:
            return
        team = self._team_manager.get(team_id)
        if not team:
            return
        changed = False
        for agent in team.agents.values():
            if not agent.worktree or agent.worktree_path:
                continue
            record = self._worktree_manager.create(agent=f"{team_id}-{agent.name}")
            agent.worktree_path = record.path
            agent.branch = record.branch
            changed = True
        if changed:
            self._team_manager._save(team_id)

    def _ensure_team_loops(self, team_id: str | None) -> None:
        if not team_id:
            return
        team = self._team_manager.get(team_id)
        if not team:
            return
        for agent_name, agent_state in team.agents.items():
            async def _runner(name: str = agent_name, state=agent_state) -> None:
                member_agent = Agent(
                    model=self.model,
                    api_base=self.api_base,
                    api_key=self.api_key,
                    custom_system_prompt=self._build_team_member_prompt(team_id, name, state.role),
                    custom_tools=self._build_team_member_tools(state.agent_type),
                    is_sub_agent=True,
                    permission_mode=self.permission_mode,
                    confirm_fn=self.confirm_fn,
                    team_manager=self._team_manager,
                    worktree_manager=self._worktree_manager,
                    team_id=team_id,
                    team_member=name,
                    cwd=state.worktree_path or self.cwd,
                )
                while True:
                    await self._team_manager.wait_for_wake(team_id, name)
                    self._team_manager.mark_agent(team_id, name, "running")
                    try:
                        await member_agent.chat(self._build_team_wake_prompt(team_id, name))
                        self._team_manager.update_agent_tokens(team_id, name, member_agent.get_token_usage())
                        self._team_manager.mark_agent(team_id, name, "idle")
                    except asyncio.CancelledError:
                        self._team_manager.mark_agent(team_id, name, "stopped")
                        raise
                    except Exception as exc:
                        self._team_manager.mark_agent(team_id, name, "idle", str(exc))

            self._team_manager.register_loop(team_id, agent_name, _runner)

    def _build_team_member_tools(self, agent_type: str) -> list[ToolDef]:
        config = get_sub_agent_config(agent_type)
        base_tools = [
            t for t in config["tools"]
            if t["name"] not in ORCHESTRATION_TOOLS and t["name"] not in TEAM_MEMBER_TOOLS
        ]
        member_tools = [
            {k: v for k, v in t.items() if k != "deferred"}
            for t in tool_definitions
            if t["name"] in TEAM_MEMBER_TOOLS
        ]
        return base_tools + member_tools

    def _build_team_member_prompt(self, team_id: str, agent_name: str, role: str) -> str:
        return f"""You are {agent_name}, a persistent member of ForgeCC team {team_id}.

Role: {role}

You participate in a shared task board and mailbox. On each wake:
1. Read your messages with team_read_messages.
2. Read the board with team_read_board.
3. Claim an open task if one matches your role, or continue your claimed task.
4. Use your normal code tools as needed.
5. Update task status with team_update_task.
6. Send messages to teammates when coordination is useful. Use kind=REQUEST
   when asking for work, REPLY when answering a message (include thread_id),
   STATUS for progress, and BLOCKED when blocked.
7. Call team_idle when there is no useful next action.

Do not wait for the coordinator to assign every step. Prefer small, clear task
updates and concise messages. If you are blocked, mark the task blocked with a
specific reason."""

    def _build_team_wake_prompt(self, team_id: str, agent_name: str) -> str:
        return (
            f"You have been woken in team {team_id} as {agent_name}. "
            "Check your mailbox and the shared board, act on useful work, then call team_idle when done."
        )

    # ─── OpenAI-compatible backend ───────────────────────────────

    async def _chat_openai(self, user_message: str) -> None:
        self._openai_messages.append({"role": "user", "content": user_message})
        # 检查容量，压缩上下文。
        # 放在用户消息追加之后，确保压缩时不容易拆散assistant tool call和tool result，最后一条消息永远是user message
        await self._check_and_compact()

        # 用户每输入新消息，就启动记忆预取
        # non-blocking，不会阻塞主循环
        memory_prefetch: MemoryPrefetch | None = None
        if not self.is_sub_agent:
            sq = self._build_side_query()
            if sq:
                memory_prefetch = start_memory_prefetch(
                    user_message, sq,
                    self._already_surfaced_memories, self._session_memory_bytes,
                )

        while True: # 核心agent loop
            if self._aborted:
                break

            self._run_compression_pipeline()

            # 使用记忆预取结果
            if memory_prefetch and memory_prefetch.settled and not memory_prefetch.consumed:
                memory_prefetch.consumed = True
                try:
                    memories = memory_prefetch.task.result()
                    if memories:
                        injection_text = format_memories_for_injection(memories)
                        last = self._openai_messages[-1] if self._openai_messages else None
                        if last and last.get("role") == "user":
                            last["content"] = (last.get("content") or "") + "\n\n" + injection_text
                        else:
                            self._openai_messages.append({"role": "user", "content": injection_text})
                        for m in memories:
                            # 记录注入过的memory，避免重复
                            self._already_surfaced_memories.add(m.path)
                            self._session_memory_bytes += len(m.content.encode())
                except Exception:
                    pass  # prefetch errors already logged

            if not self.is_sub_agent:
                start_spinner()

            response = await self._call_openai_stream()

            if not self.is_sub_agent:
                stop_spinner()

            self.last_api_call_time = time.time()

            if response.get("usage"):
                self.total_input_tokens += response["usage"]["prompt_tokens"]
                self.total_output_tokens += response["usage"]["completion_tokens"]
                self.total_cached_input_tokens += response["usage"].get("cached_prompt_tokens", 0)
                self.last_input_token_count = response["usage"]["prompt_tokens"]
                self.last_cached_input_token_count = response["usage"].get("cached_prompt_tokens", 0)

            choice = response.get("choices", [{}])[0] if response.get("choices") else {}
            message = choice.get("message", {})

            self._openai_messages.append(message) # 追加assistant消息

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                self._sync_task_usage()
                if not self.is_sub_agent:
                    print_cost(self.total_input_tokens, self.total_output_tokens, self.total_cached_input_tokens)
                break

            self.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                break

            # 有toolcall且不超过限额，继续loop
            # Phase 1: 解析，检查tool call权限
            oai_checked: list[dict] = []
            for tc in tool_calls:
                if self._aborted:
                    break
                if tc.get("type") != "function":
                    continue
                fn_name = tc["function"]["name"]
                try:
                    inp = json.loads(tc["function"]["arguments"]) # 尝试解析tool call参数
                except Exception:
                    inp = {}
                tool_use_id = tc.get("id", "")

                pre = await self._run_pre_tool_use_hook(fn_name, inp, tool_use_id)
                inp = pre["input"]
                print_tool_call(fn_name, inp)
                if not pre["allowed"]:
                    result = await self._run_post_tool_failure_hook(fn_name, inp, tool_use_id, pre["result"])
                    print_info(result)
                    oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": result})
                    continue

                perm = check_permission(fn_name, inp, self.permission_mode, self._plan_file_path)
                if perm["action"] == "deny":
                    denied = f"Action denied: {perm.get('message', '')}"
                    result = await self._run_post_tool_failure_hook(fn_name, inp, tool_use_id, denied)
                    print_info(f"Denied: {perm.get('message', '')}")
                    oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": result}) # 把拒绝原因作为tool result返回
                    continue
                if perm["action"] == "confirm" and perm.get("message") and perm["message"] not in self._confirmed_paths:
                    hook_decision = await self._run_permission_request_hook(fn_name, inp, tool_use_id, perm["message"])
                    inp = hook_decision["input"]
                    if hook_decision["decision"] == "deny":
                        result = await self._run_post_tool_failure_hook(fn_name, inp, tool_use_id, f"Action denied: {hook_decision['message']}")
                        oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": result})
                        continue
                    if hook_decision["decision"] == "allow":
                        self._confirmed_paths.add(perm["message"])
                        oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})
                        continue
                    confirmed = await self._confirm_dangerous(perm["message"]) # 需要用户确认危险操作
                    if not confirmed:
                        result = await self._run_post_tool_failure_hook(fn_name, inp, tool_use_id, "User denied this action.")
                        oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": result})
                        continue
                    self._confirmed_paths.add(perm["message"])
                oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})

            # Phase 2: 分组，执行tool call（并行执行连续安全工具）
            oai_batches: list[dict] = []
            for ct in oai_checked:
                safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS # 判断是否可执行，且并发安全
                if safe and oai_batches and oai_batches[-1]["concurrent"]: # 可并发执行的工具合并到一个batch里
                    oai_batches[-1]["items"].append(ct)
                else: # 串行执行
                    oai_batches.append({"concurrent": safe, "items": [ct]})

            oai_context_break = False
            for batch in oai_batches:
                if oai_context_break or self._aborted:
                    break

                if batch["concurrent"]:
                    async def _run_oai_safe(ct_item: dict) -> tuple[dict, str]:
                        raw = await self._execute_tool_call_with_hooks(ct_item["fn"], ct_item["inp"], ct_item["tc"].get("id", "")) # 执行工具
                        res = self._persist_large_result(ct_item["fn"], raw) # 处理过大的工具结果
                        print_tool_result(ct_item["fn"], res) # 打印工具结果
                        return ct_item, res

                    results = await asyncio.gather(*[_run_oai_safe(ct) for ct in batch["items"]]) # 并发执行多个工具
                    for ct_item, res in results:
                        self._openai_messages.append({"role": "tool", "tool_call_id": ct_item["tc"]["id"], "content": res})
                else:
                    for ct in batch["items"]:
                        if not ct["allowed"]:
                            self._openai_messages.append({"role": "tool", "tool_call_id": ct["tc"]["id"], "content": ct["result"]})
                            continue
                        raw = await self._execute_tool_call_with_hooks(ct["fn"], ct["inp"], ct["tc"].get("id", ""))
                        res = self._persist_large_result(ct["fn"], raw)
                        print_tool_result(ct["fn"], res)

                        if self._context_cleared: # plan mode的清空上下文再执行的情况
                            self._context_cleared = False
                            self._openai_messages.append({"role": "user", "content": res})
                            oai_context_break = True
                            break
                        self._openai_messages.append({"role": "tool", "tool_call_id": ct["tc"]["id"], "content": res})

            self._context_cleared = False

    async def _call_openai_stream(self) -> dict:
        async def _do(): # 交给_with_retry，请求失败可以自动重试
            kwargs = {
                "model": self.model,
                "tools": _to_openai_tools(get_active_tool_definitions(self.tools)), # 把system prompt中tool定义转换为OpenAI的tool格式
                "messages": self._openai_messages,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if self._prompt_cache_extra_body:
                kwargs["extra_body"] = self._prompt_cache_extra_body
            try:
                stream = await self._openai_client.chat.completions.create(**kwargs) # 启动请求
            except Exception as error:
                if "extra_body" not in kwargs or "prompt_cache" not in str(error):
                    raise
                extra_body = kwargs.get("extra_body") or {}
                if "prompt_cache_retention" in extra_body:
                    self._prompt_cache_extra_body = {k: v for k, v in extra_body.items() if k != "prompt_cache_retention"}
                    kwargs["extra_body"] = self._prompt_cache_extra_body
                else:
                    kwargs.pop("extra_body", None)
                    self._prompt_cache_extra_body = None
                stream = await self._openai_client.chat.completions.create(**kwargs)

            content = ""
            first_text = True # 判断是不是第一次收到文本，用来控制spinner和换行
            tool_calls: dict[int, dict] = {}
            finish_reason = ""
            usage = None

            async for chunk in stream:
                if chunk.usage: # 如果chunk带usage信息，就统计
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "cached_prompt_tokens": _get_cached_prompt_tokens(chunk.usage),
                    }

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta # 拿到这次的增量内容

                if delta and delta.content: # 如果是文本流，就打印给用户
                    if first_text:
                        stop_spinner()
                        self._emit_text("\n")
                        first_text = False
                    self._emit_text(delta.content)
                    content += delta.content

                if delta and delta.tool_calls: # 如果要求tool call，
                    for tc in delta.tool_calls:
                        existing = tool_calls.get(tc.index) # OpenAI streaming用index表示第几个tool call，这里找到正在流式输出那个
                        if existing:
                            if tc.function and tc.function.arguments:
                                existing["arguments"] += tc.function.arguments # 流式的是分开打印的，要拼到一起
                        else: # 如果还不存在，就新建一个tool call记录
                            tool_calls[tc.index] = {
                                "id": tc.id or "",
                                "name": (tc.function.name if tc.function else "") or "",
                                "arguments": (tc.function.arguments if tc.function else "") or "",
                            }

                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

            assembled = None
            if tool_calls:
                assembled = [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for _, tc in sorted(tool_calls.items()) # 把分片的tool call转成普通response格式，按index排序
                ]

            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": assembled,
                    },
                    "finish_reason": finish_reason or "stop",
                }],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "cached_prompt_tokens": 0},
            }

        return await _with_retry(_do) # 重试机制

    # ─── Shared ──────────────────────────────────────────────────

    async def _confirm_dangerous(self, command: str) -> bool:
        print_confirmation(command)
        if self.confirm_fn:
            return await self.confirm_fn(command)
        # Fallback: blocking input
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False
