#!/usr/bin/env python3
"""Live meta-skill creator E2E harness.

This intentionally prints only structural evidence. It never prints provider
API keys loaded from ``--env-file`` or the process environment.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from opensquilla.skills import proposals_lib
from opensquilla.skills.creator import proposer

DEFAULT_HISTORY = {
    "co_occurrences": [
        {"skills": ["history-explorer", "docx"], "freq": 8},
    ],
    "note": "Prefer a two-step read-and-summarize workflow using low-risk skills.",
}
DEFAULT_INTENT = (
    "Create a meta-skill that first uses history-explorer to inspect recent "
    "OpenSquilla decision history for a query, then uses docx to produce "
    "a concise operational brief. Use only history-explorer and docx."
)


def _load_env_file(path: Path | None) -> None:
    if path is None or not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def run_live_meta_skill_creator_e2e(
    *,
    home: Path | None = None,
    pattern_id: str = "p1_sequential",
    history_summary: str | None = None,
    user_intent: str = DEFAULT_INTENT,
    provider: str | None = None,
    model: str | None = None,
    auto_enable: bool = True,
    auto_enable_max_risk: str = "medium",
) -> dict[str, Any]:
    """Run fill_slots -> assemble -> lint -> smoke -> persist/auto-enable."""
    previous_provider = os.environ.get("OPENSQUILLA_LLM_PROVIDER")
    previous_model = os.environ.get("OPENSQUILLA_LLM_MODEL")
    if provider:
        os.environ["OPENSQUILLA_LLM_PROVIDER"] = provider
    if model:
        os.environ["OPENSQUILLA_LLM_MODEL"] = model

    try:
        home_path = home or Path(tempfile.mkdtemp(prefix="opensquilla-live-meta-skill-"))
        home_path.mkdir(parents=True, exist_ok=True)
        proposals_lib.write_auto_propose_settings(
            home_path,
            {
                "auto_enable": auto_enable,
                "auto_enable_max_risk": auto_enable_max_risk,
            },
        )

        history = history_summary or json.dumps(DEFAULT_HISTORY, ensure_ascii=False)
        slots_json = proposer.meta_skill_fill_slots(pattern_id, history, user_intent)
        slots = json.loads(slots_json)
        skill_md = proposer.meta_skill_assemble(pattern_id, slots_json)
        lint_result = json.loads(proposer.meta_skill_lint_run(skill_md, "G1,G2"))
        smoke_result = proposer.run_smoke_gates(
            skill_md=skill_md,
            fixture_gen_fn=lambda _md, kind: {
                "positive": f"please use {slots['triggers'][0]} for recent decisions",
                "negative": "what is the weather tomorrow in Tokyo?",
            }[kind],
            classifier_model=model or "live-meta-skill-creator-e2e",
        )
        persist = json.loads(proposer.meta_skill_persist_proposal(
            skill_md,
            json.dumps(lint_result),
            json.dumps(smoke_result),
            home=str(home_path),
        ))
        managed = (
            sorted(p.name for p in (home_path / "skills").iterdir())
            if (home_path / "skills").is_dir()
            else []
        )
        pending = (
            sorted(p.name for p in (home_path / "proposals").iterdir())
            if (home_path / "proposals").is_dir()
            else []
        )
        return {
            "ok": True,
            "home": str(home_path),
            "llm_slots": {
                "name": slots.get("name"),
                "triggers": slots.get("triggers"),
                "steps": [
                    {"id": s.get("id"), "skill": s.get("skill")}
                    for s in slots.get("steps", [])
                ],
            },
            "lint": lint_result,
            "smoke": smoke_result,
            "persist": persist,
            "managed": managed,
            "pending": pending,
        }
    finally:
        if previous_provider is None:
            os.environ.pop("OPENSQUILLA_LLM_PROVIDER", None)
        else:
            os.environ["OPENSQUILLA_LLM_PROVIDER"] = previous_provider
        if previous_model is None:
            os.environ.pop("OPENSQUILLA_LLM_MODEL", None)
        else:
            os.environ["OPENSQUILLA_LLM_MODEL"] = previous_model


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env-file", type=Path, default=None)
    p.add_argument("--home", type=Path, default=None)
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--pattern-id", default="p1_sequential")
    p.add_argument("--history-summary", default=None)
    p.add_argument("--user-intent", default=DEFAULT_INTENT)
    p.add_argument("--no-auto-enable", action="store_true")
    p.add_argument("--auto-enable-max-risk", default="medium")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _load_env_file(args.env_file)
    result = run_live_meta_skill_creator_e2e(
        home=args.home,
        pattern_id=args.pattern_id,
        history_summary=args.history_summary,
        user_intent=args.user_intent,
        provider=args.provider,
        model=args.model,
        auto_enable=not args.no_auto_enable,
        auto_enable_max_risk=args.auto_enable_max_risk,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
