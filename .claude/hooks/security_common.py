from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any


SENSITIVE_NAME_RE = re.compile(
    r"(^|[/\\])(\.env($|[./\\])|\.env\.(?!sample$|example$|template$)|id_rsa$|id_ed25519$|.*\.pem$|.*\.p12$|.*\.pfx$)",
    re.IGNORECASE,
)

SECRET_TEXT_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9_./+=:-]{20,}"),
]

DANGEROUS_SHELL_PATTERNS = [
    re.compile(r"\brm\s+.*-[A-Za-z]*r[A-Za-z]*f"),
    re.compile(r"\brm\s+.*-[A-Za-z]*f[A-Za-z]*r"),
    re.compile(r"\brm\s+--recursive\s+--force"),
    re.compile(r"\brm\s+--force\s+--recursive"),
    re.compile(r"\bdel\s+/(s|q)\b", re.IGNORECASE),
    re.compile(r"\bRemove-Item\b.*\s-Recurse\b.*\s-Force\b", re.IGNORECASE),
    re.compile(r"\b(curl|wget)\b.+\|\s*(sh|bash)\b", re.IGNORECASE),
    re.compile(r"\bInvoke-WebRequest\b.+\|\s*iex\b", re.IGNORECASE),
    re.compile(r"\biwr\b.+\|\s*iex\b", re.IGNORECASE),
]


def load_stdin() -> dict[str, Any]:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def tool_name(data: dict[str, Any]) -> str:
    return str(data.get("tool_name") or "")


def tool_input(data: dict[str, Any]) -> dict[str, Any]:
    inp = data.get("tool_input")
    return inp if isinstance(inp, dict) else {}


def normalized_tool(name: str) -> str:
    aliases = {
        "Bash": "run_shell",
        "Read": "read_file",
        "Write": "write_file",
        "Edit": "edit_file",
        "MultiEdit": "edit_file",
        "Glob": "list_files",
        "Grep": "grep_search",
    }
    return aliases.get(name, name)


def append_log(event_name: str, data: dict[str, Any], decision: str | None = None, message: str | None = None) -> None:
    try:
        log_dir = Path.home() / ".forgecc" / "hook-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{event_name}.jsonl"
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "decision": decision,
            "message": message,
            "data": data,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def block(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def json_decision(behavior: str, message: str | None = None, updated_input: dict[str, Any] | None = None) -> None:
    decision: dict[str, Any] = {"behavior": behavior}
    if message:
        decision["message"] = message
    if updated_input is not None:
        decision["updatedInput"] = updated_input
    print(json.dumps({"hookSpecificOutput": {"decision": decision}}))


def is_sensitive_path(path_value: str) -> bool:
    if not path_value:
        return False
    normalized = path_value.replace("\\", "/")
    if normalized.endswith((".env.sample", ".env.example", ".env.template")):
        return False
    return bool(SENSITIVE_NAME_RE.search(normalized))


def command_touches_sensitive_file(command: str) -> bool:
    if not command:
        return False
    if re.search(r"\.env\.(sample|example|template)\b", command, re.IGNORECASE):
        return False
    return bool(re.search(r"(^|[\s/\\])\.env\b|id_rsa\b|id_ed25519\b|\.pem\b|\.p12\b|\.pfx\b", command, re.IGNORECASE))


def is_dangerous_shell(command: str) -> bool:
    normalized = " ".join(command.strip().split())
    return any(pattern.search(normalized) for pattern in DANGEROUS_SHELL_PATTERNS)


def text_has_secret(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in SECRET_TEXT_PATTERNS)


def file_path_from_input(inp: dict[str, Any]) -> str:
    value = inp.get("file_path") or inp.get("path") or ""
    return str(value)


def is_allowed_write_path(path_value: str) -> bool:
    if not path_value:
        return True
    try:
        target = Path(path_value).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        target = target.resolve()
        allowed_roots = [
            Path.cwd().resolve(),
            (Path.home() / ".claude" / "plans").resolve(),
            (Path.home() / ".forgecc" / "projects").resolve(),
        ]
        return any(target == root or root in target.parents for root in allowed_roots)
    except Exception:
        return False


def content_from_write_input(inp: dict[str, Any]) -> str:
    parts = []
    for key in ("content", "new_string", "old_string"):
        value = inp.get(key)
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)
