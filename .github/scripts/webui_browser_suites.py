#!/usr/bin/env python3
"""Select disjoint browser file shards while keeping resource-sensitive gates isolated."""

from __future__ import annotations

import argparse
from pathlib import Path

GENERAL_SPECS = (
    "assistant-activity.spec.ts",
    "auth-connection-recovery.spec.ts",
    "chat-send-lifecycle.spec.ts",
    "chat-send-lifecycle.real.spec.ts",
    "cross-window-sync.real.spec.ts",
    "composer-paste.spec.ts",
    "durable-delivery.spec.ts",
    "history-content-reading.spec.ts",
    "history-hydration.spec.ts",
    "http-delivery.spec.ts",
    "idle-chat-recovery.spec.ts",
    "modern-session-recovery.spec.ts",
    "provider-error-experience.spec.ts",
    "retired-bgm-upgrade.spec.ts",
    "support-bundle.spec.ts",
    "console-clarity.spec.ts",
    "mobile-tabs.spec.ts",
    "header-responsive.spec.ts",
    "topbar-global-controls.spec.ts",
    "topbar-visual.spec.ts",
    "router-physical-model.spec.ts",
    "user-message-newlines.spec.ts",
    "session-created-card.spec.ts",
    "session-switch-transport.spec.ts",
    "new-task-ensemble-race.spec.ts",
    "ensemble-new-task-legacy-turn.spec.ts",
    "goal-mode.spec.ts",
    "plan-questionnaire-lifecycle.spec.ts",
    "plan-presentation.spec.ts",
    "task-progress.spec.ts",
    "queue-steer.spec.ts",
    "share.spec.ts",
    "session-chat-reliability.real.spec.ts",
    "session-delivery-feedback.spec.ts",
    "receipt-resolution.real.spec.ts",
)
GENERAL_SHARDS = tuple(f"general-{index}" for index in range(1, 5))
VIRTUALIZATION_SPECS = (
    "chat-virtualization.spec.ts",
    "conversation-minimap.spec.ts",
    "floating-composer.spec.ts",
    "long-task-resilience.spec.ts",
    "native-gateway.spec.ts",
    "sidebar-drag.spec.ts",
    "sidebar-hover-geometry.spec.ts",
    "sidebar-virtualization.spec.ts",
)
CAPACITY_SPECS = ("long-task-resilience.spec.ts", "sidebar-virtualization.spec.ts")


def specs_for_suite(suite: str) -> tuple[str, ...]:
    if suite in GENERAL_SHARDS:
        return GENERAL_SPECS[GENERAL_SHARDS.index(suite) :: len(GENERAL_SHARDS)]
    if suite == "virtualization":
        return VIRTUALIZATION_SPECS
    if suite == "capacity":
        return CAPACITY_SPECS
    raise ValueError(f"Unknown browser suite: {suite}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=(*GENERAL_SHARDS, "virtualization", "capacity"))
    args = parser.parse_args()
    specs = specs_for_suite(args.suite)
    e2e = Path(__file__).resolve().parents[2] / "opensquilla-webui" / "e2e"
    missing = [spec for spec in specs if not (e2e / spec).is_file()]
    if missing:
        parser.error(f"Browser suite references missing specs: {missing}")
    print("\n".join(specs))


if __name__ == "__main__":
    main()
