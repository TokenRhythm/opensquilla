#!/usr/bin/env python3
"""skill-creator-proposals: write/list/show/accept/reject proposals.

Subprocess entrypoint. The real logic lives in
``opensquilla.skills.proposals_lib`` so the gateway RPC layer can
call the same code in-process. This file is a thin CLI shim — it
parses argv, dispatches to the library, and serialises the result to
stdout as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Wheel-install / editable-install compatibility: make the opensquilla package
# importable when this script is invoked directly. parents layout:
#   scripts[0] → skill-creator-proposals[1] → bundled[2] → skills[3]
#   → opensquilla[4] → src (or site-packages)[5]
_OPENSQUILLA_ROOT = Path(__file__).resolve().parents[4]
if str(_OPENSQUILLA_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_OPENSQUILLA_ROOT.parent))

from opensquilla.paths import default_opensquilla_home  # noqa: E402
from opensquilla.skills import proposals_lib  # noqa: E402


def cmd_write_proposal(args: argparse.Namespace) -> dict:
    skill_md = (
        args.skill_md_inline
        if args.skill_md_inline
        else Path(args.skill_md).read_text(encoding="utf-8")
    )
    lint_result = json.loads(args.lint_result)
    smoke_result = json.loads(args.smoke_result)
    return proposals_lib.write_proposal(
        Path(args.home),
        skill_md,
        lint_result,
        smoke_result,
        creator_mode=args.creator_mode,
        acceptance_result=args.acceptance_result,
        runtime_e2e_result=args.runtime_e2e_result,
        collision_result=args.collision_result,
        risk_result=args.risk_result,
    )


def cmd_list(args: argparse.Namespace) -> dict:
    return proposals_lib.list_proposals(Path(args.home))


def cmd_pending_count(args: argparse.Namespace) -> dict:
    return proposals_lib.pending_count(Path(args.home))


def cmd_show(args: argparse.Namespace) -> dict:
    return proposals_lib.show_proposal(Path(args.home), args.proposal_id)


def cmd_accept(args: argparse.Namespace) -> dict:
    return proposals_lib.accept_proposal(
        Path(args.home), args.proposal_id, bool(args.force),
    )


def cmd_reject(args: argparse.Namespace) -> dict:
    return proposals_lib.reject_proposal(Path(args.home), args.proposal_id)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--action",
        required=True,
        choices=[
            "write_proposal", "list", "show", "accept", "reject",
            "pending_count",
        ],
    )
    p.add_argument(
        "--home",
        default=str(default_opensquilla_home()),
        help=(
            "OpenSquilla home dir (default: ~/.opensquilla/ or "
            "$OPENSQUILLA_STATE_DIR). Proposals are written under <home>/proposals/."
        ),
    )
    p.add_argument("--skill-md", default=None)
    p.add_argument("--skill-md-inline", default=None)
    p.add_argument("--lint-result", default="{}")
    p.add_argument("--smoke-result", default="{}")
    p.add_argument("--creator-mode", default="")
    p.add_argument("--acceptance-result", default="")
    p.add_argument("--runtime-e2e-result", default="")
    p.add_argument("--collision-result", default="")
    p.add_argument("--risk-result", default="")
    p.add_argument("--proposal-id", default=None)
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)

    dispatch = {
        "write_proposal": cmd_write_proposal,
        "list": cmd_list,
        "show": cmd_show,
        "accept": cmd_accept,
        "reject": cmd_reject,
        "pending_count": cmd_pending_count,
    }
    result = dispatch[args.action](args)
    json.dump(result, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
