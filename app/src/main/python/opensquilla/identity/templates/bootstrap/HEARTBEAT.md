---
# HEARTBEAT cadence tuning - edit values below, changes apply live.
coalesce_window_ms: 250
priority_bands:
  high: 1.0
  medium: 5.0
  low: 30.0
active_hours: null
---

# HEARTBEAT

Use this file for periodic heartbeat tasks and cadence configuration. Keep it
short: heartbeat runs may inject this file as live operating context.

Do not store user profile facts, assistant persona, durable memory, or ordinary
task history here.
