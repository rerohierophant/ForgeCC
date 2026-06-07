"""Persistent team runtime for swarm-style multi-agent coordination."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable


AgentLoop = Callable[[], Awaitable[None]]
MESSAGE_KINDS = {"REQUEST", "REPLY", "STATUS", "BLOCKED"}


def _now() -> float:
    return time.time()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return slug[:40] or uuid.uuid4().hex[:8]


@dataclass
class TeamAgent:
    name: str
    role: str
    agent_type: str = "general"
    worktree: bool = False
    worktree_path: str | None = None
    branch: str | None = None
    status: str = "idle"
    wake_count: int = 0
    last_error: str | None = None
    tokens: dict[str, int] = field(default_factory=dict)


@dataclass
class TeamTask:
    id: str
    title: str
    description: str
    status: str = "open"
    assignee: str | None = None
    result: str | None = None
    history: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)


@dataclass
class TeamMessage:
    id: str
    sender: str
    recipient: str
    content: str
    kind: str = "STATUS"
    thread_id: str | None = None
    created_at: float = field(default_factory=_now)
    read_by: list[str] = field(default_factory=list)


@dataclass
class Team:
    id: str
    name: str
    agents: dict[str, TeamAgent] = field(default_factory=dict)
    tasks: dict[str, TeamTask] = field(default_factory=dict)
    messages: list[TeamMessage] = field(default_factory=list)
    summary: str = ""
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)


class TeamManager:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or Path.cwd() / ".forgecc" / "teams"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._teams: dict[str, Team] = {}
        self._events: dict[tuple[str, str], asyncio.Event] = {}
        self._loops: dict[tuple[str, str], asyncio.Task] = {}
        self._load()

    def create_team(
        self,
        *,
        name: str,
        agents: list[dict],
        initial_tasks: list[dict] | None = None,
    ) -> Team:
        team_id = _slug(name)
        if team_id in self._teams:
            team_id = f"{team_id}-{uuid.uuid4().hex[:4]}"
        team = Team(id=team_id, name=name)
        for raw in agents:
            agent_name = _slug(str(raw.get("name") or raw.get("role") or "agent"))
            team.agents[agent_name] = TeamAgent(
                name=agent_name,
                role=str(raw.get("role") or agent_name),
                agent_type=str(raw.get("type") or raw.get("agent_type") or "general"),
                worktree=bool(raw.get("worktree", str(raw.get("type") or raw.get("agent_type") or "general") == "general")),
            )
        if not team.agents:
            team.agents["researcher"] = TeamAgent(name="researcher", role="Researcher", agent_type="explore")
        self._teams[team.id] = team
        for task in initial_tasks or []:
            self.add_task(
                team.id,
                title=str(task.get("title") or "Initial task"),
                description=str(task.get("description") or task.get("prompt") or ""),
            )
        self._save(team.id)
        return team

    def get(self, team_id: str) -> Team | None:
        return self._teams.get(team_id)

    def list(self) -> list[Team]:
        return sorted(self._teams.values(), key=lambda team: team.created_at)

    def add_task(self, team_id: str, *, title: str, description: str) -> TeamTask | None:
        team = self.get(team_id)
        if not team:
            return None
        task = TeamTask(id=uuid.uuid4().hex[:8], title=title, description=description)
        team.tasks[task.id] = task
        team.updated_at = _now()
        self._save(team_id)
        return task

    def send_message(
        self,
        team_id: str,
        *,
        sender: str,
        recipient: str,
        content: str,
        kind: str = "STATUS",
        thread_id: str | None = None,
    ) -> TeamMessage | None:
        team = self.get(team_id)
        if not team:
            return None
        kind = kind.upper()
        if kind not in MESSAGE_KINDS:
            raise ValueError(f"Invalid team message kind: {kind}")
        if recipient != "all" and recipient not in team.agents and recipient != "coordinator":
            raise ValueError(f"Unknown team message recipient: {recipient}")
        if kind == "REPLY" and not thread_id:
            raise ValueError("REPLY messages require thread_id.")
        msg = TeamMessage(
            id=uuid.uuid4().hex[:8],
            sender=sender,
            recipient=recipient,
            content=content,
            kind=kind,
            thread_id=thread_id,
        )
        team.messages.append(msg)
        team.updated_at = _now()
        self._save(team_id)
        self.wake_recipient(team_id, recipient)
        return msg

    def read_messages(self, team_id: str, agent_name: str, *, unread_only: bool = True) -> list[TeamMessage]:
        team = self.get(team_id)
        if not team:
            return []
        visible = [
            msg for msg in team.messages
            if msg.recipient in (agent_name, "all") and (not unread_only or agent_name not in msg.read_by)
        ]
        for msg in visible:
            if agent_name not in msg.read_by:
                msg.read_by.append(agent_name)
        if visible:
            self._save(team_id)
        return visible

    def format_message(self, msg: TeamMessage) -> str:
        thread = f" thread={msg.thread_id}" if msg.thread_id else ""
        return f"[{msg.id}] {msg.kind}{thread} from {msg.sender} to {msg.recipient}: {msg.content}"

    def claim_task(self, team_id: str, agent_name: str, task_id: str | None = None) -> TeamTask | None:
        team = self.get(team_id)
        if not team:
            return None
        candidates = [team.tasks[task_id]] if task_id and task_id in team.tasks else list(team.tasks.values())
        for task in candidates:
            if task.status == "open":
                task.status = "claimed"
                task.assignee = agent_name
                task.updated_at = _now()
                task.history.append(f"{agent_name} claimed task")
                self._save(team_id)
                return task
        return None

    def update_task(
        self,
        team_id: str,
        agent_name: str,
        *,
        task_id: str,
        status: str,
        note: str = "",
        result: str = "",
    ) -> TeamTask | None:
        team = self.get(team_id)
        task = team.tasks.get(task_id) if team else None
        if not task:
            return None
        task.status = status
        task.assignee = task.assignee or agent_name
        task.result = result or task.result
        task.updated_at = _now()
        if note:
            task.history.append(f"{agent_name}: {note}")
        self._save(team_id)
        return task

    def mark_agent(self, team_id: str, agent_name: str, status: str, error: str | None = None) -> None:
        team = self.get(team_id)
        if not team or agent_name not in team.agents:
            return
        agent = team.agents[agent_name]
        agent.status = status
        agent.last_error = error
        if status == "running":
            agent.wake_count += 1
        team.updated_at = _now()
        self._save(team_id)

    def update_agent_tokens(self, team_id: str, agent_name: str, tokens: dict[str, int]) -> None:
        team = self.get(team_id)
        if not team or agent_name not in team.agents:
            return
        team.agents[agent_name].tokens = tokens
        self._save(team_id)

    def format_status(self, team_id: str | None = None) -> str:
        teams = [self._teams[team_id]] if team_id and team_id in self._teams else self.list()
        if team_id and not teams:
            return f"Unknown team: {team_id}"
        if not teams:
            return "No teams."
        return "\n\n".join(self._format_team(team) for team in teams)

    def format_board(self, team_id: str) -> str:
        team = self.get(team_id)
        if not team:
            return f"Unknown team: {team_id}"
        lines = [f"Team {team.id}: {team.name}"]
        if team.summary:
            lines.append(f"Summary: {team.summary}")
        lines.append("\nAgents:")
        for agent in team.agents.values():
            wt = f", worktree={agent.branch} at {agent.worktree_path}" if agent.worktree_path else ""
            lines.append(f"- {agent.name} ({agent.role}, {agent.agent_type}{wt}): {agent.status}")
        lines.append("\nTasks:")
        for task in team.tasks.values():
            assignee = f" -> {task.assignee}" if task.assignee else ""
            lines.append(f"- [{task.id}] {task.status}{assignee}: {task.title}\n  {task.description}")
            if task.result:
                lines.append(f"  result: {task.result[:500]}")
        return "\n".join(lines)

    def compact(self, team_id: str) -> str:
        team = self.get(team_id)
        if not team:
            return f"Unknown team: {team_id}"
        completed = [task for task in team.tasks.values() if task.status in {"done", "blocked", "cancelled"}]
        if completed:
            parts = []
            for task in completed[-20:]:
                result = f" result={task.result}" if task.result else ""
                parts.append(f"{task.id}:{task.status}:{task.title}{result}")
            team.summary = "Recent completed work: " + " | ".join(parts)
        if len(team.messages) > 50:
            team.messages = team.messages[-50:]
        self._save(team_id)
        return f"Compacted team {team_id}."

    def register_loop(self, team_id: str, agent_name: str, runner: AgentLoop) -> bool:
        key = (team_id, agent_name)
        existing = self._loops.get(key)
        if existing and not existing.done():
            return False
        self._events.setdefault(key, asyncio.Event()).set()
        self._loops[key] = asyncio.create_task(runner(), name=f"forgecc-team-{team_id}-{agent_name}")
        return True

    def wake_agent(self, team_id: str, agent_name: str) -> bool:
        key = (team_id, agent_name)
        event = self._events.get(key)
        if not event:
            return False
        event.set()
        return True

    def wake_recipient(self, team_id: str, recipient: str) -> None:
        team = self.get(team_id)
        if not team:
            return
        names = team.agents.keys() if recipient == "all" else [recipient]
        for name in names:
            self.wake_agent(team_id, name)

    async def wait_for_wake(self, team_id: str, agent_name: str) -> bool:
        key = (team_id, agent_name)
        event = self._events.setdefault(key, asyncio.Event())
        await event.wait()
        event.clear()
        return True

    def request_idle(self, team_id: str, agent_name: str) -> None:
        key = (team_id, agent_name)
        event = self._events.setdefault(key, asyncio.Event())
        event.clear()

    def stop_team(self, team_id: str) -> str:
        stopped = 0
        for key, task in list(self._loops.items()):
            if key[0] == team_id and not task.done():
                task.cancel()
                stopped += 1
        team = self.get(team_id)
        if team:
            for agent in team.agents.values():
                agent.status = "stopped"
            self._save(team_id)
        return f"Stopped {stopped} live agent loops for team {team_id}."

    def _format_team(self, team: Team) -> str:
        lines = [f"Team {team.id}: {team.name}"]
        agent_bits = [
            f"{a.name}={a.status}/{a.agent_type}" + ("/worktree" if a.worktree_path else "")
            for a in team.agents.values()
        ]
        lines.append("Agents: " + (", ".join(agent_bits) or "(none)"))
        task_counts: dict[str, int] = {}
        for task in team.tasks.values():
            task_counts[task.status] = task_counts.get(task.status, 0) + 1
        lines.append("Tasks: " + (", ".join(f"{k}={v}" for k, v in sorted(task_counts.items())) or "(none)"))
        unread = sum(1 for msg in team.messages if not msg.read_by)
        kind_counts: dict[str, int] = {}
        for msg in team.messages:
            kind_counts[msg.kind] = kind_counts.get(msg.kind, 0) + 1
        kinds = ", ".join(f"{kind}={count}" for kind, count in sorted(kind_counts.items()))
        lines.append(f"Messages: {len(team.messages)} total, {unread} unread" + (f" ({kinds})" if kinds else ""))
        return "\n".join(lines)

    def _team_path(self, team_id: str) -> Path:
        return self.base_dir / f"{team_id}.json"

    def _save(self, team_id: str) -> None:
        team = self.get(team_id)
        if not team:
            return
        self._team_path(team_id).write_text(json.dumps(asdict(team), indent=2), encoding="utf-8")

    def _load(self) -> None:
        for path in sorted(self.base_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                team = Team(
                    id=raw["id"],
                    name=raw["name"],
                    agents={name: TeamAgent(**agent) for name, agent in raw.get("agents", {}).items()},
                    tasks={task_id: TeamTask(**task) for task_id, task in raw.get("tasks", {}).items()},
                    messages=[TeamMessage(**msg) for msg in raw.get("messages", [])],
                    summary=raw.get("summary", ""),
                    created_at=raw.get("created_at", _now()),
                    updated_at=raw.get("updated_at", _now()),
                )
                self._teams[team.id] = team
            except Exception:
                pass
