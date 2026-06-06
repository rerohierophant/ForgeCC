from __future__ import annotations

from security_common import append_log, load_stdin


def main() -> None:
    data = load_stdin()
    append_log("post_tool_use_failure", data, "record", None)


if __name__ == "__main__":
    main()
