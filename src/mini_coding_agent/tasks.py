"""In-process background task runtime for coordinator-style multi-agent work."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


TaskRunner = Callable[[], Awaitable[dict[str, Any]]]


@dataclass
class TaskRecord:
    id: str
    description: str
    agent_type: str
    prompt: str
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: str | None = None
    error: str | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    token_accounted: bool = False
    task: asyncio.Task | None = field(default=None, repr=False)


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}

    def spawn(
        self,
        *,
        description: str,
        agent_type: str,
        prompt: str,
        runner: TaskRunner,
    ) -> TaskRecord:
        task_id = uuid.uuid4().hex[:8]
        record = TaskRecord(
            id=task_id,
            description=description,
            agent_type=agent_type,
            prompt=prompt,
        )

        async def _run() -> None:
            try:
                result = await runner()
                record.result = result.get("text") or "(Sub-agent produced no output)"
                record.tokens = {
                    "input": int(result.get("tokens", {}).get("input", 0)),
                    "output": int(result.get("tokens", {}).get("output", 0)),
                    "cached_input": int(result.get("tokens", {}).get("cached_input", 0)),
                }
                record.status = "completed"
            except asyncio.CancelledError:
                record.status = "cancelled"
                record.error = "Task cancelled."
            except Exception as exc:
                record.status = "failed"
                record.error = str(exc)
            finally:
                record.updated_at = time.time()

        record.task = asyncio.create_task(_run(), name=f"forgecc-agent-{task_id}")
        self._tasks[task_id] = record
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def list(self) -> list[TaskRecord]:
        return sorted(self._tasks.values(), key=lambda task: task.created_at)

    def stop(self, task_id: str) -> bool:
        record = self.get(task_id)
        if not record or not record.task or record.task.done():
            return False
        record.task.cancel()
        record.status = "cancelling"
        record.updated_at = time.time()
        return True

    def format_status(self, task_id: str | None = None) -> str:
        if task_id:
            record = self.get(task_id)
            if not record:
                return f"Unknown task: {task_id}"
            records = [record]
        else:
            records = self.list()
        if not records:
            return "No background tasks."
        return "\n".join(self._format_record_status(record) for record in records)

    def format_output(self, task_id: str) -> str:
        record = self.get(task_id)
        if not record:
            return f"Unknown task: {task_id}"
        if record.status in {"running", "cancelling"}:
            return self._format_record_status(record)
        if record.error:
            return f"{self._format_record_status(record)}\n\nError: {record.error}"
        return f"{self._format_record_status(record)}\n\n{record.result or '(No output)'}"

    def unaccounted_completed(self) -> list[TaskRecord]:
        return [
            record for record in self._tasks.values()
            if not record.token_accounted and record.status in {"completed", "failed", "cancelled"}
        ]

    @staticmethod
    def _format_record_status(record: TaskRecord) -> str:
        age = int(time.time() - record.created_at)
        return (
            f"[{record.id}] {record.status} "
            f"{record.agent_type}: {record.description} ({age}s)"
        )
