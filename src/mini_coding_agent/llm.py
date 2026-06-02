from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


Message = dict[str, Any]
DeltaCallback = Callable[[str], None]


@dataclass(frozen=True)
class LLMToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ChatResponse:
    content: str
    tool_calls: list[LLMToolCall]
    raw: dict[str, Any]


@dataclass
class _ToolCallParts:
    id: str = ""
    name: str = ""
    arguments: str = ""


class LLMError(RuntimeError):
    pass


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float,
        timeout: int = 120,
    ) -> None:
        try:
            from openai import APIConnectionError, APIError, APITimeoutError, OpenAI
        except ModuleNotFoundError as exc:
            raise LLMError(
                "Missing dependency: openai. Install the project with `pip install -e .`."
            ) from exc

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            timeout=timeout,
        )
        self.model = model
        self.temperature = temperature
        self._api_timeout_error = APITimeoutError
        self._api_connection_error = APIConnectionError
        self._api_error = APIError

    def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                temperature=self.temperature,
            )
        except self._api_timeout_error as exc:
            raise LLMError("LLM request timed out.") from exc
        except self._api_connection_error as exc:
            raise LLMError(f"LLM connection failed: {exc}") from exc
        except self._api_error as exc:
            raise LLMError(f"LLM API error: {exc}") from exc

        message = response.choices[0].message
        content = message.content or ""
        tool_calls = [
            LLMToolCall(
                id=tool_call.id,
                name=tool_call.function.name,
                arguments=tool_call.function.arguments,
            )
            for tool_call in message.tool_calls or []
        ]

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            raw=response.model_dump(),
        )

    def chat_stream(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> ChatResponse:
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                temperature=self.temperature,
                stream=True,
            )

            content_parts: list[str] = []
            tool_call_parts: dict[int, _ToolCallParts] = {}

            for chunk in stream:
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                if delta.content:
                    content_parts.append(delta.content)
                    if on_delta is not None:
                        on_delta(delta.content)

                for tool_call_delta in delta.tool_calls or []:
                    index = tool_call_delta.index
                    parts = tool_call_parts.setdefault(index, _ToolCallParts())
                    if tool_call_delta.id:
                        parts.id += tool_call_delta.id
                    if tool_call_delta.function is not None:
                        if tool_call_delta.function.name:
                            parts.name += tool_call_delta.function.name
                        if tool_call_delta.function.arguments:
                            parts.arguments += tool_call_delta.function.arguments

        except self._api_timeout_error as exc:
            raise LLMError("LLM request timed out.") from exc
        except self._api_connection_error as exc:
            raise LLMError(f"LLM connection failed: {exc}") from exc
        except self._api_error as exc:
            raise LLMError(f"LLM API error: {exc}") from exc

        tool_calls = [
            LLMToolCall(
                id=parts.id,
                name=parts.name,
                arguments=parts.arguments,
            )
            for _index, parts in sorted(tool_call_parts.items())
        ]

        return ChatResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            raw={"stream": True},
        )
