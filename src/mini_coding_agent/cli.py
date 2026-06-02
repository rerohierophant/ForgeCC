from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from .agent import Agent
from .config import AgentConfig
from .llm import LLMError
from .ui import HELP_TEXT, Console


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cca",
        description="Mini Coding Agent: a tiny Python CLI coding agent MVP.",
    )
    parser.add_argument("prompt", nargs="*", help="Prompt to run once.")
    parser.add_argument("--api-key", help="API key. Defaults to OPENAI_API_KEY.")
    parser.add_argument("--base-url", help="Base URL. Defaults to OPENAI_BASE_URL.")
    parser.add_argument("--model", help="Model name. Defaults to OPENAI_MODEL.")
    parser.add_argument("--max-turns", type=int, help="Agent loop max turns.")
    parser.add_argument("--temperature", type=float, help="Sampling temperature.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()

    config = AgentConfig.from_env(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        max_turns=args.max_turns,
        temperature=args.temperature,
    )

    try:
        config.validate()
    except ValueError as exc:
        console.error(str(exc))
        return 2

    agent = Agent(config, cwd=Path.cwd(), approve_tool_call=console.approve_tool_call)
    prompt = " ".join(args.prompt).strip()

    if prompt:
        return run_once(agent, prompt, console)

    return run_repl(agent, console)


def run_once(agent: Agent, prompt: str, console: Console) -> int:
    on_delta, finish_stream = build_stream_handlers(console)
    try:
        answer = agent.ask(prompt, on_delta=on_delta)
    except (LLMError, RuntimeError) as exc:
        finish_stream()
        console.error(str(exc))
        return 1
    finish_stream()
    if not answer:
        console.assistant("")
    return 0


def run_repl(agent: Agent, console: Console) -> int:
    console.info("Mini Coding Agent started. Type /help for commands, /exit to quit.")

    while True:
        try:
            user_input = console.user_prompt()
        except (EOFError, KeyboardInterrupt):
            console.info("\nBye.")
            return 0

        if not user_input:
            continue

        if user_input in {"/exit", "/quit", "exit", "quit"}:
            console.info("Bye.")
            return 0

        if user_input == "/help":
            console.info(HELP_TEXT)
            continue

        if user_input == "/clear":
            agent.clear()
            console.info("Conversation cleared.")
            continue

        if user_input == "/config":
            console.info(
                f"model={agent.config.model}\n"
                f"base_url={agent.config.base_url}\n"
                f"max_turns={agent.config.max_turns}\n"
                f"temperature={agent.config.temperature}"
            )
            continue

        on_delta, finish_stream = build_stream_handlers(console)
        try:
            answer = agent.ask(user_input, on_delta=on_delta)
        except (LLMError, RuntimeError) as exc:
            finish_stream()
            console.error(str(exc))
            continue

        finish_stream()
        if not answer:
            console.assistant("")


def build_stream_handlers(console: Console) -> tuple[Callable[[str], None], Callable[[], None]]:
    started = False

    def on_delta(text: str) -> None:
        nonlocal started
        if not started:
            console.assistant_prefix()
            started = True
        console.assistant_delta(text)

    def finish_stream() -> None:
        if started:
            console.assistant_done()

    return on_delta, finish_stream


if __name__ == "__main__":
    sys.exit(main())
