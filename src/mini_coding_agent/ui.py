from __future__ import annotations

import sys
from typing import Any


class Console:
    def info(self, message: str) -> None:
        print(message)

    def user_prompt(self) -> str:
        return input("\n你 > ").strip()

    def assistant(self, message: str) -> None:
        print(f"\nAgent > {message}")

    def assistant_prefix(self) -> None:
        print("\nAgent > ", end="", flush=True)

    def assistant_delta(self, text: str) -> None:
        print(text, end="", flush=True)

    def assistant_done(self) -> None:
        print()

    def error(self, message: str) -> None:
        print(f"\nError: {message}", file=sys.stderr)

    def approve_tool_call(self, name: str, arguments: dict[str, Any]) -> bool:
        print(f"\nTool request: {name}")
        print(_summarize_tool_arguments(name, arguments))
        answer = input("Allow? [y/N] ").strip().lower()
        return answer in {"y", "yes"}


HELP_TEXT = """Commands:
  /help    Show this help
  /clear   Clear current conversation history
  /config  Show model configuration
  /exit    Exit interactive mode
"""


def _summarize_tool_arguments(name: str, arguments: dict[str, Any]) -> str:
    if name == "run_shell":
        return f"  command: {arguments.get('command', '')}"

    if name == "write_file":
        content = arguments.get("content", "")
        content_length = len(content) if isinstance(content, str) else "unknown"
        return (
            f"  path: {arguments.get('path', '')}\n"
            f"  content_length: {content_length}"
        )

    if name == "edit_file":
        old_text = arguments.get("old_text", "")
        new_text = arguments.get("new_text", "")
        old_length = len(old_text) if isinstance(old_text, str) else "unknown"
        new_length = len(new_text) if isinstance(new_text, str) else "unknown"
        return (
            f"  path: {arguments.get('path', '')}\n"
            f"  old_text_length: {old_length}\n"
            f"  new_text_length: {new_length}"
        )

    pairs = [f"  {key}: {value}" for key, value in arguments.items()]
    return "\n".join(pairs)
