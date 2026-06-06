from __future__ import annotations

from security_common import append_log, block, load_stdin, text_has_secret


def main() -> None:
    data = load_stdin()
    prompt = str(data.get("prompt") or "")
    if text_has_secret(prompt):
        message = "UserPromptSubmit blocked prompt containing secret-like material."
        append_log("user_prompt_submit", data, "deny", message)
        block(message)
    append_log("user_prompt_submit", data, "allow", None)


if __name__ == "__main__":
    main()
