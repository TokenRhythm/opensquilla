"""Goal authority observes disconnects without changing ordinary transport cleanup."""

from types import SimpleNamespace

from structlog.testing import capture_logs

from opensquilla.gateway.auth import resolve_auth
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.websocket import ConnectionRegistry, get_registry
from tests.test_gateway.test_goal_rpc import _goal_connection, _open_goal_rpc_stack


def test_unregister_observes_final_connection_before_cleanup_and_isolates_failure():
    registry = ConnectionRegistry()
    connection = _goal_connection("synthetic-observer")
    observed = []

    def broken_observer(current):
        assert registry.get(current.conn_id) is current
        observed.append(current.principal)
        raise RuntimeError("Synthetic observer failure")

    registry.set_unregister_listener(broken_observer)
    registry.register(connection)
    with capture_logs() as logs:
        registry.unregister(connection.conn_id)
    assert observed == [connection.principal]
    assert registry.get(connection.conn_id) is None
    assert [entry["event"] for entry in logs] == ["gateway.ws_unregister_listener_failed"]
    registry.unregister(connection.conn_id)
    assert len(observed) == 1


async def test_ordinary_connection_disconnect_never_reads_goal_token_authority(tmp_path):
    async with _open_goal_rpc_stack(tmp_path / "ordinary.sqlite") as stack:
        queries = []

        def unexpected_read(_public_id):
            queries.append(_public_id)
            raise AssertionError("Ordinary disconnect must not query named tokens")

        stack.service._authority_token_store = (
            str(stack.context.config.state_dir),
            SimpleNamespace(get_active_authorization=unexpected_read),
        )
        get_registry().unregister(stack.context.conn_id)
        assert get_registry().get(stack.context.conn_id) is None
        assert queries == []
        assert not stack.service._continuity_grants
        await stack.service.prepare_shutdown()
        assert get_registry()._unregister_listener is None


def test_old_service_cannot_remove_a_newer_unregister_listener():
    registry = ConnectionRegistry()

    def old_listener(_connection):
        pass

    def new_listener(_connection):
        pass

    registry.set_unregister_listener(old_listener)
    registry.set_unregister_listener(new_listener)
    registry.clear_unregister_listener(old_listener)
    assert registry._unregister_listener is new_listener
    registry.clear_unregister_listener(new_listener)
    assert registry._unregister_listener is None


async def test_open_remote_guest_does_not_gain_background_goal_authority(tmp_path):
    principal = resolve_auth(
        GatewayConfig(host="0.0.0.0", auth={"mode": "none"}),
        auth_params={}, role_claim="operator", peer_ip="192.168.1.7",
    )
    assert principal is not None
    assert principal.authenticated is False and principal.auth_state == "guest"
    assert principal.is_owner is False
    async with _open_goal_rpc_stack(tmp_path / "guest.sqlite") as stack:
        assert stack.service._principal_authority_current(principal) is False
        assert not stack.service._continuity_grants
