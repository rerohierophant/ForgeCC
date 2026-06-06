from __future__ import annotations

from security_common import (
    append_log,
    command_touches_sensitive_file,
    file_path_from_input,
    is_allowed_write_path,
    is_dangerous_shell,
    is_sensitive_path,
    json_decision,
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
            message = "PermissionRequest denied a dangerous shell command."
        elif command_touches_sensitive_file(command):
            message = "PermissionRequest denied shell access to sensitive file material."
    elif name in {"read_file", "write_file", "edit_file"}:
        path = file_path_from_input(inp)
        if is_sensitive_path(path):
            message = "PermissionRequest denied sensitive file access."
        elif name in {"write_file", "edit_file"} and not is_allowed_write_path(path):
            message = "PermissionRequest denied write outside allowed paths."

    if message:
        append_log("permission_request", data, "deny", message)
        json_decision("deny", message)
        return
    append_log("permission_request", data, "defer", None)


if __name__ == "__main__":
    main()
