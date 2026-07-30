---
name: weather
description: "Get current weather and forecasts via wttr.in or Open-Meteo. Use when: user asks about weather, temperature, or forecasts for any location. NOT for: historical weather data, severe weather alerts, or detailed meteorological analysis. No API key needed."
description_zh: "通过wttr.in或Open-Meteo获取当前天气和预报。当用户询问任意地点的天气、气温或预报时使用。不适用于历史天气数据、恶劣天气预警或详细气象分析。无需API密钥。"
homepage: https://wttr.in/:help
provenance:
  origin: openclaw-derived
  license: MIT
  upstream_url: https://github.com/openclaw/openclaw
  maintained_by: OpenSquilla
metadata:
  {
    "openclaw":
      {
        "emoji": "☔",
        "requires": { "bins": ["curl"] },
        "install":
          [
            {
              "id": "brew",
              "kind": "brew",
              "os": ["darwin"],
              "formula": "curl",
              "bins": ["curl"],
              "label": "Install curl (brew)",
            },
          ],
      },
  }
entrypoint:
  command: python {baseDir}/scripts/weather_fetch.py
  args:
    - --location
    - "{{ with.location | default(inputs.user_message) }}"
    - --days
    - "{{ with.days | default(3) }}"
    - --timeout
    - "{{ with.timeout | default(8) }}"
    - --max-chars
    - "{{ with.max_chars | default(2500) }}"
  parse: json
  timeout: 15
---

# Weather Skill

Get current weather conditions and forecasts.

## Meta-Skill Entrypoint

Meta-skills can run this skill as `skill_exec` for a bounded JSON forecast
without spawning an LLM sub-agent. The entrypoint extracts `DESTINATION:` from
planner contracts, queries wttr.in, and returns compact `current`, `forecast`,
`seasonal_hint`, and `errors` fields. Network failures are reported in
`errors` while preserving a usable seasonal hint.

## Location

Always include a city, region, or airport code in weather queries.

## Commands

### Current Weather

```bash
# One-line summary
curl "https://wttr.in/London?format=3"

# Detailed current conditions
curl "https://wttr.in/London?0"

# Specific city
curl "https://wttr.in/New+York?format=3"
```

### Forecasts

```bash
# 3-day forecast
curl "https://wttr.in/London"

# Week forecast
curl "https://wttr.in/London?format=v2"

# Specific day (0=today, 1=tomorrow, 2=day after)
curl "https://wttr.in/London?1"
```

### Format Options

```bash
# One-liner
curl "https://wttr.in/London?format=%l:+%c+%t+%w"

# JSON output
curl "https://wttr.in/London?format=j1"

# PNG image
curl "https://wttr.in/London.png"
```

### Format Codes

- `%c` — Weather condition emoji
- `%t` — Temperature
- `%f` — "Feels like"
- `%w` — Wind
- `%h` — Humidity
- `%p` — Precipitation
- `%l` — Location

## Quick Responses

**"What's the weather?"**

```bash
curl -s "https://wttr.in/London?format=%l:+%c+%t+(feels+like+%f),+%w+wind,+%h+humidity"
```

**"Will it rain?"**

```bash
curl -s "https://wttr.in/London?format=%l:+%c+%p"
```

**"Weekend forecast"**

```bash
curl "https://wttr.in/London?format=v2"
```

## Notes

- No API key needed (uses wttr.in)
- Rate limited; don't spam requests
- Works for most global cities
- Supports airport codes: `curl https://wttr.in/ORD`
