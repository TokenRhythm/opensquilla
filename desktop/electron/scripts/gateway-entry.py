import os
import ssl
import sys

_DESKTOP_CA_PROBE_ARG = "--_desktop-ca-probe"
_DESKTOP_CA_PROBE_OK = "opensquilla-desktop-ca-store-ok"
_SANDBOX_FILESYSTEM_WORKER_ARG = "--_sandbox-filesystem-worker"
_INTERNAL_CHILD_ARG = "--internal-child"


def _top_level_command_index(argv: list[str]) -> int | None:
    """Find the first positional command without importing the CLI package."""

    index = 1
    while index < len(argv):
        value = argv[index]
        if value == "--":
            return index + 1 if index + 1 < len(argv) else None
        if value == "--profile":
            index += 2
            continue
        if value.startswith("--profile=") or value.startswith("-"):
            index += 1
            continue
        return index
    return None


def _is_recovery_invocation(argv: list[str]) -> bool:
    index = _top_level_command_index(argv)
    return index is not None and argv[index] == "recovery"


def _run_desktop_ca_probe() -> int:
    try:
        context = ssl.create_default_context()
        ca_certificate_count = len(context.get_ca_certs(binary_form=True))
    except Exception:
        ca_certificate_count = 0

    if ca_certificate_count <= 0:
        print(
            "OpenSquilla Desktop TLS trust probe found no trusted CA certificates.",
            file=sys.stderr,
        )
        return 1

    print(f"{_DESKTOP_CA_PROBE_OK} x509_ca={ca_certificate_count}")
    return 0

if __name__ == "__main__":
    if _is_recovery_invocation(sys.argv):
        # Set this before importing *any* recovery module.  The lightweight
        # dispatcher deliberately bypasses opensquilla.cli.main and dotenv.
        os.environ["OPENSQUILLA_RECOVERY_OFFLINE"] = "1"
        from opensquilla.cli.recovery_entry import app

        app()
        raise SystemExit(0)

    if sys.argv[1:] == [_DESKTOP_CA_PROBE_ARG]:
        raise SystemExit(_run_desktop_ca_probe())

    if sys.argv[1:] == [_SANDBOX_FILESYSTEM_WORKER_ARG]:
        from opensquilla.sandbox.runtime_launcher import dispatch_internal_child

        raise SystemExit(dispatch_internal_child(["filesystem-worker", "-"]))

    if len(sys.argv) >= 3 and sys.argv[1] == _INTERNAL_CHILD_ARG:
        from opensquilla.sandbox.runtime_launcher import dispatch_internal_child

        raise SystemExit(dispatch_internal_child(sys.argv[2:]))

    if len(sys.argv) == 3 and sys.argv[1] == "--elevated-helper":
        from opensquilla.sandbox.backend.windows_default_setup import (
            elevated_setup_helper_main,
        )

        raise SystemExit(elevated_setup_helper_main(sys.argv[1:]))

    from opensquilla.cli.main import app

    app()
