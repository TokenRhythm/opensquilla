import os
import ssl
import sys

_DESKTOP_CA_PROBE_ARG = "--_desktop-ca-probe"
_DESKTOP_CA_PROBE_OK = "opensquilla-desktop-ca-store-ok"
_DESKTOP_TOOL_SEARCH_PROBE_ARG = "--_desktop-tool-search-probe"
_DESKTOP_TOOL_SEARCH_PROBE_OK = "opensquilla-desktop-tool-search-ok"
_DESKTOP_DOCUMENT_PROBE_ARG = "--_desktop-document-probe"
_DESKTOP_MCP_PROBE_ARG = "--_desktop-mcp-probe"
_DESKTOP_PTY_PROBE_ARG = "--_desktop-pty-probe"
_DESKTOP_PTY_PROBE_TIMEOUT_SECONDS = 10.0
_DESKTOP_PTY_POST_EXIT_GRACE_SECONDS = 1.0
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


def _run_desktop_tool_search_probe() -> int:
    """Exercise frozen tool-search code and its dynamically loaded Unicode data."""
    try:
        from opensquilla.tools.search import tokenize_for_bm25

        tokens = tokenize_for_bm25("文件 résumé Straße")
        if tokens != ("wenjian", "resum", "strass"):
            raise ValueError("Unexpected tool-search normalization")
    except Exception:
        print(
            "OpenSquilla Desktop tool search could not load its Unicode resources.",
            file=sys.stderr,
        )
        return 1

    print(_DESKTOP_TOOL_SEARCH_PROBE_OK)
    return 0


def _run_desktop_document_probe(filename: str) -> int:
    """Exercise the same PDF extraction and image decoding used by media tools."""
    import asyncio
    import io
    import json
    from pathlib import Path

    try:
        from PIL import Image

        from opensquilla.contracts.image_validation import validate_image_bytes
        from opensquilla.tools.builtin.media import _render_pdf_first_page_png, pdf

        result = json.loads(asyncio.run(pdf(filename)))
        image_bytes = _render_pdf_first_page_png(Path(filename))
        validate_image_bytes(image_bytes, "image/png")
        with Image.open(io.BytesIO(image_bytes)) as image:
            dimensions = list(image.size)
        if result.get("total_pages") != 1 or not result.get("text"):
            raise ValueError("Unexpected document extraction result")
    except Exception:
        print("Packaged document extraction or image rendering failed.", file=sys.stderr)
        return 1

    print(json.dumps({
        "probe": "opensquilla-desktop-document",
        "pages": result["total_pages"],
        "text": result["text"],
        "imageMime": "image/png",
        "imageSize": dimensions,
    }))
    return 0


def _run_desktop_pty_probe() -> int:
    """Verify that the frozen gateway contains a working platform PTY backend."""
    import asyncio
    import json
    from contextlib import suppress

    from opensquilla.tools.pty_backend import (
        PtyBackendError,
        read_pty,
        spawn_pty,
        terminate_pty,
        wait_pty,
    )

    if os.name == "nt":
        command = (
            "if (-not [Console]::IsInputRedirected -and "
            "-not [Console]::IsOutputRedirected) { "
            "Write-Output 'opensquilla-pty-ok' } else { "
            "Write-Output 'opensquilla-pty-miss' }"
        )
    else:
        command = (
            "if [ -t 0 ] && [ -t 1 ]; then printf 'opensquilla-pty-ok\\n'; "
            "else printf 'opensquilla-pty-miss\\n'; fi"
        )

    async def collect() -> tuple[str, int | None]:
        handle = None
        exited = None
        reading = None
        try:
            handle = spawn_pty(command, cwd=os.getcwd(), env=dict(os.environ))
            async with asyncio.timeout(_DESKTOP_PTY_PROBE_TIMEOUT_SECONDS):
                chunks: list[bytes] = []
                exited = asyncio.create_task(wait_pty(handle))
                while True:
                    reading = asyncio.create_task(read_pty(handle))
                    done, _ = await asyncio.wait(
                        {reading, exited}, return_when=asyncio.FIRST_COMPLETED,
                    )
                    if exited in done and reading not in done:
                        # ConPTY can keep its socket open after the child has
                        # exited.  Give the reader a bounded grace period to
                        # receive buffered tail bytes, then treat quietness as
                        # EOF for this probe.
                        try:
                            chunk = await asyncio.wait_for(
                                reading, timeout=_DESKTOP_PTY_POST_EXIT_GRACE_SECONDS,
                            )
                        except TimeoutError:
                            reading.cancel()
                            with suppress(asyncio.CancelledError):
                                await reading
                            break
                    else:
                        try:
                            chunk = reading.result()
                        except EOFError:
                            break
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace"), await exited
        except PtyBackendError as exc:
            handle = exc.handle or handle
            raise
        finally:
            if reading is not None and not reading.done():
                reading.cancel()
                with suppress(asyncio.CancelledError):
                    await reading
            if exited is not None and not exited.done():
                exited.cancel()
                with suppress(asyncio.CancelledError):
                    await exited
            if handle is not None:
                # Stop the PTY child before asyncio shuts down its blocking
                # reader thread, including a failed spawn that returned a handle.
                with suppress(Exception):
                    await terminate_pty(handle)
                if exited is None or exited.cancelled() or not exited.done():
                    with suppress(Exception):
                        await asyncio.wait_for(wait_pty(handle), timeout=5.0)

    try:
        output, returncode = asyncio.run(collect())
        is_tty = "opensquilla-pty-ok" in output
        result = {
            "probe": "opensquilla-desktop-pty",
            "available": is_tty and returncode == 0,
            "ioMode": "pty" if is_tty else "pipe",
            "returncode": returncode,
        }
        print(json.dumps(result))
        return 0 if result["available"] else 1
    except PtyBackendError as exc:
        print(json.dumps({
            "probe": "opensquilla-desktop-pty",
            "available": False,
            "ioMode": "unavailable",
            "reason": str(exc),
        }))
        return 1
    except Exception as exc:
        print(json.dumps({
            "probe": "opensquilla-desktop-pty",
            "available": False,
            "ioMode": "error",
            "reason": "PTY probe timed out" if isinstance(exc, TimeoutError) else str(exc),
        }))
        return 1


def _run_desktop_mcp_probe(gateway_url: str) -> int:
    """Use real MCP stdio and the production server bridge to the local Gateway."""
    import asyncio
    import json
    from urllib.parse import urlsplit

    target = urlsplit(gateway_url)
    if target.scheme != "ws" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        print("The packaged MCP probe requires a loopback Gateway.", file=sys.stderr)
        return 1

    async def check() -> dict:
        from mcp import Client, StdioServerParameters
        from mcp_types import LATEST_PROTOCOL_VERSION

        arguments = [] if getattr(sys, "frozen", False) else [os.path.abspath(__file__)]
        arguments.extend(["mcp-server", "run", "--gateway", gateway_url])
        parameters = StdioServerParameters(
            command=sys.executable, args=arguments, env=dict(os.environ),
        )
        async with Client(parameters, mode="auto", read_timeout_seconds=20) as client:
            # Both ends use the bundled SDK and must negotiate its latest protocol.
            if client.protocol_version != LATEST_PROTOCOL_VERSION:
                raise ValueError("MCP probe negotiated an outdated protocol")
            if (
                client.server_capabilities.tools is None
                or client.server_capabilities.resources is None
            ):
                raise ValueError("MCP server capabilities are incomplete")
            tools = await client.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            expected = sorted([
                "conversations_list", "session_resolve", "messages_read",
                "messages_send", "events_wait", "transcript_export",
            ])
            if names != expected:
                raise ValueError("MCP product tool registration is incomplete")
            result = await client.call_tool("conversations_list", {"limit": 3})
            if result.is_error:
                raise ValueError("MCP Gateway tool call failed")
            payload = result.structured_content
            if not isinstance(payload, dict):
                payload = json.loads(result.content[0].text)
            if payload.get("sessions") != []:
                raise ValueError("The isolated Gateway must start with no sessions")
            resources = await client.list_resources()
            uris = sorted(str(resource.uri) for resource in resources.resources)
            if uris != ["opensquilla://sessions"]:
                raise ValueError("MCP session resource registration is incomplete")
            resource = await client.read_resource("opensquilla://sessions")
            if json.loads(resource.contents[0].text).get("sessions") != []:
                raise ValueError("MCP resource did not read the isolated Gateway")
            return {"probe": "opensquilla-desktop-mcp", "tools": names,
                    "sessions": 0, "resources": uris,
                    "protocolVersion": client.protocol_version}

    try:
        result = asyncio.run(check())
    except Exception:
        print("Packaged MCP stdio and Gateway bridge probe failed.", file=sys.stderr)
        return 1
    print(json.dumps(result))
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

    if sys.argv[1:] == [_DESKTOP_TOOL_SEARCH_PROBE_ARG]:
        raise SystemExit(_run_desktop_tool_search_probe())

    if len(sys.argv) == 3 and sys.argv[1] == _DESKTOP_DOCUMENT_PROBE_ARG:
        raise SystemExit(_run_desktop_document_probe(sys.argv[2]))

    if sys.argv[1:] == [_DESKTOP_PTY_PROBE_ARG]:
        raise SystemExit(_run_desktop_pty_probe())

    if len(sys.argv) == 3 and sys.argv[1] == _DESKTOP_MCP_PROBE_ARG:
        raise SystemExit(_run_desktop_mcp_probe(sys.argv[2]))

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
