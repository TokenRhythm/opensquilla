"""Windows Firewall rule planning for windows_default managed networking."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from opensquilla.sandbox.backend.windows_default_network import (
    blocked_loopback_tcp_remote_ports,
)
from opensquilla.sandbox.types import SandboxBackendError

LOOPBACK_REMOTE_ADDRESSES = "127.0.0.0/8,::/127"
NON_LOOPBACK_REMOTE_ADDRESSES = (
    "0.0.0.0-126.255.255.255,"
    "128.0.0.0-255.255.255.255,"
    "::,::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"
)


@dataclass(frozen=True)
class FirewallRuleSpec:
    name: str
    display_name: str
    protocol: str
    local_user_sid: str
    remote_addresses: str
    remote_ports: tuple[str, ...] = ()
    icmp_types: tuple[str, ...] = ()


def firewall_rule_specs(
    *,
    offline_sid: str,
    allowed_proxy_ports: tuple[int, ...],
    allow_local_binding: bool,
) -> tuple[FirewallRuleSpec, ...]:
    specs = [
        FirewallRuleSpec(
            name="opensquilla_sandbox_offline_block_non_loopback",
            display_name="OpenSquilla Sandbox Offline - Block Non-Loopback Outbound",
            protocol="Any",
            local_user_sid=offline_sid,
            remote_addresses=NON_LOOPBACK_REMOTE_ADDRESSES,
        )
    ]
    specs.extend(
        (
            FirewallRuleSpec(
                name="opensquilla_sandbox_offline_block_non_loopback_icmp_v4",
                display_name="OpenSquilla Sandbox Offline - Block Non-Loopback ICMPv4",
                protocol="ICMPv4",
                local_user_sid=offline_sid,
                remote_addresses=NON_LOOPBACK_REMOTE_ADDRESSES,
                icmp_types=("Any",),
            ),
            FirewallRuleSpec(
                name="opensquilla_sandbox_offline_block_non_loopback_icmp_v6",
                display_name="OpenSquilla Sandbox Offline - Block Non-Loopback ICMPv6",
                protocol="ICMPv6",
                local_user_sid=offline_sid,
                remote_addresses=NON_LOOPBACK_REMOTE_ADDRESSES,
                icmp_types=("Any",),
            ),
        )
    )
    specs.append(
        FirewallRuleSpec(
            name="opensquilla_sandbox_offline_block_dns_tcp",
            display_name="OpenSquilla Sandbox Offline - Block DNS TCP",
            protocol="TCP",
            local_user_sid=offline_sid,
            remote_addresses=NON_LOOPBACK_REMOTE_ADDRESSES,
            remote_ports=("53", "853"),
        )
    )
    specs.append(
        FirewallRuleSpec(
            name="opensquilla_sandbox_offline_block_dns_udp",
            display_name="OpenSquilla Sandbox Offline - Block DNS UDP",
            protocol="UDP",
            local_user_sid=offline_sid,
            remote_addresses=NON_LOOPBACK_REMOTE_ADDRESSES,
            remote_ports=("53", "853"),
        )
    )
    if allow_local_binding:
        return tuple(specs)
    specs.append(
        FirewallRuleSpec(
            name="opensquilla_sandbox_offline_block_loopback_icmp_v4",
            display_name="OpenSquilla Sandbox Offline - Block Loopback ICMPv4",
            protocol="ICMPv4",
            local_user_sid=offline_sid,
            remote_addresses=LOOPBACK_REMOTE_ADDRESSES,
            icmp_types=("Any",),
        )
    )
    specs.append(
        FirewallRuleSpec(
            name="opensquilla_sandbox_offline_block_loopback_icmp_v6",
            display_name="OpenSquilla Sandbox Offline - Block Loopback ICMPv6",
            protocol="ICMPv6",
            local_user_sid=offline_sid,
            remote_addresses=LOOPBACK_REMOTE_ADDRESSES,
            icmp_types=("Any",),
        )
    )
    specs.append(
        FirewallRuleSpec(
            name="opensquilla_sandbox_offline_block_loopback_udp",
            display_name="OpenSquilla Sandbox Offline - Block Loopback UDP",
            protocol="UDP",
            local_user_sid=offline_sid,
            remote_addresses=LOOPBACK_REMOTE_ADDRESSES,
        )
    )
    specs.append(
        FirewallRuleSpec(
            name="opensquilla_sandbox_offline_block_loopback_tcp_except_proxy",
            display_name="OpenSquilla Sandbox Offline - Block Loopback TCP Except Proxy",
            protocol="TCP",
            local_user_sid=offline_sid,
            remote_addresses=LOOPBACK_REMOTE_ADDRESSES,
            remote_ports=blocked_loopback_tcp_remote_ports(allowed_proxy_ports),
        )
    )
    return tuple(specs)


def powershell_firewall_commands(specs: tuple[FirewallRuleSpec, ...]) -> tuple[str, ...]:
    commands: list[str] = []
    for spec in specs:
        port_clause = _powershell_firewall_port_clause(spec)
        remote_addresses = _powershell_array_literal(spec.remote_addresses.split(","))
        local_user = _local_user_authorized_list(spec.local_user_sid)
        commands.append(
            f"if (Get-NetFirewallRule -Name '{spec.name}' -ErrorAction SilentlyContinue) "
            f"{{ Remove-NetFirewallRule -Name '{spec.name}' }}; "
            f"New-NetFirewallRule -Name '{spec.name}' -DisplayName '{spec.display_name}' "
            "-Direction Outbound -Action Block -Enabled True "
            f"-Profile Any -Protocol {spec.protocol} -RemoteAddress {remote_addresses} "
            f"{port_clause} -LocalUser '{local_user}'"
        )
    return tuple(commands)


def _powershell_firewall_port_clause(spec: FirewallRuleSpec) -> str:
    if _is_icmp_protocol(spec.protocol):
        icmp_types = _powershell_array_literal(list(spec.icmp_types or ("Any",)))
        return f"-IcmpType {icmp_types}"
    remote_ports = (
        _powershell_array_literal(list(spec.remote_ports))
        if spec.remote_ports
        else _powershell_single_quote("Any")
    )
    return f"-RemotePort {remote_ports}"


def _is_icmp_protocol(protocol: str) -> bool:
    return protocol.upper() in {"ICMPV4", "ICMPV6"}


def _powershell_array_literal(values: list[str]) -> str:
    return ",".join(_powershell_single_quote(value.strip()) for value in values if value.strip())


def _powershell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _local_user_authorized_list(sid: str) -> str:
    return f"D:(A;;CC;;;{sid})"


def install_firewall_rules(specs: tuple[FirewallRuleSpec, ...]) -> None:
    script = "$ErrorActionPreference = 'Stop'; " + "; ".join(
        powershell_firewall_commands(specs)
    )
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SandboxBackendError(f"firewall_rule_install_failed: {detail}")


__all__ = [
    "LOOPBACK_REMOTE_ADDRESSES",
    "NON_LOOPBACK_REMOTE_ADDRESSES",
    "FirewallRuleSpec",
    "firewall_rule_specs",
    "install_firewall_rules",
    "powershell_firewall_commands",
]
