"""Minimal spawn target: do not import the recovery test's CLI dependency tree."""

from __future__ import annotations

from multiprocessing.connection import Connection


def contend_for_gateway(connection: Connection) -> None:
    from opensquilla.gateway.pidlock import GatewayPidLock

    try:
        connection.send("ready")
        lock = GatewayPidLock(connection.recv())
        try:
            lock.acquire()
        except SystemExit:
            connection.send("busy")
        else:
            try:
                connection.send("acquired")
            finally:
                lock.release()
    finally:
        connection.close()
