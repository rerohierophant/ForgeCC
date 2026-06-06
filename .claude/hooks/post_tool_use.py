from __future__ import annotations

from security_common import (
    append_log,
    content_from_write_input,
    json_decision,
    load_stdin,
    normalized_tool,
    text_has_secret,
    tool_input,
    tool_name,
)


def main() -> None:
    data = load_stdin()
    name = normalized_tool(tool_name(data))
    inp = tool_input(data)
    response = str(data.get("tool_response") or "")

    message = None
    if name in {"write_file", "edit_file"} and text_has_secret(content_from_write_input(inp)):
        message = "PostToolUse found secret-like material in written content. Remove it immediately."
    elif text_has_secret(response):
        message = "PostToolUse found secret-like material in tool output. Do not repeat or expose it."

    if message:
        append_log("post_tool_use", data, "deny", message)
        json_decision("deny", message)
        return
    append_log("post_tool_use", data, "allow", None)


if __name__ == "__main__":
    main()
