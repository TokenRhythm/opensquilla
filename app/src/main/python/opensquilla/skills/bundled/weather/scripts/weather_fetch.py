#!/usr/bin/env python3
"""Fetch a compact weather summary for meta-skill DAGs."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _extract_location(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "London"
    for line in text.splitlines():
        match = re.match(r"\s*DESTINATION:\s*(.+?)\s*$", line, flags=re.I)
        if match:
            return match.group(1).strip()
    first = text.splitlines()[0].strip()
    return first[:120] or "London"


def _seasonal_hint(query: str, location: str) -> str:
    lowered = f"{query} {location}".lower()
    if "tokyo" in lowered and ("june" in lowered or "late june" in lowered):
        return (
            "Tokyo in late June is usually tsuyu rainy season: humid, warm, "
            "frequent showers, and occasional heavy rain. Treat outdoor plans "
            "as weather-dependent and keep indoor backups."
        )
    if "june" in lowered:
        return (
            "Requested dates appear outside the reliable short forecast window; "
            "use current forecast only as near-term context and verify seasonal "
            "normals before booking."
        )
    return (
        "Short-range forecast only; verify dates again near departure for "
        "weather-sensitive bookings."
    )


def _fetch_wttr_json(location: str, timeout: float) -> dict[str, Any]:
    encoded = urllib.parse.quote(location)
    url = f"https://wttr.in/{encoded}?format=j1"
    req = urllib.request.Request(  # noqa: S310 - fixed trusted weather endpoint
        url,
        headers={"User-Agent": "OpenSquilla-weather-skill/0.1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _pick_current(payload: dict[str, Any]) -> dict[str, str]:
    current = (payload.get("current_condition") or [{}])[0]
    desc = (current.get("weatherDesc") or [{}])[0].get("value", "")
    return {
        "condition": desc,
        "temperature_c": str(current.get("temp_C", "")),
        "feels_like_c": str(current.get("FeelsLikeC", "")),
        "humidity_pct": str(current.get("humidity", "")),
        "precip_mm": str(current.get("precipMM", "")),
        "wind_kmph": str(current.get("windspeedKmph", "")),
    }


def _pick_forecast(payload: dict[str, Any], days: int) -> list[dict[str, str]]:
    forecast: list[dict[str, str]] = []
    for item in (payload.get("weather") or [])[:days]:
        hourly = item.get("hourly") or []
        rain_chances = [
            int(h.get("chanceofrain", 0))
            for h in hourly
            if str(h.get("chanceofrain", "")).isdigit()
        ]
        forecast.append(
            {
                "date": str(item.get("date", "")),
                "min_c": str(item.get("mintempC", "")),
                "max_c": str(item.get("maxtempC", "")),
                "rain_chance_max_pct": str(max(rain_chances) if rain_chances else ""),
                "rain_hours_over_50pct": str(sum(1 for value in rain_chances if value >= 50)),
            }
        )
    return forecast


def _summarize(result: dict[str, Any], max_chars: int) -> dict[str, Any]:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= max_chars:
        return result
    result = dict(result)
    result["truncated"] = True
    result["forecast"] = result.get("forecast", [])[:2]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--location", required=True)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--max-chars", type=int, default=2500)
    args = parser.parse_args(argv)

    raw_location = args.location
    location = _extract_location(raw_location)
    days = max(1, min(args.days, 5))
    result: dict[str, Any] = {
        "location": location,
        "source": "wttr.in",
        "forecast_window": "short_range_current_service",
        "seasonal_hint": _seasonal_hint(raw_location, location),
        "current": {},
        "forecast": [],
        "errors": [],
    }
    try:
        payload = _fetch_wttr_json(location, args.timeout)
        result["current"] = _pick_current(payload)
        result["forecast"] = _pick_forecast(payload, days)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - keep meta DAG resilient
        result["errors"].append(f"{type(exc).__name__}: {exc}")

    sys.stdout.write(json.dumps(_summarize(result, args.max_chars), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
