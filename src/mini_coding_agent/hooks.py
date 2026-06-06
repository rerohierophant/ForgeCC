"""Configurable hook runner for ForgeCC security and permission events."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any


HOOK_EVENTS = {
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PostToolUseFailure",
}


@dataclass
class HookResult:
    blocked: bool = False
    message: str = ""
    additional_context: str = ""
    updated_input: dict[str, Any] | None = None
    permission_behavior: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.permission_behavior == "allow"

    @property
    def denied(self) -> bool:
        return self.blocked or self.permission_behavior == "deny"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _load_hook_entries(event_name: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in (Path.home() / ".claude" / "settings.json", Path.cwd() / ".claude" / "settings.json"):
        settings = _load_json(path)
        hooks = settings.get("hooks") if settings else None
        configured = hooks.get(event_name) if isinstance(hooks, dict) else None
        if isinstance(configured, list):
            entries.extend(item for item in configured if isinstance(item, dict))
    return entries


def _matches(entry: dict[str, Any], matcher_value: str | None) -> bool:
    matcher = entry.get("matcher", "")
    if matcher in ("", None):
        return True
    if matcher_value is None:
        return False
    matcher = str(matcher)
    return matcher == matcher_value or fnmatch(matcher_value, matcher)


def _iter_commands(event_name: str, matcher_value: str | None) -> list[str]:
    commands: list[str] = []
    for entry in _load_hook_entries(event_name):
        if not _matches(entry, matcher_value):
            continue
        for hook in entry.get("hooks", []):
            if not isinstance(hook, dict) or hook.get("type", "command") != "command":
                continue
            command = hook.get("command")
            if isinstance(command, str) and command.strip():
                commands.append(command)
    return commands


def _base_payload(event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    data.setdefault("hook_event_name", event_name)
    data.setdefault("cwd", str(Path.cwd()))
    data.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return data


def _expanded_command(command: str, payload: dict[str, Any]) -> str:
    project_dir = str(Path.cwd())
    session_id = str(payload.get("session_id", ""))
    replacements = {
        "$CLAUDE_PROJECT_DIR": project_dir,
        "${CLAUDE_PROJECT_DIR}": project_dir,
        "%CLAUDE_PROJECT_DIR%": project_dir,
        "$FORGECC_PROJECT_DIR": project_dir,
        "${FORGECC_PROJECT_DIR}": project_dir,
        "%FORGECC_PROJECT_DIR%": project_dir,
        "$CLAUDE_SESSION_ID": session_id,
        "${CLAUDE_SESSION_ID}": session_id,
        "%CLAUDE_SESSION_ID%": session_id,
        "$FORGECC_SESSION_ID": session_id,
        "${FORGECC_SESSION_ID}": session_id,
        "%FORGECC_SESSION_ID%": session_id,
    }
    for old, new in replacements.items():
        command = command.replace(old, new)
    return command


def _apply_json_output(result: HookResult, data: dict[str, Any]) -> None:
    hook_output = data.get("hookSpecificOutput")
    decision = None
    if isinstance(hook_output, dict):
        decision = hook_output.get("decision")
        extra = hook_output.get("additionalContext") or hook_output.get("additional_context")
        if isinstance(extra, str) and extra.strip():
            result.additional_context += extra.strip() + "\n"

    if not isinstance(decision, dict):
        decision = data.get("decision")
    if isinstance(decision, str):
        decision = {"behavior": decision}

    if isinstance(decision, dict):
        behavior = str(decision.get("behavior") or decision.get("action") or "").lower()
        if behavior in ("allow", "deny"):
            result.permission_behavior = behavior
        if behavior in ("block", "blocked", "deny"):
            result.blocked = True
        updated = decision.get("updatedInput") or decision.get("updated_input")
        if isinstance(updated, dict):
            result.updated_input = updated
        msg = decision.get("message") or decision.get("reason")
        if isinstance(msg, str) and msg.strip():
            result.message = msg.strip()

    updated_input = data.get("updatedInput") or data.get("updated_input")
    if isinstance(updated_input, dict):
        result.updated_input = updated_input

    extra = data.get("additionalContext") or data.get("additional_context")
    if isinstance(extra, str) and extra.strip():
        result.additional_context += extra.strip() + "\n"

    behavior = data.get("behavior") or data.get("action")
    if isinstance(behavior, str):
        behavior = behavior.lower()
        if behavior in ("allow", "deny"):
            result.permission_behavior = behavior
        if behavior in ("block", "blocked", "deny"):
            result.blocked = True

    msg = data.get("message") or data.get("reason")
    if isinstance(msg, str) and msg.strip() and not result.message:
        result.message = msg.strip()


async def run_hooks(
    event_name: str,
    payload: dict[str, Any],
    *,
    matcher_value: str | None = None,
) -> HookResult:
    """Run configured hooks for an event.

    Hook commands receive JSON on stdin. Exit code 2 blocks the event. JSON stdout
    may return a Claude-style hookSpecificOutput object or a small generic shape:
    {"behavior": "deny", "message": "...", "updated_input": {...}}.
    """
    result = HookResult()
    if event_name not in HOOK_EVENTS:
        return result

    commands = _iter_commands(event_name, matcher_value)
    if not commands:
        return result

    data = _base_payload(event_name, payload)
    stdin_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
    timeout = float(os.environ.get("CCA_HOOK_TIMEOUT", "10"))

    for command in commands:
        expanded = _expanded_command(command, data)
        env = {
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(Path.cwd()),
            "FORGECC_PROJECT_DIR": str(Path.cwd()),
            "CLAUDE_SESSION_ID": str(data.get("session_id", "")),
            "FORGECC_SESSION_ID": str(data.get("session_id", "")),
            "FORGECC_HOOK_EVENT": event_name,
        }
        try:
            process = await asyncio.create_subprocess_shell(
                expanded,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path.cwd()),
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin_bytes), timeout=timeout)
        except asyncio.TimeoutError:
            result.errors.append(f"Hook timed out after {timeout:g}s: {expanded}")
            continue
        except Exception as exc:
            result.errors.append(f"Hook failed to start: {expanded}: {exc}")
            continue

        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if out:
            try:
                parsed = json.loads(out)
            except json.JSONDecodeError:
                result.additional_context += out + "\n"
            else:
                if isinstance(parsed, dict):
                    _apply_json_output(result, parsed)
                else:
                    result.additional_context += out + "\n"

        if process.returncode == 2:
            result.blocked = True
            if err:
                result.message = err
            elif out and not result.message:
                result.message = out
            break
        if process.returncode not in (0, None):
            msg = err or out or f"exit code {process.returncode}"
            result.errors.append(f"Hook exited non-zero: {expanded}: {msg}")

        if result.denied:
            break

    result.additional_context = result.additional_context.strip()
    return result
