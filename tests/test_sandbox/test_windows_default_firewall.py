from __future__ import annotations

from types import SimpleNamespace

import opensquilla.sandbox.backend.windows_default_firewall as firewall_mod
from opensquilla.sandbox.backend.windows_default_firewall import (
    LOOPBACK_REMOTE_ADDRESSES,
    NON_LOOPBACK_REMOTE_ADDRESSES,
    firewall_rule_specs,
    powershell_firewall_commands,
)


def test_firewall_rule_specs_scope_to_offline_user_sid() -> None:
    specs = firewall_rule_specs(
        offline_sid="S-1-5-21-100-200-300-400",
        allowed_proxy_ports=(43128,),
        allow_local_binding=False,
    )

    assert {spec.name for spec in specs} == {
        "opensquilla_sandbox_offline_block_non_loopback",
        "opensquilla_sandbox_offline_block_non_loopback_icmp_v4",
        "opensquilla_sandbox_offline_block_non_loopback_icmp_v6",
        "opensquilla_sandbox_offline_block_dns_tcp",
        "opensquilla_sandbox_offline_block_dns_udp",
        "opensquilla_sandbox_offline_block_loopback_icmp_v4",
        "opensquilla_sandbox_offline_block_loopback_icmp_v6",
        "opensquilla_sandbox_offline_block_loopback_udp",
        "opensquilla_sandbox_offline_block_loopback_tcp_except_proxy",
    }
    assert all(spec.local_user_sid == "S-1-5-21-100-200-300-400" for spec in specs)
    assert specs[0].remote_addresses == NON_LOOPBACK_REMOTE_ADDRESSES
    dns_specs = {spec.name: spec for spec in specs if "block_dns" in spec.name}
    assert dns_specs["opensquilla_sandbox_offline_block_dns_tcp"].protocol == "TCP"
    assert dns_specs["opensquilla_sandbox_offline_block_dns_tcp"].remote_ports == (
        "53",
        "853",
    )
    assert dns_specs["opensquilla_sandbox_offline_block_dns_udp"].protocol == "UDP"
    assert dns_specs["opensquilla_sandbox_offline_block_dns_udp"].remote_ports == (
        "53",
        "853",
    )
    loopback_tcp = next(
        spec
        for spec in specs
        if spec.name == "opensquilla_sandbox_offline_block_loopback_tcp_except_proxy"
    )
    assert loopback_tcp.remote_ports == ("1-43127", "43129-65535")
    icmp_specs = {spec.name: spec for spec in specs if spec.protocol.upper().startswith("ICMP")}
    assert icmp_specs["opensquilla_sandbox_offline_block_non_loopback_icmp_v4"].protocol == (
        "ICMPv4"
    )
    assert icmp_specs["opensquilla_sandbox_offline_block_non_loopback_icmp_v6"].protocol == (
        "ICMPv6"
    )
    assert all(spec.icmp_types == ("Any",) for spec in icmp_specs.values())


def test_firewall_rule_specs_allow_local_binding_removes_loopback_blocks() -> None:
    specs = firewall_rule_specs(
        offline_sid="S-1-5-21-100-200-300-400",
        allowed_proxy_ports=(43128,),
        allow_local_binding=True,
    )

    assert [spec.name for spec in specs] == [
        "opensquilla_sandbox_offline_block_non_loopback",
        "opensquilla_sandbox_offline_block_non_loopback_icmp_v4",
        "opensquilla_sandbox_offline_block_non_loopback_icmp_v6",
        "opensquilla_sandbox_offline_block_dns_tcp",
        "opensquilla_sandbox_offline_block_dns_udp",
    ]


def test_powershell_firewall_commands_include_local_user_scope() -> None:
    specs = firewall_rule_specs(
        offline_sid="S-1-5-21-100-200-300-400",
        allowed_proxy_ports=(43128,),
        allow_local_binding=False,
    )
    commands = powershell_firewall_commands(specs)
    joined = "\n".join(commands)

    assert "-LocalUser 'D:(A;;CC;;;S-1-5-21-100-200-300-400)'" in joined
    assert "-LocalUser 'S-1-5-21-100-200-300-400'" not in joined
    assert "O:LSD:" not in joined
    assert "-Direction Outbound" in joined
    assert "-Action Block" in joined
    assert "43129-65535" in joined


def test_powershell_firewall_commands_pass_remote_addresses_as_arrays() -> None:
    specs = firewall_rule_specs(
        offline_sid="S-1-5-21-100-200-300-400",
        allowed_proxy_ports=(43128,),
        allow_local_binding=False,
    )
    commands = powershell_firewall_commands(specs)
    joined = "\n".join(commands)

    assert "-RemoteAddress '127.0.0.0/8','::/127'" in joined
    assert "-RemoteAddress '127.0.0.0/8,::/127'" not in joined


def test_powershell_firewall_commands_pass_remote_ports_as_arrays() -> None:
    specs = firewall_rule_specs(
        offline_sid="S-1-5-21-100-200-300-400",
        allowed_proxy_ports=(48123,),
        allow_local_binding=False,
    )
    commands = powershell_firewall_commands(specs)
    joined = "\n".join(commands)

    assert "-RemotePort '1-48122','48124-65535'" in joined
    assert "-RemotePort '1-48122,48124-65535'" not in joined


def test_powershell_firewall_commands_use_icmp_type_without_remote_port() -> None:
    specs = firewall_rule_specs(
        offline_sid="S-1-5-21-100-200-300-400",
        allowed_proxy_ports=(48123,),
        allow_local_binding=False,
    )
    commands = powershell_firewall_commands(specs)
    icmp_commands = [command for command in commands if "_icmp_" in command]

    assert icmp_commands
    assert all("-IcmpType 'Any'" in command for command in icmp_commands)
    assert all("-RemotePort" not in command for command in icmp_commands)


def test_loopback_remote_addresses_use_firewall_accepted_ipv6_range() -> None:
    assert LOOPBACK_REMOTE_ADDRESSES == "127.0.0.0/8,::/127"


def test_install_firewall_rules_batches_all_rules_into_one_powershell_call(
    monkeypatch,
) -> None:
    specs = firewall_rule_specs(
        offline_sid="S-1-5-21-100-200-300-400",
        allowed_proxy_ports=(43128,),
        allow_local_binding=False,
    )
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(firewall_mod.subprocess, "run", fake_run)

    firewall_mod.install_firewall_rules(specs)

    assert len(calls) == 1
    script = calls[0][0][0][5]
    assert script.startswith("$ErrorActionPreference = 'Stop'; ")
    assert all(spec.name in script for spec in specs)
