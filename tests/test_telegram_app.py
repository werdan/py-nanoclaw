from __future__ import annotations

from nanoclaw.scheduler import ScheduledTask
from nanoclaw.telegram_app import (
    _newly_created_tasks,
    _scheduled_task_confirmation,
)


def test_scheduled_task_confirmation_contains_key_fields() -> None:
    text = _scheduled_task_confirmation("abcd1234", "35 21 29 3 *", "2026-03-29T21:35:00+00:00")
    assert "Scheduled task accepted." in text
    assert "Task ID:" in text
    assert "Next run:" in text
    assert "abcd1234" in text
    assert "35 21 29 3 *" in text
    assert "2026-03-29T21:35:00+00:00" in text


def test_newly_created_tasks_detects_added_ids() -> None:
    before = [
        ScheduledTask(
            id="a1",
            prompt="old",
            cron="0 9 * * *",
            next_run="2026-03-30T09:00:00+00:00",
            paused=False,
        )
    ]
    after = before + [
        ScheduledTask(
            id="b2",
            prompt="new",
            cron="10 9 * * *",
            next_run="2026-03-30T09:10:00+00:00",
            paused=False,
        )
    ]
    created = _newly_created_tasks(before, after)
    assert [task.id for task in created] == ["b2"]
