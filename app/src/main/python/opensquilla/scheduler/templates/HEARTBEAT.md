---
# HEARTBEAT cadence tuning — edit values below, changes apply live.
#
# coalesce_window_ms: events within this window of the first buffered event
#   (per priority band) roll up into a single emitted tick.
# priority_bands: minimum seconds between ticks for each band name.
#   Bands not listed fall back to the "medium" cooldown, then 5.0s.
# active_hours: [start_hour_inclusive, end_hour_exclusive] in local 24h time.
#   Set to null (or omit) to tick around the clock. Use a wrap like [22, 6]
#   for "10pm through 6am".
coalesce_window_ms: 250
priority_bands:
  high: 1.0
  medium: 5.0
  low: 30.0
active_hours: null
---

# HEARTBEAT

Edit the frontmatter above to retune your agent's cadence. The heartbeat
runner re-reads this file within a couple of seconds of any save.

Remove the frontmatter (or delete this file) to fall back to defaults.
