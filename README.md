# Mini Coding Agent

一个用于学习 coding agent 工作原理的 Python CLI MVP。

当前版本实现了一个带基础工具调用的 coding agent loop：

- CLI 入口：一次性提问和交互模式
- Agent loop：维护消息历史，循环调用 LLM 和工具
- LLM 后端适配：基于 `openai` Python SDK 的 OpenAI-compatible `/chat/completions`
- 流式输出：模型回复会边生成边打印
- 基础命令：`/help`、`/clear`、`/config`、`/exit`
- 基础工具：`list_files`、`read_file`、`grep_search`、`write_file`、`edit_file`、`run_shell`

写文件、编辑文件和执行 shell 命令前会询问用户是否允许。

## Conda 环境

你已经有 conda 环境 `cc`，在 PowerShell 里执行：

```powershell
conda activate cc
pip install -e .
```

## 配置模型

PowerShell 临时设置环境变量：

```powershell
$env:OPENAI_API_KEY="你的 key"
$env:OPENAI_BASE_URL="https://api.openai.com/v1"
$env:OPENAI_MODEL="gpt-4.1-mini"
```

如果你使用其它 OpenAI-compatible 服务，把 `OPENAI_BASE_URL` 和 `OPENAI_MODEL` 换成对应值即可。

也可以用命令行参数覆盖：

```powershell
cca --model gpt-4.1-mini "解释一下 agent loop 是什么"
cca --base-url https://api.openai.com/v1
```

## 使用

一次性提问：

```powershell
cca "用三句话解释 coding agent MVP"
```

进入交互模式：

```powershell
cca
```

交互模式内置命令：

```text
/help     显示帮助
/clear    清空当前会话历史
/config   显示当前模型配置
/exit     退出
```

## 项目结构

```text
src/mini_coding_agent/
  __init__.py
  __main__.py
  agent.py
  cli.py
  config.py
  llm.py
  prompt.py
  tools.py
  ui.py
```

## 设计说明

coding agent 的核心闭环现在分成三步：

1. CLI 接收用户输入并维护交互体验。
2. Agent 把消息历史和工具定义发送给模型。
3. 模型请求工具时，Agent 执行工具、把结果加入历史，再继续调用模型直到得到最终回答。

工具实现保持在 `tools.py` 中，方便继续扩展下一批能力。
