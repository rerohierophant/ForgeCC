# Mini Coding Agent Development Notes

This project is a learning-oriented Python implementation of a coding agent MVP.

## Current Scope

The first milestone focuses on the minimum loop needed to make the CLI usable:

- CLI entry point named `cca`
- One-shot prompt mode
- Interactive REPL mode
- Message history
- System prompt construction
- OpenAI-compatible `/chat/completions` backend

Tool calling is intentionally not implemented yet. The next milestone should add tools in this order:

1. `list_files`
2. `read_file`
3. `grep_search`
4. `edit_file`
5. `run_shell`

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

The app reads OpenAI-compatible settings from environment variables:

```powershell
$env:OPENAI_API_KEY="your-key"
$env:OPENAI_BASE_URL="https://api.openai.com/v1"
$env:OPENAI_MODEL="gpt-4.1-mini"
```

CLI flags can override these values:

```powershell
cca --model gpt-4.1-mini "hello"
```

## Design Principles

- Keep each milestone small enough to understand by reading the code directly.
- Prefer standard-library code until an external dependency clearly improves learning or reliability.
- Keep the agent loop explicit instead of hiding it behind a framework.
- Keep tools isolated behind small handler functions.
- Validate tool inputs before execution.
- Truncate large model or tool outputs before adding them to history.
- Ask for permission before file writes or shell execution unless an explicit auto-approve mode is added.

## Code Layout

```text
src/mini_coding_agent/
  cli.py       CLI parsing and REPL
  agent.py     Message history and agent loop
  llm.py       OpenAI-compatible HTTP client
  config.py    Environment and CLI configuration
  prompt.py    System prompt builder
  ui.py        Terminal input/output helpers
```

## Verification

Before handing off a change, run:

```powershell
conda run -n cc python -m compileall src
conda run -n cc cca --help
```

If LLM behavior changes, test against a real or fake OpenAI-compatible endpoint.
