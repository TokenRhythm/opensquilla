"""Pure planning: turn an :class:`Inventory` + options into an :class:`UninstallPlan`.

This module performs **no I/O** beyond reading the inventory snapshot it is
handed (which itself globbed/stat'd at discovery time). It decides *what* would
happen — the ordered list of stop/remove/purge actions, what is kept, and what
the user must handle manually — so ``--dry-run`` and ``--json`` render exactly
what execution will do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opensquilla.uninstall import safety
from opensquilla.uninstall.inventory import (
    PURGE_ALL_ONLY,
    PURGE_CONFIG,
    PURGE_STATE,
    REFUSE_PROGRAM_REMOVAL,
    Inventory,
)


@dataclass
class PlanOptions:
    purge_state: bool = False
    purge_config: bool = False
    purge_all: bool = False
    remove_source_dir: bool = False

    @property
    def any_purge(self) -> bool:
        return self.purge_state or self.purge_config or self.purge_all

    @property
    def effective_state(self) -> bool:
        return self.purge_state or self.purge_all

    @property
    def effective_config(self) -> bool:
        return self.purge_config or self.purge_all


@dataclass
class Action:
    kind: str  # stop-gateway | unregister-service | run-package-uninstall
    #          | remove-path | remove-tree | manual | instructions
    summary: str
    paths: list[str] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "summary": self.summary}
        if self.paths:
            payload["paths"] = self.paths
        if self.commands:
            payload["commands"] = self.commands
        if self.reason:
            payload["reason"] = self.reason
        return payload


@dataclass
class UninstallPlan:
    method: str
    home: str
    actions: list[Action] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)
    manual: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "home": self.home,
            "actions": [a.to_payload() for a in self.actions],
            "keep": self.keep,
            "manual": [a.to_payload() for a in self.manual],
            "warnings": self.warnings,
        }


_DOCKER_INSTRUCTIONS = (
    "OpenSquilla is running in a container; its files are immutable image layers "
    "and its state is a mounted volume. Remove it from the host with: "
    "`docker compose down -v` (or `docker rm -f <ctr> && docker volume rm "
    "opensquilla-state && docker image rm <image>`)."
)

_DESKTOP_INSTRUCTIONS = (
    "This is the desktop app's bundled runtime. Quit OpenSquilla and remove the "
    "application via your OS (drag OpenSquilla.app to Trash on macOS, or use "
    "Add/Remove Programs on Windows). The desktop app removes its own profile."
)

_UNKNOWN_INSTRUCTIONS = (
    "Could not determine how OpenSquilla was installed, so the program runtime "
    "was left untouched to avoid deleting the wrong files. Remove it with the "
    "tool you used to install it (pip / uv / pipx), then re-run with --purge-all "
    "to clear data."
)


def build_plan(inventory: Inventory, options: PlanOptions) -> UninstallPlan:
    """Build the ordered uninstall plan from an inventory + caller options."""
    plan = UninstallPlan(method=inventory.method, home=str(inventory.home))

    # 1. Quiesce the gateway first (drain in-flight work before deleting files).
    plan.actions.append(Action("stop-gateway", "Stop the running gateway (graceful drain, if any)"))

    # 2. Unregister OS service units so a supervisor can't respawn a deleted app.
    for svc in inventory.services:
        paths = [str(svc.path)] if svc.path else []
        plan.actions.append(
            Action(
                "unregister-service",
                f"Unregister {svc.platform} service '{svc.label}'",
                paths=paths,
                commands=list(svc.commands),
            )
        )

    # 3. Remove the program (or refuse + instruct for non-user-managed installs).
    _plan_program_removal(inventory, options, plan)

    # 4. Purge data per flags (or record what is kept).
    _plan_data_purge(inventory, options, plan)

    for note in inventory.notes:
        plan.warnings.append(note)

    return plan


def _plan_program_removal(inventory: Inventory, options: PlanOptions, plan: UninstallPlan) -> None:
    method = inventory.method
    if method in REFUSE_PROGRAM_REMOVAL:
        instructions = {
            "docker": _DOCKER_INSTRUCTIONS,
            "desktop": _DESKTOP_INSTRUCTIONS,
        }.get(method, _UNKNOWN_INSTRUCTIONS)
        plan.manual.append(
            Action("instructions", "Remove the OpenSquilla program", reason=instructions)
        )
        return

    if inventory.program_paths:
        plan.actions.append(
            Action(
                "remove-tree",
                "Remove the portable OpenSquilla runtime (venv)",
                paths=[str(p) for p in inventory.program_paths],
            )
        )
        plan.manual.append(
            Action(
                "instructions",
                "Portable download directory",
                reason="The extracted portable release folder (start.sh / packages / "
                "runtime) is your download — delete it manually if you no longer need it.",
            )
        )
        return

    if inventory.package_uninstall:
        plan.actions.append(
            Action(
                "run-package-uninstall",
                f"Uninstall the opensquilla package ({method})",
                commands=[list(inventory.package_uninstall)],
            )
        )
        if method == "source-editable":
            plan.warnings.append(
                "Source/editable install: only the package metadata and console "
                "scripts are removed — your git checkout is left untouched."
            )
            if options.remove_source_dir and inventory.source_checkout is not None:
                plan.manual.append(
                    Action(
                        "instructions",
                        "Remove the source checkout",
                        paths=[str(inventory.source_checkout)],
                        reason="--remove-source-dir does not auto-delete a git working "
                        "tree (it may contain uncommitted work and unrelated files). "
                        "Remove it yourself once you have confirmed the path.",
                    )
                )
        return

    plan.manual.append(
        Action(
            "instructions",
            "Remove the OpenSquilla program",
            reason="The package manager for this install could not be resolved; "
            "remove the 'opensquilla' package manually (pip / uv / pipx).",
        )
    )


def _plan_data_purge(inventory: Inventory, options: PlanOptions, plan: UninstallPlan) -> None:
    home_protected = safety.protected_root_reason(inventory.home)
    # A whole-home rmtree requires BOTH that the home is not a dangerous root AND
    # that it is positively recognized as an OpenSquilla home — otherwise fall
    # back to removing only the known buckets individually.
    purge_whole_home = options.purge_all and home_protected is None and inventory.home_recognized

    if options.purge_all and home_protected is not None:
        plan.warnings.append(
            f"Refusing to recursively delete the OpenSquilla home "
            f"({inventory.home}): {home_protected}. Known files are removed "
            "individually instead."
        )
    elif options.purge_all and not inventory.home_recognized:
        plan.warnings.append(
            f"The OpenSquilla home ({inventory.home}) is not recognized as a "
            "standard OpenSquilla directory; removing only known files instead of "
            "deleting the whole directory."
        )

    scheduled_removal_roots: list[Path] = []
    for bucket in inventory.buckets:
        existing = bucket.existing_paths()
        if not existing:
            continue

        if bucket.outside_home:
            # Relocated outside the home (e.g. via an absolute env override) —
            # may point into user-owned trees, so never auto-delete. Only surface
            # it as a manual step when the user actually intends to purge data;
            # on a default keep-all uninstall it would just be noise.
            if options.any_purge:
                plan.manual.append(
                    Action(
                        "manual",
                        f"{bucket.name} is outside the OpenSquilla home",
                        paths=[str(p) for p in existing],
                        reason="Located outside the OpenSquilla home (relocated via env/"
                        "config); remove manually if you intend to.",
                    )
                )
            continue

        if purge_whole_home:
            # Subsumed by the whole-home removal below; don't double-list.
            continue

        enabled = (
            (bucket.purge_flag == PURGE_STATE and options.effective_state)
            or (bucket.purge_flag == PURGE_CONFIG and options.effective_config)
            or (bucket.purge_flag == PURGE_ALL_ONLY and options.purge_all)
        )
        if enabled:
            # Inventory may expose a notable child bucket (for example managed
            # toolchains) alongside its containing state directory. Keep that
            # visibility in dry-run/keep output, but never schedule overlapping
            # destructive actions for the same tree.
            uncovered = [
                path
                for path in existing
                if not any(safety.is_within(path, root) for root in scheduled_removal_roots)
            ]
            if not uncovered:
                continue
            plan.actions.append(
                Action(
                    "remove-path",
                    f"Delete {bucket.name}",
                    paths=[str(p) for p in uncovered],
                )
            )
            scheduled_removal_roots.extend(uncovered)
        else:
            plan.keep.append(f"{bucket.name} ({existing[0]})")

    if purge_whole_home:
        plan.actions.append(
            Action(
                "remove-tree",
                f"Delete the OpenSquilla home and all data ({inventory.home})",
                paths=[str(inventory.home)],
            )
        )

    if not options.any_purge:
        plan.keep.append(
            f"All user data under {inventory.home} "
            "(pass --purge-state / --purge-config / --purge-all to remove)"
        )
