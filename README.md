# ForgeCC

ForgeCC 是一个基于 Python 的 coding agent 学习项目，参考 `claude-code-from-scratch` 的实现思路整理而来。当前根目录只保留一份 Python 实现，CLI 入口为 `cca`。

这个项目的重点不是做一个生产级 Agent，而是把 coding agent 的核心结构摊开：消息循环、工具调用、权限确认、上下文压缩、会话保存、记忆、skills、Plan Mode、Sub-Agent、Coordinator/Swarm multi-agent 编排和 MCP 集成。

## 功能

- CLI：一次性 prompt 和交互式 REPL
- OpenAI-compatible `/chat/completions` 后端
- 工具系统：文件读写、精确编辑、列表、搜索、shell、web_fetch、todo_write、skill、sub-agent、plan mode、tool_search
- 权限模式：默认确认、只读计划、自动批准编辑、跳过确认、自动拒绝
- 上下文管理：大输出裁剪、microcompact、自动 compact、预算和轮次限制
- 会话持久化：`--resume` 恢复最近会话
- 记忆系统：按项目保存记忆并注入 prompt
- Skills：从 `.claude/skills/<name>/SKILL.md` 发现可复用指令
- Coordinator：支持显式 `--coordinator` 或模型通过 `enter_coordinator_mode` 自主切换，将任务拆给后台 sub-agent 并汇总结果
- Swarm Team：持久化 agent team、共享任务板、固定消息协议、mailbox、idle/wake loop、自主 claim/update task
- Worktree backend：写入型 team member 可在独立 git worktree 中修改代码，主 Agent review diff 后再 commit/merge
- MCP：从 `.claude/settings.json` 或 `~/.claude/settings.json` 加载外部工具服务器，支持 tools、resources、subscriptions、polling 和 OAuth/token 状态检查
- Hooks：从 `.claude/settings.json` 或 `~/.claude/settings.json` 加载安全/权限 hook

## 安装

目标环境：

- macOS
- Shell：zsh
- Conda 环境：`cc`
- Python 3.11+

```bash
conda activate cc
pip install -e .
```

## 配置

OpenAI-compatible 后端：

```bash
export OPENAI_API_KEY="your-key"
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_MODEL="gpt-4.1-mini"
```

使用官方 OpenAI API 时，ForgeCC 会发送稳定的 prompt cache key，并在 `/cost` 中显示 cached input tokens。ForgeCC 默认不主动设置 retention；如需请求 24h retention，可设置：

```bash
export CCA_PROMPT_CACHE_RETENTION="24h"
```

CLI 参数可以覆盖模型和 API 地址：

```bash
cca --model gpt-4.1-mini --api-base https://api.openai.com/v1 "hello"
```

## 使用

一次性运行：

```bash
cca "解释这个项目的 agent loop"
```

进入交互式 REPL：

```bash
cca
```

常用选项：

```bash
cca --plan "先分析如何重构工具系统"
cca --accept-edits "修复测试失败"
cca --yolo "运行测试并修复问题"
cca --dont-ask "只做静态检查"
cca --coordinator "让多个 agent 并行审查这个仓库"
cca --resume
cca --max-cost 0.50 --max-turns 20 "实现一个小功能"
```

REPL 内置命令：

```text
/clear      清空会话历史
/plan       切换只读计划模式
/cost       查看 token 和估算成本
/compact    手动压缩上下文
/todos      查看当前会话 todo_write 清单
/tasks      查看后台 sub-agent 任务
/teams      查看持久化 agent team
/memory     列出记忆
/skills     列出 skills
exit        退出
```

会话保存在 `~/.forgecc/sessions`，工具大结果和记忆也保存在 `~/.forgecc` 下。项目级 team/worktree 状态保存在当前仓库的 `.forgecc/teams` 和 `.forgecc/worktrees` 下，便于 agent team 与 git worktree 后端围绕当前项目工作。

## Multi-agent

ForgeCC 当前包含三层 multi-agent 能力：

- Sub-Agent fork-return：`agent` 工具启动独立上下文完成搜索、分析、计划或一般任务，结果返回主会话。
- Coordinator：主 Agent 进入编排模式后，只保留 orchestration 工具，负责拆分任务、启动后台 sub-agent、查看 `task_status` / `task_output` 并汇总结果。
- Swarm Team：`team_create` 创建持久化 team，成员通过共享 task board 和 mailbox 协作，可自主 `team_claim_task`、`team_update_task`、`team_send_message`、`team_idle`。

Team mailbox 使用固定消息协议：

```text
REQUEST  请求某个 agent 或全体执行/协助一项工作
REPLY    回复某条消息，必须带 thread_id
STATUS   报告进度或完成情况
BLOCKED  报告阻塞和原因
```

写入型 team member 可设置 `worktree=true`，ForgeCC 会为其创建独立 git worktree。常用 review / integration 工具：

```text
worktree_status
worktree_diff
worktree_commit
worktree_merge
worktree_cleanup
```

`worktree_commit`、`worktree_merge`、`worktree_cleanup` 在普通权限模式下会触发确认；Plan Mode 下只允许查看 status/diff。

## MCP Runtime

MCP 配置仍从 `.claude/settings.json`、`~/.claude/settings.json` 或 `.mcp.json` 读取。除了把 MCP server tools 暴露为 `mcp__server__tool`，ForgeCC 还支持：

```text
mcp_list_resources
mcp_read_resource
mcp_subscribe_resource
mcp_unsubscribe_resource
mcp_poll
mcp_oauth_status
```

其中 `mcp_oauth_status` 只检查配置和 token/env 状态；CLI 当前不执行浏览器式 OAuth 授权流。

## 安全 Hooks

ForgeCC 支持 Claude Code 风格的 hook 配置，重点接入安全和权限相关事件：

- `UserPromptSubmit`：用户输入进入模型前触发，可记录 prompt、阻断敏感信息、追加短上下文。
- `PreToolUse`：工具执行前触发，可阻断危险 shell、`.env` 访问、越界写入。
- `PermissionRequest`：需要权限确认时触发，可自动 allow/deny 或继续交给用户确认。
- `PostToolUse`：工具成功后触发，可做写入后的 secret scan、代码安全检查、审计记录。
- `PostToolUseFailure`：工具失败、被拒或被 hook 阻断后触发，可记录结构化安全事件。

仓库自带的 `.claude/settings.json` 已启用默认安全策略，脚本在 `.claude/hooks/`：

- `pre_tool_use.py`：阻断危险 shell、敏感文件访问、越界写入。
- `permission_request.py`：对需要确认的操作做二次安全判断。
- `post_tool_use.py`：检查写入内容和工具输出中的 secret-like 内容。
- `post_tool_use_failure.py`：记录失败、拒绝、阻断事件。
- `user_prompt_submit.py`：阻断用户 prompt 中疑似直接粘贴的 secret。

也可以按项目需要调整 `.claude/settings.json`：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "run_shell",
        "hooks": [
          {"type": "command", "command": "python .claude/hooks/pre_tool_use.py"}
        ]
      }
    ],
    "PermissionRequest": [
      {
        "matcher": "",
        "hooks": [
          {"type": "command", "command": "python .claude/hooks/permission_request.py"}
        ]
      }
    ]
  }
}
```

Hook 命令会从 stdin 收到 JSON payload。返回 exit code `2` 会阻断当前事件；也可以向 stdout 输出 JSON，例如：

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {
      "behavior": "deny",
      "message": "Reading .env is not allowed"
    }
  }
}
```

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
  tasks.py         后台 sub-agent task runtime
  teams.py         Swarm team、task board、mailbox、idle/wake runtime
  worktrees.py     Git worktree 隔离写入 backend
  mcp_client.py    MCP JSON-RPC over stdio 客户端
  hooks.py         安全/权限 hook 加载和执行
  ui.py            终端输出
```

说明性教程文档保存在 `docs/`。这些文档来自参考项目，用于学习 agent 架构；根项目的可运行实现以 `src/mini_coding_agent/` 为准。

## 验证

```bash
conda run -n cc python -m compileall src
conda run -n cc cca --help
```

如果修改了 LLM 交互、工具调用或权限逻辑，建议再用真实或假的 OpenAI-compatible endpoint 做一次端到端测试。
