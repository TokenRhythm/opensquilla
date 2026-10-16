#!/usr/bin/env python3
"""Compare saved Actions run/jobs snapshots without conflating failures with speedups."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def analyze(run: dict[str, Any], jobs: list[dict[str, Any]]) -> dict[str, Any]:
    origin = timestamp(run["created_at"])
    executed = [job for job in jobs if job.get("started_at") and job.get("conclusion") != "skipped"]
    complete = [job for job in executed if job.get("completed_at")]
    end = max((timestamp(job["completed_at"]) for job in complete), default=origin)
    events: list[tuple[datetime, int]] = []
    rows = []
    for job in complete:
        start = timestamp(job["started_at"])
        finish = timestamp(job["completed_at"])
        events.extend(((start, 1), (finish, -1)))
        steps = [
            {
                "name": step["name"],
                "conclusion": step.get("conclusion"),
                "seconds": (
                    timestamp(step["completed_at"]) - timestamp(step["started_at"])
                ).total_seconds(),
            }
            for step in job.get("steps", [])
            if step.get("started_at") and step.get("completed_at")
        ]
        rows.append(
            {
                "name": job["name"],
                "id": job["id"],
                "url": job.get("html_url", ""),
                "conclusion": job.get("conclusion"),
                "start_seconds": (start - origin).total_seconds(),
                "finish_seconds": (finish - origin).total_seconds(),
                "seconds": (finish - start).total_seconds(),
                "steps": steps,
            }
        )
    active = peak = 0
    for _, change in sorted(events):
        active += change
        peak = max(peak, active)
    wall = (end - origin).total_seconds()
    job_seconds = sum(row["seconds"] for row in rows)
    waits = [
        (timestamp(job["started_at"]) - timestamp(job["created_at"])).total_seconds()
        for job in executed
        if job.get("created_at")
    ]
    failures = [
        {
            "name": row["name"],
            "url": row["url"],
            "failed_steps": [
                step["name"] for step in row["steps"] if step["conclusion"] == "failure"
            ],
        }
        for row in rows
        if row["conclusion"] not in {"success", "skipped"}
    ]
    build = next(
        (
            row
            for row in rows
            if row["name"] == "Windows NSIS acceptance / build"
            and row["conclusion"] == "success"
        ),
        None,
    )
    acceptance = [
        row for row in rows if "upgrade-and-start (" in row["name"] or "/ Candidate " in row["name"]
    ]
    return {
        "run_id": run["id"],
        "url": run["html_url"],
        "sha": run["head_sha"],
        "event": run["event"],
        "status": run["status"],
        "conclusion": run.get("conclusion"),
        "wall_seconds": wall,
        "executed_jobs": len(executed),
        "completed_jobs": len(complete),
        "skipped_jobs": sum(job.get("conclusion") == "skipped" for job in jobs),
        "peak_jobs": peak,
        "mean_jobs": job_seconds / wall if wall else 0,
        "summed_job_seconds": job_seconds,
        "median_created_to_start_seconds": median(waits) if waits else None,
        "nsis_candidate_ready_seconds": build["finish_seconds"] if build else None,
        "nsis_first_acceptance_start_seconds": min(
            (row["start_seconds"] for row in acceptance), default=None
        ),
        "nsis_last_acceptance_finish_seconds": max(
            (row["finish_seconds"] for row in acceptance), default=None
        ),
        "failures": failures,
        "jobs": sorted(rows, key=lambda row: row["seconds"], reverse=True),
    }


def load_sample(directory: Path) -> dict[str, Any]:
    run = json.loads((directory / "run.json").read_text(encoding="utf-8-sig"))
    if run["status"] != "completed":
        raise ValueError("timing comparison requires a completed workflow run")
    payload = json.loads((directory / "jobs.json").read_text(encoding="utf-8-sig"))
    pages = payload if isinstance(payload, list) else [payload]
    jobs = [job for page in pages for job in page["jobs"]]
    # A snapshot must include every page. Refuse a misleading partial result.
    expected = pages[0]["total_count"]
    if len(jobs) != expected or len({job["id"] for job in jobs}) != len(jobs):
        raise ValueError(f"incomplete or duplicate job snapshot: {len(jobs)} / {expected}")
    if any(job["status"] != "completed" for job in jobs):
        raise ValueError("timing comparison requires every job to have completed")
    return analyze(run, jobs)


def duration(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{int(value) // 60}:{int(value) % 60:02d}"


def compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> str:
    lines = [
        "# CI parallelism timing experiment",
        "",
        "Both samples retain their actual conclusions. "
        "Failed or skipped work is not proof of equivalent completed coverage.",
        "",
        "| Metric | Baseline | Candidate |",
        "| --- | ---: | ---: |",
    ]
    metrics = [
        ("Run", "url"),
        ("Conclusion", "conclusion"),
        ("Event", "event"),
        ("Wall time (trigger to last completed job)", "wall_seconds"),
        ("Executed jobs", "executed_jobs"),
        ("Completed jobs", "completed_jobs"),
        ("Skipped jobs", "skipped_jobs"),
        ("Peak running jobs", "peak_jobs"),
        ("Mean running jobs", "mean_jobs"),
        ("Summed runner time", "summed_job_seconds"),
        ("Median job-created to start (seconds)", "median_created_to_start_seconds"),
        ("NSIS candidate ready", "nsis_candidate_ready_seconds"),
        ("NSIS first acceptance starts", "nsis_first_acceptance_start_seconds"),
        ("NSIS last acceptance attempt finishes", "nsis_last_acceptance_finish_seconds"),
    ]
    for label, key in metrics:
        vals = [sample[key] for sample in (baseline, candidate)]
        if key.endswith("seconds") and not key.startswith("median"):
            vals = [duration(value) for value in vals]
        elif key == "mean_jobs":
            vals = [f"{value:.1f}" for value in vals]
        elif key == "url":
            vals = [f"[run]({value})" for value in vals]
        lines.append(f"| {label} | {vals[0]} | {vals[1]} |")
    if baseline["wall_seconds"]:
        delta = baseline["wall_seconds"] - candidate["wall_seconds"]
        lines += [
            "",
            f"Observed wall-time difference: {delta:.0f} seconds "
            f"({delta / baseline['wall_seconds']:.1%}).",
            "This is a single-run observation; inspect failed steps and coverage "
            "before interpreting it as speedup.",
        ]
    for label, sample in (("Baseline", baseline), ("Candidate", candidate)):
        lines += ["", f"## {label} failures", ""]
        lines += [
            f"- [{failure['name']}]({failure['url']}): {', '.join(failure['failed_steps'])}"
            for failure in sample["failures"]
        ] or ["None."]
        lines += [
            "",
            f"## {label} longest jobs",
            "",
            "| Job | Start | Duration | Finish | Result |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        for row in sample["jobs"][:20]:
            lines.append(
                f"| [{row['name']}]({row['url']}) | {duration(row['start_seconds'])} "
                f"| {duration(row['seconds'])} | {duration(row['finish_seconds'])} "
                f"| {row['conclusion']} |"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    baseline = load_sample(args.baseline)
    candidate = load_sample(args.candidate)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "comparison.json").write_text(
        json.dumps({"baseline": baseline, "candidate": candidate}, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "comparison.md").write_text(compare(baseline, candidate), encoding="utf-8")


if __name__ == "__main__":
    main()
