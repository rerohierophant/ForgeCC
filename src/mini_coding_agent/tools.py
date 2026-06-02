from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


MAX_TOOL_OUTPUT_CHARS = 12000
MAX_FILE_READ_CHARS = 20000
MAX_LIST_ENTRIES = 200
MAX_GREP_MATCHES = 100

ApprovalCallback = Callable[[str, dict[str, Any]], bool]


class ToolError(RuntimeError):
    pass


@dataclass
class ToolState:
    read_mtimes: dict[Path, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file inside the current workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path to read.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a UTF-8 text file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete file contents to write.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Edit a UTF-8 text file by replacing one exact text block. "
                "Use read_file first so the old_text is known exactly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path to edit.",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Exact text to replace. It must occur once.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory to list.",
                        "default": ".",
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Search text files in the workspace for a literal string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Literal text to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file or directory to search.",
                        "default": ".",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Whether matching should be case-sensitive.",
                        "default": True,
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command in the workspace after user approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command line to execute.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout in seconds, from 1 to 120.",
                        "default": 30,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
]


class ToolExecutor:
    def __init__(
        self,
        cwd: Path,
        *,
        approve: ApprovalCallback | None = None,
        state: ToolState | None = None,
    ) -> None:
        self.cwd = cwd.resolve()
        self.approve = approve or (lambda _name, _args: False)
        self.state = state or ToolState()
        self.handlers = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "list_files": self._list_files,
            "grep_search": self._grep_search,
            "run_shell": self._run_shell,
        }

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        handler = self.handlers.get(name)
        if handler is None:
            return f"Error: unknown tool {name!r}."

        try:
            result = handler(arguments)
        except ToolError as exc:
            result = f"Error: {exc}"

        return _truncate(result, MAX_TOOL_OUTPUT_CHARS)

    def _read_file(self, arguments: dict[str, Any]) -> str:
        path = self._resolve_path(_require_str(arguments, "path"))
        if not path.is_file():
            raise ToolError(f"{_display_path(path, self.cwd)} is not a file.")

        content = _read_text(path)
        self.state.read_mtimes[path] = path.stat().st_mtime_ns
        header = f"File: {_display_path(path, self.cwd)}"
        return f"{header}\n\n{_truncate(content, MAX_FILE_READ_CHARS)}"

    def _write_file(self, arguments: dict[str, Any]) -> str:
        path = self._resolve_path(_require_str(arguments, "path"))
        content = _require_str(arguments, "content")
        self._require_approval("write_file", arguments)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.state.read_mtimes[path] = path.stat().st_mtime_ns
        return f"Wrote {len(content)} characters to {_display_path(path, self.cwd)}."

    def _edit_file(self, arguments: dict[str, Any]) -> str:
        path = self._resolve_path(_require_str(arguments, "path"))
        old_text = _require_str(arguments, "old_text")
        new_text = _require_str(arguments, "new_text")
        if old_text == "":
            raise ToolError("old_text cannot be empty.")
        if not path.is_file():
            raise ToolError(f"{_display_path(path, self.cwd)} is not a file.")

        self._ensure_not_changed_since_read(path)
        content = _read_text(path)
        count = content.count(old_text)
        if count == 0:
            raise ToolError("old_text was not found in the file.")
        if count > 1:
            raise ToolError(
                f"old_text occurs {count} times. Provide a more specific block."
            )

        self._require_approval("edit_file", arguments)
        updated = content.replace(old_text, new_text, 1)
        path.write_text(updated, encoding="utf-8")
        self.state.read_mtimes[path] = path.stat().st_mtime_ns
        return (
            f"Edited {_display_path(path, self.cwd)}. "
            f"Replaced {len(old_text)} characters with {len(new_text)} characters."
        )

    def _list_files(self, arguments: dict[str, Any]) -> str:
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string.")

        path = self._resolve_path(raw_path)
        if path.is_file():
            return _display_path(path, self.cwd)
        if not path.is_dir():
            raise ToolError(f"{_display_path(path, self.cwd)} is not a directory.")

        entries: list[str] = []
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(
                d for d in dirs if not _should_skip(Path(root) / d)
            )
            visible_files = sorted(
                f for f in files if not _should_skip(Path(root) / f)
            )

            rel_root = Path(root).resolve().relative_to(path)
            indent_level = 0 if str(rel_root) == "." else len(rel_root.parts)
            if str(rel_root) != ".":
                entries.append(f"{'  ' * (indent_level - 1)}{rel_root.parts[-1]}/")

            for filename in visible_files:
                entries.append(f"{'  ' * indent_level}{filename}")
                if len(entries) >= MAX_LIST_ENTRIES:
                    entries.append(f"... truncated after {MAX_LIST_ENTRIES} entries")
                    return "\n".join(entries)

            if len(entries) >= MAX_LIST_ENTRIES:
                break

        if not entries:
            return f"No files found in {_display_path(path, self.cwd)}."
        return "\n".join(entries)

    def _grep_search(self, arguments: dict[str, Any]) -> str:
        pattern = _require_str(arguments, "pattern")
        if pattern == "":
            raise ToolError("pattern cannot be empty.")

        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string.")

        case_sensitive = arguments.get("case_sensitive", True)
        if not isinstance(case_sensitive, bool):
            raise ToolError("case_sensitive must be a boolean.")

        search_path = self._resolve_path(raw_path)
        files = [search_path] if search_path.is_file() else _iter_text_files(search_path)
        needle = pattern if case_sensitive else pattern.lower()
        matches: list[str] = []

        for file_path in files:
            try:
                lines = _read_text(file_path).splitlines()
            except ToolError:
                continue

            for line_no, line in enumerate(lines, start=1):
                haystack = line if case_sensitive else line.lower()
                if needle in haystack:
                    matches.append(
                        f"{_display_path(file_path, self.cwd)}:{line_no}: {line}"
                    )
                    if len(matches) >= MAX_GREP_MATCHES:
                        matches.append(
                            f"... truncated after {MAX_GREP_MATCHES} matches"
                        )
                        return "\n".join(matches)

        if not matches:
            return f"No matches for {pattern!r}."
        return "\n".join(matches)

    def _run_shell(self, arguments: dict[str, Any]) -> str:
        command = _require_str(arguments, "command")
        if command.strip() == "":
            raise ToolError("command cannot be empty.")

        timeout = arguments.get("timeout_seconds", 30)
        if not isinstance(timeout, int):
            raise ToolError("timeout_seconds must be an integer.")
        timeout = max(1, min(timeout, 120))
        approved_args = {"command": command, "timeout_seconds": timeout}
        self._require_approval("run_shell", approved_args)

        try:
            completed = subprocess.run(
                command,
                cwd=self.cwd,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            return (
                f"Command timed out after {timeout} seconds.\n"
                f"{_truncate(output, MAX_TOOL_OUTPUT_CHARS)}"
            )

        output_parts = [
            f"exit_code={completed.returncode}",
            "",
            "stdout:",
            completed.stdout or "",
            "",
            "stderr:",
            completed.stderr or "",
        ]
        return "\n".join(output_parts).strip()

    def _resolve_path(self, raw_path: str) -> Path:
        if raw_path.strip() == "":
            raise ToolError("path cannot be empty.")

        path = Path(raw_path)
        if not path.is_absolute():
            path = self.cwd / path
        resolved = path.resolve()
        if not resolved.is_relative_to(self.cwd):
            raise ToolError("path must stay inside the current workspace.")
        return resolved

    def _ensure_not_changed_since_read(self, path: Path) -> None:
        last_read_mtime = self.state.read_mtimes.get(path)
        if last_read_mtime is None:
            raise ToolError("read_file must be called before edit_file for this path.")
        current_mtime = path.stat().st_mtime_ns
        if current_mtime != last_read_mtime:
            raise ToolError(
                "file changed since the last read_file call. Read it again before editing."
            )

    def _require_approval(self, name: str, arguments: dict[str, Any]) -> None:
        if not self.approve(name, arguments):
            raise ToolError(f"user rejected {name}.")


def parse_tool_arguments(raw_arguments: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as exc:
        raise ToolError(f"invalid tool arguments JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ToolError("tool arguments must be a JSON object.")
    return parsed


def _require_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str):
        raise ToolError(f"{key} must be a string.")
    return value


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"{_display_path(path, path.parent)} is not valid UTF-8.") from exc
    except OSError as exc:
        raise ToolError(str(exc)) from exc


def _iter_text_files(path: Path) -> list[Path]:
    if not path.exists():
        raise ToolError(f"{path} does not exist.")
    if not path.is_dir():
        raise ToolError(f"{path} is not a directory.")

    files: list[Path] = []
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if not _should_skip(Path(root) / d)]
        for name in names:
            file_path = Path(root) / name
            if not _should_skip(file_path):
                files.append(file_path.resolve())
    return files


def _should_skip(path: Path) -> bool:
    return path.name in {".git", "__pycache__", ".mypy_cache", ".pytest_cache"} or (
        path.suffix in {".pyc", ".pyo", ".so", ".dll", ".exe"}
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n... truncated {omitted} characters ..."


def _display_path(path: Path, cwd: Path) -> str:
    try:
        return str(path.resolve().relative_to(cwd.resolve()))
    except ValueError:
        return str(path)
