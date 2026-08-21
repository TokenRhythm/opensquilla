from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from opensquilla import private_paths


def test_windows_private_acl_is_verified_through_the_same_bound_handle() -> None:
    events: list[tuple[object, ...]] = []

    class FakeNative:
        def current_user_sid(self) -> str:
            return "S-1-5-21-123"

        @contextlib.contextmanager
        def open_bound(self, *_args: object, **_kwargs: object) -> Iterator[object]:
            events.append(("open",))
            yield "synthetic-handle"
            events.append(("close",))

        def set_protected_dacl(self, handle: object, sddl: str) -> None:
            events.append(("set", handle, sddl))

        def read_dacl_sddl(self, handle: object) -> str:
            events.append(("read", handle))
            return "O:S-1-5-21-123D:P(A;;FA;;;S-1-5-21-123)(A;;FA;;;SY)"

    private_paths.apply_windows_private_dacl(
        Path("synthetic-private-file"),
        directory=False,
        expected_device=7,
        expected_inode=42,
        native=FakeNative(),
    )

    assert events == [
        ("open",),
        (
            "set",
            "synthetic-handle",
            "D:P(A;;FA;;;S-1-5-21-123)(A;;FA;;;SY)",
        ),
        ("read", "synthetic-handle"),
        ("close",),
    ]


def test_windows_private_acl_rejects_unverified_extra_principal() -> None:
    class FakeNative:
        def current_user_sid(self) -> str:
            return "S-1-5-21-123"

        @contextlib.contextmanager
        def open_bound(self, *_args: object, **_kwargs: object) -> Iterator[object]:
            yield "synthetic-handle"

        def set_protected_dacl(self, _handle: object, _sddl: str) -> None:
            return None

        def read_dacl_sddl(self, _handle: object) -> str:
            return "O:S-1-5-21-123D:P(A;;FA;;;S-1-5-21-123)(A;;FA;;;SY)(A;;FR;;;S-1-1-0)"

    with pytest.raises(OSError, match="did not retain a private DACL"):
        private_paths.apply_windows_private_dacl(
            Path("synthetic-private-file"),
            directory=False,
            expected_device=7,
            expected_inode=42,
            native=FakeNative(),
        )


def test_windows_private_acl_accepts_private_inherited_sidecar_shape() -> None:
    assert private_paths._windows_sddl_is_private(
        "O:S-1-5-21-123D:AI(A;ID;FA;;;S-1-5-21-123)(A;ID;FA;;;SY)",
        user_sid="S-1-5-21-123",
        directory=False,
        require_protected=False,
    )
    assert not private_paths._windows_sddl_is_private(
        "O:S-1-5-21-123D:AI(A;ID;FA;;;S-1-5-21-123)(A;ID;FA;;;SY)",
        user_sid="S-1-5-21-123",
        directory=False,
        require_protected=True,
    )


def test_windows_private_acl_rejects_untrusted_owner() -> None:
    assert not private_paths._windows_sddl_is_private(
        "O:S-1-5-21-999D:P(A;;FA;;;S-1-5-21-123)(A;;FA;;;SY)",
        user_sid="S-1-5-21-123",
        directory=False,
        require_protected=True,
    )


def test_windows_private_acl_accepts_trusted_administrators_owner() -> None:
    assert private_paths._windows_sddl_is_private(
        "O:BAD:P(A;;FA;;;S-1-5-21-123)(A;;FA;;;SY)",
        user_sid="S-1-5-21-123",
        directory=False,
        require_protected=True,
    )


def test_windows_private_acl_canonicalizes_aliases_and_exact_hex_masks() -> None:
    canonical_sids = {
        "LA": "S-1-5-21-123-500",
        "SY": "S-1-5-18",
        "BA": "S-1-5-32-544",
    }
    canonicalized: list[str] = []

    def canonicalize_sid(value: str) -> str:
        canonicalized.append(value)
        return canonical_sids.get(value, value)

    assert private_paths._windows_sddl_is_private(
        "O:LAD:P(A;CIOI;0x001f01ff;;;SY)(A;OICI;0x1F01FF;;;LA)",
        user_sid="S-1-5-21-123-500",
        directory=True,
        require_protected=True,
        canonicalize_sid=canonicalize_sid,
    )
    assert {"LA", "SY", "BA", "S-1-5-21-123-500"} <= set(canonicalized)


def test_windows_private_acl_rejects_non_full_hex_access_mask() -> None:
    assert not private_paths._windows_sddl_is_private(
        "O:S-1-5-21-123D:P"
        "(A;;0x001f01fe;;;S-1-5-21-123)"
        "(A;;0x001f01ff;;;SY)",
        user_sid="S-1-5-21-123",
        directory=False,
        require_protected=True,
    )


def test_windows_private_acl_rejects_unresolvable_principal() -> None:
    def reject_sid(_value: str) -> str:
        raise OSError("synthetic SID conversion failure")

    assert not private_paths._windows_sddl_is_private(
        "O:LAD:P(A;;FA;;;LA)(A;;FA;;;SY)",
        user_sid="S-1-5-21-123-500",
        directory=False,
        require_protected=True,
        canonicalize_sid=reject_sid,
    )


def test_windows_private_acl_deduplicates_system_service_identity() -> None:
    assert (
        private_paths._private_windows_sddl(
            "S-1-5-18",
            directory=True,
        )
        == "D:P(A;OICI;FA;;;S-1-5-18)"
    )
    assert private_paths._windows_sddl_is_private(
        "O:SYD:P(A;OICI;FA;;;SY)",
        user_sid="S-1-5-18",
        directory=True,
        require_protected=True,
    )
