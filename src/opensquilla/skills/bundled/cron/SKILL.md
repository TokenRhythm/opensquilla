---
name: cron
description: "Use when the user asks to schedule recurring tasks, one-off reminders, timers, or cron-style jobs through the OpenSquilla cron tool."
description_zh: "当用户需要通过OpenSquilla cron工具安排周期性任务、一次性提醒、计时器或cron风格的作业时使用。"
always: false
triggers:
  - schedule
  - recurring
  - timer
  - cron
  - every
  - reminder
  - remind
  - 提醒
  - 每分钟
  - 每5分钟
  - 每天
  - 定时
provenance:
  origin: openclaw-derived
  license: MIT
  upstream_url: https://github.com/openclaw/openclaw
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    requires_tools:
      - cron
---

# Cron Skill

When the user asks to schedule something, set up a recurring task, create a timer, or create a reminder, use the `cron` tool.

The `schedule` argument is a **structured object**, not a string. Choose one shape and translate any natural language yourself before calling the tool — the tool will not parse free-form text and will reject flat strings with a structured error.

Three accepted schedule shapes:

- `{"kind": "cron", "expr": "<5-field POSIX cron>", "tz": "<optional IANA timezone>"}`
  Recurring on a calendar pattern. Example: `{"kind": "cron", "expr": "0 9 * * 1-5", "tz": "Asia/Shanghai"}` for weekdays at 09:00 Shanghai wall time.
- `{"kind": "every", "every_seconds": <integer ≥ 1>}`
  Recurring on a fixed sub-minute or odd interval. Example: `{"kind": "every", "every_seconds": 30}` for every 30 seconds.
- `{"kind": "at", "at": "<ISO-8601 with timezone>"}`
  One-shot at an absolute time. The timestamp must include a timezone offset.

Translation examples (do this in your own reasoning before calling the tool):

- "每5分钟提醒我喝水" → `cron(action="add", schedule={"kind": "cron", "expr": "*/5 * * * *"}, task="喝水", job_kind="system_event", session_target="main")`
- "每30秒打印一次" → `cron(action="add", schedule={"kind": "every", "every_seconds": 30}, task="...", job_kind="agent_turn", session_target="isolated")`
- "明天早上9点叫我" → compute the absolute ISO-8601 string with timezone, then `cron(action="add", schedule={"kind": "at", "at": "<that ISO-8601>"}, task="...", job_kind="system_event", session_target="main")`
- "every weekday at 9am Los Angeles time" → `cron(action="add", schedule={"kind": "cron", "expr": "0 9 * * 1-5", "tz": "America/Los_Angeles"}, task="...")`

Other actions:

- List: `cron(action="list")`.
- Trigger now: `cron(action="run", job_id="<job id>")`.
- Cancel: `cron(action="remove", job_id="<job id>")`.

Cron expression format: `minute hour day month weekday` (e.g. `0 9 * * 1-5` = weekdays at 9am).
