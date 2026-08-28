"""Windows Filtering Platform hardening for ``windows_default``.

The Windows firewall rules are the primary network boundary for subprocesses
running as the offline sandbox account. These WFP filters cover traffic shapes
that should never be left to proxy environment variables: ICMP, DNS, SMB, and
loopback traffic outside the managed proxy port.
"""

# mypy: disable-error-code=attr-defined

from __future__ import annotations

import ctypes
import sys
import uuid
from dataclasses import dataclass

from opensquilla.sandbox.types import SandboxBackendError

_ERROR_SUCCESS = 0
_FWP_E_FILTER_NOT_FOUND = 0x80320003
_FWP_E_NOT_FOUND = 0x80320008
_FWP_E_ALREADY_EXISTS = 0x80320009

_RPC_C_AUTHN_DEFAULT = 0xFFFFFFFF
_INFINITE = 0xFFFFFFFF

_FWPM_PROVIDER_FLAG_PERSISTENT = 0x00000001
_FWPM_SUBLAYER_FLAG_PERSISTENT = 0x00000001
_FWPM_FILTER_FLAG_PERSISTENT = 0x00000001

_FWP_ACTION_FLAG_TERMINATING = 0x00001000
_FWP_ACTION_BLOCK = 0x00000001 | _FWP_ACTION_FLAG_TERMINATING
_FWP_ACTRL_MATCH_FILTER = 1
_FWP_EMPTY = 0
_FWP_UINT8 = 1
_FWP_UINT16 = 2
_FWP_UINT32 = 3
_FWP_SECURITY_DESCRIPTOR_TYPE = 14
_FWP_MATCH_EQUAL = 0
_FWP_MATCH_FLAGS_ALL_SET = 6
_FWP_MATCH_NOT_EQUAL = 10
_FWP_CONDITION_FLAG_IS_LOOPBACK = 0x00000001

_GRANT_ACCESS = 1
_NO_MULTIPLE_TRUSTEE = 0
_TRUSTEE_IS_SID = 0
_TRUSTEE_IS_USER = 1

_IPPROTO_ICMP = 1
_IPPROTO_TCP = 6
_IPPROTO_UDP = 17
_IPPROTO_ICMPV6 = 58

_SESSION_NAME = "OpenSquilla Windows Sandbox WFP"
_PROVIDER_NAME = "OpenSquilla Windows Sandbox WFP"
_PROVIDER_DESCRIPTION = "Persistent WFP provider for OpenSquilla Windows sandbox filters"
_SUBLAYER_NAME = "OpenSquilla Windows Sandbox WFP"
_SUBLAYER_DESCRIPTION = "Persistent WFP sublayer for OpenSquilla Windows sandbox filters"

_PROVIDER_KEY = "8ec0b46b-f650-4145-b248-30b6c3e3a901"
_SUBLAYER_KEY = "b7c0560c-78ed-4010-8bbd-78ac2c2ab401"

_LAYER_KEYS: dict[str, str] = {
    "ALE_AUTH_CONNECT_V4": "c38d57d1-05a7-4c33-904f-7fbceee60e82",
    "ALE_AUTH_CONNECT_V6": "4a72393b-319f-44bc-84c3-ba54dcb3b6b4",
    "ALE_RESOURCE_ASSIGNMENT_V4": "1247d66d-0b60-4a15-8d44-7155d0f53a0c",
    "ALE_RESOURCE_ASSIGNMENT_V6": "55a650e1-5f0a-4eca-a653-88f53b26aa8c",
}

_CONDITION_KEYS: dict[str, str] = {
    "user": "af043a0a-b34d-4f86-979c-c90371af6e66",
    "flags": "632ce23b-5167-435c-86d7-e903684aa80c",
    "protocol": "3971ef2b-623e-4f9a-8cb1-6e79b806b9a7",
    "remote_port": "c35a604d-d22b-4e1a-91b4-68f674ee674b",
}


@dataclass(frozen=True)
class WfpFilterSpec:
    key: str
    name: str
    layer: str
    conditions: tuple[str, ...]


_BASE_WFP_FILTER_SPECS: tuple[WfpFilterSpec, ...] = (
    WfpFilterSpec(
        "7f67c090-5f57-4ec5-95df-474575629001",
        "opensquilla_wfp_icmp_connect_v4",
        "ALE_AUTH_CONNECT_V4",
        ("user", "icmp"),
    ),
    WfpFilterSpec(
        "7f67c090-5f57-4ec5-95df-474575629002",
        "opensquilla_wfp_icmp_connect_v6",
        "ALE_AUTH_CONNECT_V6",
        ("user", "icmpv6"),
    ),
    WfpFilterSpec(
        "7f67c090-5f57-4ec5-95df-474575629003",
        "opensquilla_wfp_icmp_assign_v4",
        "ALE_RESOURCE_ASSIGNMENT_V4",
        ("user", "icmp"),
    ),
    WfpFilterSpec(
        "7f67c090-5f57-4ec5-95df-474575629004",
        "opensquilla_wfp_icmp_assign_v6",
        "ALE_RESOURCE_ASSIGNMENT_V6",
        ("user", "icmpv6"),
    ),
    WfpFilterSpec(
        "7f67c090-5f57-4ec5-95df-474575629005",
        "opensquilla_wfp_dns_53_v4",
        "ALE_AUTH_CONNECT_V4",
        ("user", "remote_port:53"),
    ),
    WfpFilterSpec(
        "7f67c090-5f57-4ec5-95df-474575629006",
        "opensquilla_wfp_dns_53_v6",
        "ALE_AUTH_CONNECT_V6",
        ("user", "remote_port:53"),
    ),
    WfpFilterSpec(
        "7f67c090-5f57-4ec5-95df-474575629007",
        "opensquilla_wfp_dns_853_v4",
        "ALE_AUTH_CONNECT_V4",
        ("user", "remote_port:853"),
    ),
    WfpFilterSpec(
        "7f67c090-5f57-4ec5-95df-474575629008",
        "opensquilla_wfp_dns_853_v6",
        "ALE_AUTH_CONNECT_V6",
        ("user", "remote_port:853"),
    ),
    WfpFilterSpec(
        "7f67c090-5f57-4ec5-95df-474575629009",
        "opensquilla_wfp_smb_445_v4",
        "ALE_AUTH_CONNECT_V4",
        ("user", "remote_port:445"),
    ),
    WfpFilterSpec(
        "7f67c090-5f57-4ec5-95df-474575629010",
        "opensquilla_wfp_smb_445_v6",
        "ALE_AUTH_CONNECT_V6",
        ("user", "remote_port:445"),
    ),
    WfpFilterSpec(
        "7f67c090-5f57-4ec5-95df-474575629011",
        "opensquilla_wfp_smb_139_v4",
        "ALE_AUTH_CONNECT_V4",
        ("user", "remote_port:139"),
    ),
    WfpFilterSpec(
        "7f67c090-5f57-4ec5-95df-474575629012",
        "opensquilla_wfp_smb_139_v6",
        "ALE_AUTH_CONNECT_V6",
        ("user", "remote_port:139"),
    ),
)


def _normalise_allowed_proxy_ports(allowed_proxy_ports: tuple[int, ...]) -> tuple[int, ...]:
    try:
        ports = tuple(int(port) for port in allowed_proxy_ports)
    except (TypeError, ValueError) as exc:
        raise SandboxBackendError(
            "wfp_filter_install_failed: allowed proxy ports must be integers"
        ) from exc
    if any(port < 1 or port > 65535 for port in ports):
        raise SandboxBackendError(
            "wfp_filter_install_failed: allowed proxy ports must be in 1-65535"
        )
    return tuple(sorted(set(ports)))


def _loopback_tcp_conditions(allowed_proxy_ports: tuple[int, ...]) -> tuple[str, ...]:
    if not allowed_proxy_ports:
        return ("user", "loopback", "tcp")
    if len(allowed_proxy_ports) > 1:
        raise SandboxBackendError(
            "wfp_filter_install_failed: multiple loopback proxy ports are not supported"
        )
    return ("user", "loopback", "tcp", f"remote_port_not:{allowed_proxy_ports[0]}")


def wfp_filter_specs(
    allowed_proxy_ports: tuple[int, ...] = (48123,),
) -> tuple[WfpFilterSpec, ...]:
    ports = _normalise_allowed_proxy_ports(allowed_proxy_ports)
    loopback_tcp_conditions = _loopback_tcp_conditions(ports)
    return _BASE_WFP_FILTER_SPECS + (
        WfpFilterSpec(
            "7f67c090-5f57-4ec5-95df-474575629013",
            "opensquilla_wfp_loopback_udp_v4",
            "ALE_AUTH_CONNECT_V4",
            ("user", "loopback", "udp"),
        ),
        WfpFilterSpec(
            "7f67c090-5f57-4ec5-95df-474575629014",
            "opensquilla_wfp_loopback_udp_v6",
            "ALE_AUTH_CONNECT_V6",
            ("user", "loopback", "udp"),
        ),
        WfpFilterSpec(
            "7f67c090-5f57-4ec5-95df-474575629015",
            "opensquilla_wfp_loopback_tcp_except_proxy_v4",
            "ALE_AUTH_CONNECT_V4",
            loopback_tcp_conditions,
        ),
        WfpFilterSpec(
            "7f67c090-5f57-4ec5-95df-474575629016",
            "opensquilla_wfp_loopback_tcp_except_proxy_v6",
            "ALE_AUTH_CONNECT_V6",
            loopback_tcp_conditions,
        ),
    )


WFP_FILTER_SPECS: tuple[WfpFilterSpec, ...] = wfp_filter_specs()


def install_wfp_filters_for_user(
    offline_sid: str,
    *,
    allowed_proxy_ports: tuple[int, ...] = (48123,),
) -> None:
    if not sys.platform.startswith("win"):
        raise SandboxBackendError("wfp_filter_install_failed: Windows is required")
    if not offline_sid:
        raise SandboxBackendError("wfp_filter_install_failed: offline SID is required")
    _install_wfp_filters_native(offline_sid, allowed_proxy_ports=allowed_proxy_ports)


def _install_wfp_filters_native(
    offline_sid: str,
    *,
    allowed_proxy_ports: tuple[int, ...] = (48123,),
) -> None:
    specs = wfp_filter_specs(allowed_proxy_ports)
    native = _WfpNative()
    engine = native.open_engine()
    committed = False
    try:
        native.begin_transaction(engine)
        try:
            native.ensure_provider(engine)
            native.ensure_sublayer(engine)
            with _UserMatchCondition(native, offline_sid) as user_condition:
                for spec in specs:
                    native.delete_filter_if_present(engine, _guid(spec.key))
                    native.add_filter(engine, spec, user_condition)
            native.commit_transaction(engine)
            committed = True
        finally:
            if not committed:
                native.abort_transaction(engine)
    finally:
        native.close_engine(engine)


class _GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    )


class _FWP_BYTE_BLOB(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("size", ctypes.c_uint32),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
    )


class _FWPM_DISPLAY_DATA0(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("name", ctypes.c_wchar_p),
        ("description", ctypes.c_wchar_p),
    )


class _FWPM_SESSION0(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("sessionKey", _GUID),
        ("displayData", _FWPM_DISPLAY_DATA0),
        ("flags", ctypes.c_uint32),
        ("txnWaitTimeoutInMSec", ctypes.c_uint32),
        ("processId", ctypes.c_uint32),
        ("sid", ctypes.c_void_p),
        ("username", ctypes.c_wchar_p),
        ("kernelMode", ctypes.c_int),
    )


class _FWPM_PROVIDER0(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("providerKey", _GUID),
        ("displayData", _FWPM_DISPLAY_DATA0),
        ("flags", ctypes.c_uint32),
        ("providerData", _FWP_BYTE_BLOB),
        ("serviceName", ctypes.c_wchar_p),
    )


class _FWPM_SUBLAYER0(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("subLayerKey", _GUID),
        ("displayData", _FWPM_DISPLAY_DATA0),
        ("flags", ctypes.c_uint32),
        ("providerKey", ctypes.POINTER(_GUID)),
        ("providerData", _FWP_BYTE_BLOB),
        ("weight", ctypes.c_uint16),
    )


class _FWP_VALUE0_0(ctypes.Union):  # noqa: N801
    _fields_ = (
        ("uint8", ctypes.c_uint8),
        ("uint16", ctypes.c_uint16),
        ("uint32", ctypes.c_uint32),
        ("uint64", ctypes.c_uint64),
        ("byteBlob", ctypes.POINTER(_FWP_BYTE_BLOB)),
        ("sd", ctypes.POINTER(_FWP_BYTE_BLOB)),
    )


class _FWP_VALUE0(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("type", ctypes.c_uint32),
        ("Anonymous", _FWP_VALUE0_0),
    )


class _FWP_CONDITION_VALUE0_0(ctypes.Union):  # noqa: N801
    _fields_ = _FWP_VALUE0_0._fields_


class _FWP_CONDITION_VALUE0(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("type", ctypes.c_uint32),
        ("Anonymous", _FWP_CONDITION_VALUE0_0),
    )


class _FWPM_FILTER_CONDITION0(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("fieldKey", _GUID),
        ("matchType", ctypes.c_uint32),
        ("conditionValue", _FWP_CONDITION_VALUE0),
    )


class _FWPM_ACTION0_0(ctypes.Union):  # noqa: N801
    _fields_ = (
        ("filterType", _GUID),
        ("calloutKey", _GUID),
    )


class _FWPM_ACTION0(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("type", ctypes.c_uint32),
        ("Anonymous", _FWPM_ACTION0_0),
    )


class _FWPM_FILTER0_0(ctypes.Union):  # noqa: N801
    _fields_ = (
        ("rawContext", ctypes.c_uint64),
        ("providerContextKey", ctypes.POINTER(_GUID)),
        ("providerContextData", _FWP_BYTE_BLOB),
    )


class _FWPM_FILTER0(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("filterKey", _GUID),
        ("displayData", _FWPM_DISPLAY_DATA0),
        ("flags", ctypes.c_uint32),
        ("providerKey", ctypes.POINTER(_GUID)),
        ("providerData", _FWP_BYTE_BLOB),
        ("layerKey", _GUID),
        ("subLayerKey", _GUID),
        ("weight", _FWP_VALUE0),
        ("numFilterConditions", ctypes.c_uint32),
        ("filterCondition", ctypes.POINTER(_FWPM_FILTER_CONDITION0)),
        ("action", _FWPM_ACTION0),
        ("Anonymous", _FWPM_FILTER0_0),
        ("reserved", ctypes.POINTER(_GUID)),
        ("filterId", ctypes.c_uint64),
        ("effectiveWeight", _FWP_VALUE0),
    )


class _TRUSTEE_W(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_uint32),
        ("TrusteeForm", ctypes.c_uint32),
        ("TrusteeType", ctypes.c_uint32),
        ("ptstrName", ctypes.c_wchar_p),
    )


class _EXPLICIT_ACCESS_W(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("grfAccessPermissions", ctypes.c_uint32),
        ("grfAccessMode", ctypes.c_uint32),
        ("grfInheritance", ctypes.c_uint32),
        ("Trustee", _TRUSTEE_W),
    )


def _guid(value: str) -> _GUID:
    parsed = uuid.UUID(value)
    return _GUID(
        parsed.time_low,
        parsed.time_mid,
        parsed.time_hi_version,
        (_GUID._fields_[3][1])(*parsed.bytes[8:]),
    )


def _empty_blob() -> _FWP_BYTE_BLOB:
    return _FWP_BYTE_BLOB(0, None)


def _empty_value() -> _FWP_VALUE0:
    return _FWP_VALUE0(_FWP_EMPTY, _FWP_VALUE0_0())


def _filter_description(spec: WfpFilterSpec) -> str:
    return f"Block offline sandbox traffic for {spec.name}"


def _protocol_for_condition(raw: str) -> int | None:
    if raw == "icmp":
        return _IPPROTO_ICMP
    if raw == "icmpv6":
        return _IPPROTO_ICMPV6
    if raw == "tcp":
        return _IPPROTO_TCP
    if raw == "udp":
        return _IPPROTO_UDP
    return None


def _remote_port_condition(raw: str) -> tuple[int, int] | None:
    for prefix, match_type in (
        ("remote_port:", _FWP_MATCH_EQUAL),
        ("remote_port_not:", _FWP_MATCH_NOT_EQUAL),
    ):
        if raw.startswith(prefix):
            return int(raw.removeprefix(prefix)), match_type
    return None


def _condition_user(user_condition: _UserMatchCondition) -> _FWPM_FILTER_CONDITION0:
    value = _FWP_CONDITION_VALUE0(_FWP_SECURITY_DESCRIPTOR_TYPE, _FWP_CONDITION_VALUE0_0())
    value.Anonymous.sd = ctypes.pointer(user_condition.blob)
    return _FWPM_FILTER_CONDITION0(
        _guid(_CONDITION_KEYS["user"]),
        _FWP_MATCH_EQUAL,
        value,
    )


def _condition_protocol(protocol: int) -> _FWPM_FILTER_CONDITION0:
    value = _FWP_CONDITION_VALUE0(_FWP_UINT8, _FWP_CONDITION_VALUE0_0())
    value.Anonymous.uint8 = protocol
    return _FWPM_FILTER_CONDITION0(
        _guid(_CONDITION_KEYS["protocol"]),
        _FWP_MATCH_EQUAL,
        value,
    )


def _condition_loopback() -> _FWPM_FILTER_CONDITION0:
    value = _FWP_CONDITION_VALUE0(_FWP_UINT32, _FWP_CONDITION_VALUE0_0())
    value.Anonymous.uint32 = _FWP_CONDITION_FLAG_IS_LOOPBACK
    return _FWPM_FILTER_CONDITION0(
        _guid(_CONDITION_KEYS["flags"]),
        _FWP_MATCH_FLAGS_ALL_SET,
        value,
    )


def _condition_remote_port(
    port: int,
    match_type: int = _FWP_MATCH_EQUAL,
) -> _FWPM_FILTER_CONDITION0:
    value = _FWP_CONDITION_VALUE0(_FWP_UINT16, _FWP_CONDITION_VALUE0_0())
    value.Anonymous.uint16 = port
    return _FWPM_FILTER_CONDITION0(
        _guid(_CONDITION_KEYS["remote_port"]),
        match_type,
        value,
    )


def _build_conditions(
    spec: WfpFilterSpec,
    user_condition: _UserMatchCondition,
) -> ctypes.Array[_FWPM_FILTER_CONDITION0]:
    conditions: list[_FWPM_FILTER_CONDITION0] = []
    for raw in spec.conditions:
        if raw == "user":
            conditions.append(_condition_user(user_condition))
            continue
        if raw == "loopback":
            conditions.append(_condition_loopback())
            continue
        protocol = _protocol_for_condition(raw)
        if protocol is not None:
            conditions.append(_condition_protocol(protocol))
            continue
        remote_port = _remote_port_condition(raw)
        if remote_port is not None:
            port, match_type = remote_port
            conditions.append(_condition_remote_port(port, match_type))
            continue
        raise SandboxBackendError(f"wfp_filter_install_failed: unknown condition {raw!r}")
    array_type = _FWPM_FILTER_CONDITION0 * len(conditions)
    return array_type(*conditions)


class _UserMatchCondition:
    def __init__(self, native: _WfpNative, offline_sid: str) -> None:
        self._native = native
        self._sid = ctypes.c_void_p()
        self._sd = ctypes.c_void_p()
        self._sd_len = ctypes.c_uint32(0)
        self.blob = _FWP_BYTE_BLOB()

        ok = native.advapi32.ConvertStringSidToSidW(offline_sid, ctypes.byref(self._sid))
        if not ok:
            raise native.last_error("ConvertStringSidToSidW")

        access = _EXPLICIT_ACCESS_W()
        access.grfAccessPermissions = _FWP_ACTRL_MATCH_FILTER
        access.grfAccessMode = _GRANT_ACCESS
        access.grfInheritance = 0
        access.Trustee.pMultipleTrustee = None
        access.Trustee.MultipleTrusteeOperation = _NO_MULTIPLE_TRUSTEE
        access.Trustee.TrusteeForm = _TRUSTEE_IS_SID
        access.Trustee.TrusteeType = _TRUSTEE_IS_USER
        access.Trustee.ptstrName = ctypes.cast(self._sid, ctypes.c_wchar_p)

        result = native.advapi32.BuildSecurityDescriptorW(
            None,
            None,
            1,
            ctypes.byref(access),
            0,
            None,
            None,
            ctypes.byref(self._sd_len),
            ctypes.byref(self._sd),
        )
        native.ensure_success(result, "BuildSecurityDescriptorW")
        self.blob = _FWP_BYTE_BLOB(
            self._sd_len.value,
            ctypes.cast(self._sd, ctypes.POINTER(ctypes.c_uint8)),
        )

    def __enter__(self) -> _UserMatchCondition:
        return self

    def __exit__(self, *args: object) -> None:
        if self._sd.value:
            self._native.kernel32.LocalFree(self._sd)
            self._sd = ctypes.c_void_p()
        if self._sid.value:
            self._native.kernel32.LocalFree(self._sid)
            self._sid = ctypes.c_void_p()


class _WfpNative:
    def __init__(self) -> None:
        try:
            self.fwpuclnt = ctypes.WinDLL("fwpuclnt.dll", use_last_error=True)
            self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except Exception as exc:
            raise SandboxBackendError(
                f"wfp_filter_install_failed: Windows API unavailable: {exc}"
            ) from exc
        self._configure_prototypes()

    def _configure_prototypes(self) -> None:
        self.fwpuclnt.FwpmEngineOpen0.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(_FWPM_SESSION0),
            ctypes.POINTER(ctypes.c_void_p),
        )
        self.fwpuclnt.FwpmEngineOpen0.restype = ctypes.c_uint32
        self.fwpuclnt.FwpmEngineClose0.argtypes = (ctypes.c_void_p,)
        self.fwpuclnt.FwpmEngineClose0.restype = ctypes.c_uint32
        self.fwpuclnt.FwpmTransactionBegin0.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        self.fwpuclnt.FwpmTransactionBegin0.restype = ctypes.c_uint32
        self.fwpuclnt.FwpmTransactionCommit0.argtypes = (ctypes.c_void_p,)
        self.fwpuclnt.FwpmTransactionCommit0.restype = ctypes.c_uint32
        self.fwpuclnt.FwpmTransactionAbort0.argtypes = (ctypes.c_void_p,)
        self.fwpuclnt.FwpmTransactionAbort0.restype = ctypes.c_uint32
        self.fwpuclnt.FwpmProviderAdd0.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_FWPM_PROVIDER0),
            ctypes.c_void_p,
        )
        self.fwpuclnt.FwpmProviderAdd0.restype = ctypes.c_uint32
        self.fwpuclnt.FwpmSubLayerAdd0.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_FWPM_SUBLAYER0),
            ctypes.c_void_p,
        )
        self.fwpuclnt.FwpmSubLayerAdd0.restype = ctypes.c_uint32
        self.fwpuclnt.FwpmFilterAdd0.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_FWPM_FILTER0),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
        )
        self.fwpuclnt.FwpmFilterAdd0.restype = ctypes.c_uint32
        self.fwpuclnt.FwpmFilterDeleteByKey0.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_GUID),
        )
        self.fwpuclnt.FwpmFilterDeleteByKey0.restype = ctypes.c_uint32

        self.advapi32.ConvertStringSidToSidW.argtypes = (
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self.advapi32.ConvertStringSidToSidW.restype = ctypes.c_int
        self.advapi32.BuildSecurityDescriptorW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_EXPLICIT_ACCESS_W),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
        )
        self.advapi32.BuildSecurityDescriptorW.restype = ctypes.c_uint32
        self.kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        self.kernel32.LocalFree.restype = ctypes.c_void_p

    def open_engine(self) -> ctypes.c_void_p:
        session = _FWPM_SESSION0()
        session.displayData = _FWPM_DISPLAY_DATA0(_SESSION_NAME, None)
        session.txnWaitTimeoutInMSec = _INFINITE
        handle = ctypes.c_void_p()
        result = self.fwpuclnt.FwpmEngineOpen0(
            None,
            _RPC_C_AUTHN_DEFAULT,
            None,
            ctypes.byref(session),
            ctypes.byref(handle),
        )
        self.ensure_success(result, "FwpmEngineOpen0")
        return handle

    def close_engine(self, engine: ctypes.c_void_p) -> None:
        if engine:
            self.fwpuclnt.FwpmEngineClose0(engine)

    def begin_transaction(self, engine: ctypes.c_void_p) -> None:
        self.ensure_success(
            self.fwpuclnt.FwpmTransactionBegin0(engine, 0),
            "FwpmTransactionBegin0",
        )

    def commit_transaction(self, engine: ctypes.c_void_p) -> None:
        self.ensure_success(
            self.fwpuclnt.FwpmTransactionCommit0(engine),
            "FwpmTransactionCommit0",
        )

    def abort_transaction(self, engine: ctypes.c_void_p) -> None:
        self.fwpuclnt.FwpmTransactionAbort0(engine)

    def ensure_provider(self, engine: ctypes.c_void_p) -> None:
        provider = _FWPM_PROVIDER0(
            _guid(_PROVIDER_KEY),
            _FWPM_DISPLAY_DATA0(_PROVIDER_NAME, _PROVIDER_DESCRIPTION),
            _FWPM_PROVIDER_FLAG_PERSISTENT,
            _empty_blob(),
            None,
        )
        self.ensure_success(
            self.fwpuclnt.FwpmProviderAdd0(engine, ctypes.byref(provider), None),
            "FwpmProviderAdd0",
            allowed=(_FWP_E_ALREADY_EXISTS,),
        )

    def ensure_sublayer(self, engine: ctypes.c_void_p) -> None:
        provider_key = _guid(_PROVIDER_KEY)
        sublayer = _FWPM_SUBLAYER0(
            _guid(_SUBLAYER_KEY),
            _FWPM_DISPLAY_DATA0(_SUBLAYER_NAME, _SUBLAYER_DESCRIPTION),
            _FWPM_SUBLAYER_FLAG_PERSISTENT,
            ctypes.pointer(provider_key),
            _empty_blob(),
            0x8000,
        )
        self.ensure_success(
            self.fwpuclnt.FwpmSubLayerAdd0(engine, ctypes.byref(sublayer), None),
            "FwpmSubLayerAdd0",
            allowed=(_FWP_E_ALREADY_EXISTS,),
        )

    def delete_filter_if_present(self, engine: ctypes.c_void_p, key: _GUID) -> None:
        self.ensure_success(
            self.fwpuclnt.FwpmFilterDeleteByKey0(engine, ctypes.byref(key)),
            "FwpmFilterDeleteByKey0",
            allowed=(_FWP_E_FILTER_NOT_FOUND, _FWP_E_NOT_FOUND),
        )

    def add_filter(
        self,
        engine: ctypes.c_void_p,
        spec: WfpFilterSpec,
        user_condition: _UserMatchCondition,
    ) -> None:
        provider_key = _guid(_PROVIDER_KEY)
        conditions = _build_conditions(spec, user_condition)
        filter_id = ctypes.c_uint64(0)
        filter_ = _FWPM_FILTER0()
        filter_.filterKey = _guid(spec.key)
        filter_.displayData = _FWPM_DISPLAY_DATA0(spec.name, _filter_description(spec))
        filter_.flags = _FWPM_FILTER_FLAG_PERSISTENT
        filter_.providerKey = ctypes.pointer(provider_key)
        filter_.providerData = _empty_blob()
        filter_.layerKey = _guid(_LAYER_KEYS[spec.layer])
        filter_.subLayerKey = _guid(_SUBLAYER_KEY)
        filter_.weight = _empty_value()
        filter_.numFilterConditions = len(conditions)
        filter_.filterCondition = ctypes.cast(
            conditions,
            ctypes.POINTER(_FWPM_FILTER_CONDITION0),
        )
        filter_.action = _FWPM_ACTION0(_FWP_ACTION_BLOCK, _FWPM_ACTION0_0())
        filter_.Anonymous = _FWPM_FILTER0_0()
        filter_.effectiveWeight = _empty_value()
        self.ensure_success(
            self.fwpuclnt.FwpmFilterAdd0(
                engine,
                ctypes.byref(filter_),
                None,
                ctypes.byref(filter_id),
            ),
            f"FwpmFilterAdd0({spec.name})",
        )

    def ensure_success(
        self,
        result: int,
        operation: str,
        *,
        allowed: tuple[int, ...] = (),
    ) -> None:
        if result == _ERROR_SUCCESS or result in allowed:
            return
        raise SandboxBackendError(
            f"wfp_filter_install_failed: {operation} failed: 0x{result:08X}"
        )

    def last_error(self, operation: str) -> SandboxBackendError:
        error = ctypes.get_last_error()
        return SandboxBackendError(
            f"wfp_filter_install_failed: {operation} failed: {error}"
        )


__all__ = [
    "WFP_FILTER_SPECS",
    "WfpFilterSpec",
    "install_wfp_filters_for_user",
    "wfp_filter_specs",
]
