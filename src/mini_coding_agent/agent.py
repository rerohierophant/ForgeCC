from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import AgentConfig
from .llm import DeltaCallback, LLMError, Message, OpenAICompatibleClient
from .prompt import build_system_prompt
from .tools import TOOL_SCHEMAS, ToolError, ToolExecutor, parse_tool_arguments


ApprovalCallback = Callable[[str, dict[str, Any]], bool]


class Agent:
    def __init__(
        self,
        config: AgentConfig,
        *,
        cwd: Path | None = None,
        approve_tool_call: ApprovalCallback | None = None,
    ) -> None:
        self.config = config
        self.cwd = cwd or Path.cwd()
        self.approve_tool_call = approve_tool_call
        self.client = OpenAICompatibleClient(
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            temperature=config.temperature,
        )
        self.tools = ToolExecutor(
            self.cwd,
            approve=approve_tool_call,
        )
        self.messages: list[Message] = [
            {"role": "system", "content": build_system_prompt(self.cwd)}
        ]

    def clear(self) -> None:
        self.messages = [{"role": "system", "content": build_system_prompt(self.cwd)}]
        self.tools = ToolExecutor(
            self.cwd,
            approve=self.approve_tool_call,
        )

    def ask(self, user_input: str, *, on_delta: DeltaCallback | None = None) -> str:
        original_message_count = len(self.messages)
        self.messages.append({"role": "user", "content": user_input})

        for _ in range(self.config.max_turns):
            try:
                if on_delta is None:
                    response = self.client.chat(self.messages, tools=TOOL_SCHEMAS)
                else:
                    response = self.client.chat_stream(
                        self.messages,
                        tools=TOOL_SCHEMAS,
                        on_delta=on_delta,
                    )
            except LLMError:
                self.messages = self.messages[:original_message_count]
                raise

            assistant_message: Message = {
                "role": "assistant",
                "content": response.content,
            }
            if response.tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.name,
                            "arguments": tool_call.arguments,
                        },
                    }
                    for tool_call in response.tool_calls
                ]
            self.messages.append(assistant_message)

            if not response.tool_calls:
                return response.content

            for tool_call in response.tool_calls:
                try:
                    arguments = parse_tool_arguments(tool_call.arguments)
                    tool_result = self.tools.execute(tool_call.name, arguments)
                except ToolError as exc:
                    tool_result = f"Error: {exc}"

                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result,
                    }
                )

        raise RuntimeError("Agent reached max_turns without producing a response.")
