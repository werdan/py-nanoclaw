from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanoclaw.scheduler import (
    ScheduledTask,
    delete_task,
    get_due_tasks_and_advance,
    list_tasks,
    pause_task,
    run_scheduler_loop,
    save_tasks,
    schedule_task,
)


def test_schedule_task_persists_and_lists(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    now = datetime(2026, 3, 29, 8, 0, tzinfo=timezone.utc)

    created = schedule_task("check email", "0 9 * * *", path=path, now=now)
    tasks = list_tasks(path=path)

    assert len(tasks) == 1
    assert tasks[0].id == created.id
    assert tasks[0].prompt == "check email"
    assert tasks[0].cron == "0 9 * * *"
    assert tasks[0].delete_after_run is False
    assert datetime.fromisoformat(tasks[0].next_run) > now


def test_pause_and_delete_task(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    now = datetime(2026, 3, 29, 8, 0, tzinfo=timezone.utc)
    created = schedule_task("check email", "0 9 * * *", path=path, now=now)

    paused = pause_task(created.id, paused=True, path=path)
    assert paused is not None
    assert paused.paused is True

    assert delete_task(created.id, path=path) is True
    assert list_tasks(path=path) == []


def test_get_due_tasks_advances_next_run(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    now = datetime(2026, 3, 29, 9, 0, tzinfo=timezone.utc)
    due_task = ScheduledTask(
        id="abc12345",
        prompt="check email",
        cron="*/5 * * * *",
        next_run=(now - timedelta(minutes=1)).isoformat(),
        paused=False,
    )
    save_tasks([due_task], path=path)

    due = get_due_tasks_and_advance(path=path, now=now)
    stored = list_tasks(path=path)

    assert [t.id for t in due] == ["abc12345"]
    assert len(stored) == 1
    assert datetime.fromisoformat(stored[0].next_run) > now


@pytest.mark.asyncio
async def test_run_scheduler_loop_enqueues_due_prompt(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    now = datetime.now(timezone.utc)
    save_tasks(
        [
            ScheduledTask(
                id="due12345",
                prompt="check email",
                cron="* * * * *",
                next_run=(now - timedelta(minutes=1)).isoformat(),
                paused=False,
            )
        ],
        path=path,
    )
    inbound: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_scheduler_loop(inbound, poll_interval_s=0.05, stop=stop, path=path)
    )
    item = await asyncio.wait_for(inbound.get(), timeout=1.0)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert "Execute scheduled task now." in item.content
    assert "Task ID: due12345" in item.content
    assert "Reminder task: check email" in item.content


@pytest.mark.asyncio
async def test_run_scheduler_loop_does_not_enqueue_paused_task(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    now = datetime.now(timezone.utc)
    save_tasks(
        [
            ScheduledTask(
                id="paused123",
                prompt="do not send",
                cron="* * * * *",
                next_run=(now - timedelta(minutes=1)).isoformat(),
                paused=True,
            )
        ],
        path=path,
    )
    inbound: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_scheduler_loop(inbound, poll_interval_s=0.05, stop=stop, path=path)
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(inbound.get(), timeout=0.2)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)


def test_list_tasks_ignores_malformed_entries(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    payload = [
        {
            "id": "ok123456",
            "prompt": "check email",
            "cron": "0 9 * * *",
            "next_run": "2026-03-30T09:00:00+00:00",
            "paused": False,
        },
        {"id": "missing-fields"},
        "not-an-object",
        {"id": "", "prompt": "x", "cron": "* * * * *", "next_run": "2026-03-30T09:00:00+00:00"},
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")

    tasks = list_tasks(path=path)

    assert len(tasks) == 1
    assert tasks[0].id == "ok123456"


def test_one_shot_task_is_deleted_after_due(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    now = datetime(2026, 3, 29, 9, 0, tzinfo=timezone.utc)
    one_shot = ScheduledTask(
        id="oneshot1",
        prompt="remind now",
        cron="* * * * *",
        next_run=(now - timedelta(minutes=1)).isoformat(),
        paused=False,
        delete_after_run=True,
    )
    save_tasks([one_shot], path=path)

    due = get_due_tasks_and_advance(path=path, now=now)
    stored = list_tasks(path=path)

    assert [t.id for t in due] == ["oneshot1"]
    assert stored == []
