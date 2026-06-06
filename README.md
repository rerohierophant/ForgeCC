# ForgeCC

ForgeCC 是一个基于 Python 的 coding agent 学习项目，参考 `claude-code-from-scratch` 的实现思路整理而来。当前根目录只保留一份 Python 实现，CLI 入口为 `cca`。

这个项目的重点不是做一个生产级 Agent，而是把 coding agent 的核心结构摊开：消息循环、工具调用、权限确认、上下文压缩、会话保存、记忆、skills、Plan Mode、Sub-Agent 和 MCP 集成。

## 功能

- CLI：一次性 prompt 和交互式 REPL
- OpenAI-compatible `/chat/completions` 后端
- 工具系统：文件读写、精确编辑、列表、搜索、shell、web_fetch、skill、sub-agent、plan mode、tool_search
- 权限模式：默认确认、只读计划、自动批准编辑、跳过确认、自动拒绝
- 上下文管理：大输出裁剪、microcompact、自动 compact、预算和轮次限制
- 会话持久化：`--resume` 恢复最近会话
- 记忆系统：按项目保存记忆并注入 prompt
- Skills：从 `.claude/skills/<name>/SKILL.md` 发现可复用指令
- MCP：从 `.claude/settings.json` 或 `~/.claude/settings.json` 加载外部工具服务器

## 安装

目标环境：

- Windows
- Conda 环境：`cc`
- Python 3.11+

```powershell
conda activate cc
pip install -e .
```

## 配置

OpenAI-compatible 后端：

```powershell
$env:OPENAI_API_KEY="your-key"
$env:OPENAI_BASE_URL="https://api.openai.com/v1"
$env:OPENAI_MODEL="gpt-4.1-mini"
```

使用官方 OpenAI API 时，ForgeCC 会发送稳定的 prompt cache key，并在 `/cost` 中显示 cached input tokens。ForgeCC 默认不主动设置 retention；如需请求 24h retention，可设置：

```powershell
$env:CCA_PROMPT_CACHE_RETENTION="24h"
```

CLI 参数可以覆盖模型和 API 地址：

```powershell
cca --model gpt-4.1-mini --api-base https://api.openai.com/v1 "hello"
```

## 使用

一次性运行：

```powershell
cca "解释这个项目的 agent loop"
```

进入交互式 REPL：

```powershell
cca
```

常用选项：

```powershell
cca --plan "先分析如何重构工具系统"
cca --accept-edits "修复测试失败"
cca --yolo "运行测试并修复问题"
cca --dont-ask "只做静态检查"
cca --resume
cca --max-cost 0.50 --max-turns 20 "实现一个小功能"
```

REPL 内置命令：

```text
/clear      清空会话历史
/plan       切换只读计划模式
/cost       查看 token 和估算成本
/compact    手动压缩上下文
/memory     列出记忆
/skills     列出 skills
exit        退出
```

会话保存在 `~/.forgecc/sessions`，工具大结果和记忆也保存在 `~/.forgecc` 下。

## 项目结构

```text
src/mini_coding_agent/
  __main__.py      CLI 参数解析和 REPL
  cli.py           cca 命令入口 wrapper
  agent.py         Agent loop、OpenAI 后端、流式输出、压缩、预算、Plan Mode
  tools.py         工具定义、执行和权限检查
  prompt.py        System Prompt 构造，加载 AGENTS.md / CLAUDE.md / rules
  session.py       会话持久化
  memory.py        项目记忆
  skills.py        skills 发现和解析
  subagent.py      子 Agent 配置
  mcp_client.py    MCP JSON-RPC over stdio 客户端
  ui.py            终端输出
```

说明性教程文档保存在 `docs/`。这些文档来自参考项目，用于学习 agent 架构；根项目的可运行实现以 `src/mini_coding_agent/` 为准。

## 验证

```powershell
conda run -n cc python -m compileall src
conda run -n cc cca --help
```

如果修改了 LLM 交互、工具调用或权限逻辑，建议再用真实或假的 OpenAI-compatible endpoint 做一次端到端测试。
