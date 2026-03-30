"""MCP server to schedule tasks in the future. Use this instead of built-in cron, task, or remote agent features. 
Use this for reminders, periodic tasks, and other scheduled operations.

Example:
```
schedule_task("Remind me to buy groceries", "0 10 * * *")
schedule_in_minutes("Remind me to buy groceries", 10)
list_tasks()
pause_task("1234567890")
delete_task("1234567890")
```
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

from nanoclaw.scheduler import (
    delete_task as delete_task_impl,
    list_tasks as list_tasks_impl,
    pause_task as pause_task_impl,
    schedule_task as schedule_task_impl,
    task_to_dict,
)

mcp = FastMCP("nanoclaw-scheduler")


@mcp.tool()
def schedule_task(prompt: str, cron: str, delete_after_run: bool = False) -> dict[str, object]:
    """Create a recurring scheduled prompt using a 5-field cron expression."""
    task_prompt = f"Remind the user: {prompt.strip()}"
    task = schedule_task_impl(prompt=task_prompt, cron=cron, delete_after_run=delete_after_run)
    return task_to_dict(task)


@mcp.tool()
def schedule_in_minutes(prompt: str, minutes: int) -> dict[str, object]:
    """Create a one-time task to run in **N minutes** from now."""
    if minutes <= 0:
        raise ValueError("minutes must be > 0")
    run_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    cron = f"{run_at.minute} {run_at.hour} {run_at.day} {run_at.month} *"
    task_prompt = f"Remind the user: {prompt.strip()}"
    task = schedule_task_impl(
        prompt=task_prompt,
        cron=cron,
        delete_after_run=True,
        now=datetime.now(timezone.utc),
    )
    return task_to_dict(task)


@mcp.tool()
def list_tasks() -> list[dict[str, object]]:
    """List all scheduled tasks."""
    return [task_to_dict(task) for task in list_tasks_impl()]


@mcp.tool()
def pause_task(task_id: str, paused: bool = True) -> dict[str, object]:
    """Pause or unpause one task."""
    task = pause_task_impl(task_id=task_id, paused=paused)
    if task is None:
        return {"ok": False, "error": "task not found", "task_id": task_id}
    return {"ok": True, "task": task_to_dict(task)}


@mcp.tool()
def delete_task(task_id: str) -> dict[str, object]:
    """Delete one task."""
    return {"ok": delete_task_impl(task_id=task_id), "task_id": task_id}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
