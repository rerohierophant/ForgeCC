# ForgeCC Development Notes

ForgeCC is a learning-oriented Python coding agent. The current project keeps one implementation under `src/mini_coding_agent`; the imported `claude-code-from-scratch` repository is not part of the final tree.

## Current Scope

The CLI entry point is `cca`. The implementation follows the Python version of `claude-code-from-scratch` while keeping this project's package name and OpenAI-compatible defaults.

Implemented areas:

- One-shot prompt mode and interactive REPL
- Message history and JSON session persistence
- OpenAI-compatible chat backend
- Streaming assistant output
- Tool calling with permission modes
- File tools: `list_files`, `read_file`, `write_file`, `edit_file`, `grep_search`
- Runtime tools: `run_shell`, `web_fetch`, `tool_search`
- Agent features: Plan Mode, memory, skills, sub-agents, MCP, compacting, cost and turn limits

## Python Environment

Target environment:

- Windows
- Conda environment: `cc`
- Python 3.11+

Typical setup:

```powershell
conda activate cc
pip install -e .
```

## Configuration

OpenAI-compatible settings:

```powershell
$env:OPENAI_API_KEY="your-key"
$env:OPENAI_BASE_URL="https://api.openai.com/v1"
$env:OPENAI_MODEL="gpt-4.1-mini"
```

CLI flags can override model and API base URL:

```powershell
cca --model gpt-4.1-mini --api-base https://api.openai.com/v1 "hello"
```

## Design Principles

- Keep the agent loop explicit and readable.
- Prefer standard-library code unless a dependency directly supports the learning goal or reliability.
- Keep tool definitions and handlers isolated in `tools.py`.
- Validate tool inputs before execution.
- Truncate or persist large model/tool outputs before they bloat history.
- Ask for permission before file writes or shell execution unless the selected permission mode explicitly allows it.
- Preserve `AGENTS.md` as the primary project instruction file; `CLAUDE.md` is also loaded for compatibility.

## Code Layout

```text
src/mini_coding_agent/
  __main__.py      CLI parsing and REPL
  cli.py           installed cca wrapper
  agent.py         agent loop, model backends, compression, plan mode
  tools.py         tool schemas, permission checks, tool handlers
  prompt.py        system prompt builder
  session.py       saved sessions
  memory.py        project memory
  skills.py        skill discovery and invocation
  subagent.py      sub-agent prompts and custom agent loading
  mcp_client.py    MCP client
  ui.py            terminal rendering helpers
```

## Verification

Before handing off a change, run:

```powershell
conda run -n cc python -m compileall src
conda run -n cc cca --help
```

If LLM behavior changes, test against a real or fake OpenAI-compatible endpoint.
