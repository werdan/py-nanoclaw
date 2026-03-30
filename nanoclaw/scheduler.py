"""Cron task storage and scheduler loop for queueing scheduled prompts."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from croniter import croniter

from nanoclaw.models import Inbound

logger = logging.getLogger(__name__)

_DEFAULT_TASKS_FILE = ".nanoclaw_tasks.json"
_TASKS_PATH_ENV = "NANOCLAW_TASKS_PATH"


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    id: str
    prompt: str
    cron: str
    next_run: str
    paused: bool = False
    delete_after_run: bool = False


def _tasks_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    raw = ""
    try:
        import os

        raw = os.environ.get(_TASKS_PATH_ENV, "")
    except Exception:
        raw = ""
    if raw.strip():
        return Path(raw).expanduser()
    return Path.cwd() / _DEFAULT_TASKS_FILE


def _task_from_dict(data: dict[str, object]) -> ScheduledTask | None:
    task_id = data.get("id")
    prompt = data.get("prompt")
    cron = data.get("cron")
    next_run = data.get("next_run")
    paused = data.get("paused", False)
    delete_after_run = data.get("delete_after_run", False)
    if not isinstance(task_id, str) or not task_id:
        return None
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    if not isinstance(cron, str) or not cron.strip():
        return None
    if not isinstance(next_run, str) or not next_run.strip():
        return None
    if not isinstance(paused, bool):
        paused = False
    if not isinstance(delete_after_run, bool):
        delete_after_run = False
    return ScheduledTask(
        id=task_id,
        prompt=prompt.strip(),
        cron=cron.strip(),
        next_run=next_run.strip(),
        paused=paused,
        delete_after_run=delete_after_run,
    )


def task_to_dict(task: ScheduledTask) -> dict[str, object]:
    return {
        "id": task.id,
        "prompt": task.prompt,
        "cron": task.cron,
        "next_run": task.next_run,
        "paused": task.paused,
        "delete_after_run": task.delete_after_run,
    }


def load_tasks(path: Path | None = None) -> list[ScheduledTask]:
    p = _tasks_path(path)
    if not p.exists():
        return []
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("Tasks file must contain a JSON array.")
    out: list[ScheduledTask] = []
    for item in data:
        if isinstance(item, dict):
            task = _task_from_dict(item)
            if task is not None:
                out.append(task)
    return out


def save_tasks(tasks: list[ScheduledTask], path: Path | None = None) -> None:
    p = _tasks_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [task_to_dict(t) for t in tasks]
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _next_run_iso(cron: str, *, now: datetime) -> str:
    next_dt = croniter(cron, now).get_next(datetime)
    return next_dt.isoformat()


def schedule_task(
    prompt: str,
    cron: str,
    *,
    delete_after_run: bool = False,
    path: Path | None = None,
    now: datetime | None = None,
) -> ScheduledTask:
    if not prompt.strip():
        raise ValueError("prompt must be non-empty")
    if not cron.strip():
        raise ValueError("cron must be non-empty")
    base = now or datetime.now().astimezone()
    task = ScheduledTask(
        id=uuid.uuid4().hex[:8],
        prompt=prompt.strip(),
        cron=cron.strip(),
        next_run=_next_run_iso(cron.strip(), now=base),
        paused=False,
        delete_after_run=delete_after_run,
    )
    tasks = load_tasks(path)
    tasks.append(task)
    save_tasks(tasks, path)
    return task


def list_tasks(*, path: Path | None = None) -> list[ScheduledTask]:
    return load_tasks(path)


def pause_task(task_id: str, *, paused: bool = True, path: Path | None = None) -> ScheduledTask | None:
    tasks = load_tasks(path)
    updated: ScheduledTask | None = None
    out: list[ScheduledTask] = []
    for task in tasks:
        if task.id == task_id:
            updated = ScheduledTask(
                id=task.id,
                prompt=task.prompt,
                cron=task.cron,
                next_run=task.next_run,
                paused=paused,
                delete_after_run=task.delete_after_run,
            )
            out.append(updated)
        else:
            out.append(task)
    if updated is not None:
        save_tasks(out, path)
    return updated


def delete_task(task_id: str, *, path: Path | None = None) -> bool:
    tasks = load_tasks(path)
    kept = [task for task in tasks if task.id != task_id]
    if len(kept) == len(tasks):
        return False
    save_tasks(kept, path)
    return True


def get_due_tasks_and_advance(*, path: Path | None = None, now: datetime | None = None) -> list[ScheduledTask]:
    current = now or datetime.now().astimezone()
    tasks = load_tasks(path)
    due: list[ScheduledTask] = []
    updated: list[ScheduledTask] = []
    changed = False
    for task in tasks:
        if task.paused:
            updated.append(task)
            continue
        try:
            next_run_dt = datetime.fromisoformat(task.next_run)
        except ValueError:
            logger.warning("Invalid next_run for task %s; recomputing.", task.id)
            next_run_dt = current
        if next_run_dt <= current:
            due.append(task)
            changed = True
            if task.delete_after_run:
                # One-shot task: fire once and remove from storage.
                continue
            updated.append(
                ScheduledTask(
                    id=task.id,
                    prompt=task.prompt,
                    cron=task.cron,
                    next_run=_next_run_iso(task.cron, now=current),
                    paused=task.paused,
                    delete_after_run=task.delete_after_run,
                )
            )
        else:
            updated.append(task)
    if changed:
        save_tasks(updated, path)
    return due


def _scheduled_inbound(task: ScheduledTask) -> Inbound:
    content = (
        "Execute scheduled task now.\n"
        f"Task ID: {task.id}\n"
        "Instruction: send a reminder message to the user in this chat.\n"
        f"Reminder task: {task.prompt}"
    )
    return Inbound(content)


async def run_scheduler_loop(
    inbound_queue: asyncio.Queue[Inbound],
    *,
    poll_interval_s: float = 60.0,
    stop: asyncio.Event | None = None,
    path: Path | None = None,
) -> None:
    while stop is None or not stop.is_set():
        due = get_due_tasks_and_advance(path=path)
        for task in due:
            await inbound_queue.put(_scheduled_inbound(task))
        if stop is not None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval_s)
                return
            except TimeoutError:
                continue
        await asyncio.sleep(poll_interval_s)
