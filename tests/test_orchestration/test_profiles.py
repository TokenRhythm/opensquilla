from __future__ import annotations

import pytest

from opensquilla.orchestration.profiles import (
    PRESET_PROFILES,
    AgentProfile,
    ProfileCapabilityError,
    get_profile,
    resolve_profile_tools,
)


def test_inherit_profile_preserves_parent_effective_tools() -> None:
    inherited = frozenset({"read_file", "apply_patch", "delegate_task"})

    assert (
        resolve_profile_tools(
            inherited=inherited,
            registered=inherited,
            preset=get_profile("inherit"),
        )
        == inherited
    )


def test_preset_filters_after_inheritance_and_deny_wins() -> None:
    preset = AgentProfile(
        name="reviewer-test",
        allow=frozenset({"read_file", "exec_command", "apply_patch"}),
        deny=frozenset({"apply_patch"}),
        required=frozenset({"read_file", "exec_command"}),
        may_delegate=False,
    )

    effective = resolve_profile_tools(
        inherited=frozenset({"read_file", "exec_command", "apply_patch", "web_search"}),
        registered=frozenset({"read_file", "exec_command", "apply_patch", "web_search"}),
        preset=preset,
    )

    assert effective == frozenset({"read_file", "exec_command"})


def test_unknown_allow_tool_fails_instead_of_silently_degrading() -> None:
    preset = AgentProfile(
        name="broken",
        allow=frozenset({"read_file", "missing_tool"}),
        required=frozenset({"read_file"}),
    )

    with pytest.raises(ProfileCapabilityError) as exc_info:
        resolve_profile_tools(
            inherited=frozenset({"read_file"}),
            registered=frozenset({"read_file"}),
            preset=preset,
        )

    assert exc_info.value.unknown == frozenset({"missing_tool"})
    assert exc_info.value.missing == frozenset()


def test_required_tool_must_survive_inheritance_and_deny_filtering() -> None:
    preset = AgentProfile(
        name="broken",
        allow=frozenset({"read_file", "exec_command"}),
        deny=frozenset({"exec_command"}),
        required=frozenset({"read_file", "exec_command"}),
    )

    with pytest.raises(ProfileCapabilityError) as exc_info:
        resolve_profile_tools(
            inherited=frozenset({"read_file", "exec_command"}),
            registered=frozenset({"read_file", "exec_command"}),
            preset=preset,
        )

    assert exc_info.value.missing == frozenset({"exec_command"})


def test_only_inherit_and_worker_presets_can_delegate() -> None:
    assert PRESET_PROFILES["inherit"].may_delegate is True
    assert PRESET_PROFILES["worker"].may_delegate is True
    assert PRESET_PROFILES["explorer"].may_delegate is False
    assert PRESET_PROFILES["researcher"].may_delegate is False
    assert PRESET_PROFILES["reviewer"].may_delegate is False


def test_read_only_presets_exclude_generic_execution_and_mutation_tools() -> None:
    unsafe = {
        "apply_patch",
        "background_process",
        "edit_file",
        "exec_command",
        "process",
        "write_file",
    }
    for name in ("explorer", "researcher", "reviewer"):
        profile = PRESET_PROFILES[name]
        assert profile.allow is not None
        assert unsafe.isdisjoint(profile.allow)
        assert unsafe <= profile.deny or unsafe.isdisjoint(profile.allow)


def test_unknown_profile_name_is_rejected() -> None:
    with pytest.raises(ProfileCapabilityError, match="Unknown agent profile"):
        get_profile("planner")
