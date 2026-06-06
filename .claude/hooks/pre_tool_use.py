from __future__ import annotations

from security_common import (
    append_log,
    block,
    command_touches_sensitive_file,
    file_path_from_input,
    is_allowed_write_path,
    is_dangerous_shell,
    is_sensitive_path,
    load_stdin,
    normalized_tool,
    tool_input,
    tool_name,
)


def main() -> None:
    data = load_stdin()
    name = normalized_tool(tool_name(data))
    inp = tool_input(data)

    message = None
    if name == "run_shell":
        command = str(inp.get("command") or "")
        if is_dangerous_shell(command):
            message = "PreToolUse blocked a dangerous shell command."
        elif command_touches_sensitive_file(command):
            message = "PreToolUse blocked shell access to sensitive file material."
    elif name in {"read_file", "write_file", "edit_file"}:
        path = file_path_from_input(inp)
        if is_sensitive_path(path):
            message = "PreToolUse blocked access to sensitive file material."
        elif name in {"write_file", "edit_file"} and not is_allowed_write_path(path):
            message = "PreToolUse blocked a write outside the allowed project/memory/plan paths."

    if message:
        append_log("pre_tool_use", data, "deny", message)
        block(message)
    append_log("pre_tool_use", data, "allow", None)


if __name__ == "__main__":
    main()
