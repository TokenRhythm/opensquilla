import os
import ssl
import sys

_DESKTOP_CA_PROBE_ARG = "--_desktop-ca-probe"
_DESKTOP_CA_PROBE_OK = "opensquilla-desktop-ca-store-ok"
_DESKTOP_TOOL_SEARCH_PROBE_ARG = "--_desktop-tool-search-probe"
_DESKTOP_TOOL_SEARCH_PROBE_OK = "opensquilla-desktop-tool-search-ok"
_DESKTOP_DOCUMENT_PROBE_ARG = "--_desktop-document-probe"
_DESKTOP_MCP_PROBE_ARG = "--_desktop-mcp-probe"
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


def _run_desktop_mcp_probe(gateway_url: str) -> int:
    """Use real MCP stdio and the production server bridge to the local Gateway."""
    import asyncio
    import json
    from datetime import timedelta
    from urllib.parse import urlsplit

    target = urlsplit(gateway_url)
    if target.scheme != "ws" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        print("The packaged MCP probe requires a loopback Gateway.", file=sys.stderr)
        return 1

    async def check() -> dict:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        arguments = [] if getattr(sys, "frozen", False) else [os.path.abspath(__file__)]
        arguments.extend(["mcp-server", "run", "--gateway", gateway_url])
        parameters = StdioServerParameters(
            command=sys.executable, args=arguments, env=dict(os.environ),
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(
                read_stream, write_stream, read_timeout_seconds=timedelta(seconds=20),
            ) as session:
                initialized = await session.initialize()
                if (
                    initialized.capabilities.tools is None
                    or initialized.capabilities.resources is None
                ):
                    raise ValueError("MCP server capabilities are incomplete")
                tools = await session.list_tools()
                names = sorted(tool.name for tool in tools.tools)
                expected = sorted([
                    "conversations_list", "session_resolve", "messages_read",
                    "messages_send", "events_wait", "transcript_export",
                ])
                if names != expected:
                    raise ValueError("MCP product tool registration is incomplete")
                result = await session.call_tool("conversations_list", {"limit": 3})
                if result.isError:
                    raise ValueError("MCP Gateway tool call failed")
                payload = result.structuredContent
                if not isinstance(payload, dict):
                    payload = json.loads(result.content[0].text)
                if payload.get("sessions") != []:
                    raise ValueError("The isolated Gateway must start with no sessions")
                resources = await session.list_resources()
                uris = sorted(str(resource.uri) for resource in resources.resources)
                if uris != ["opensquilla://sessions"]:
                    raise ValueError("MCP session resource registration is incomplete")
                resource = await session.read_resource("opensquilla://sessions")
                if json.loads(resource.contents[0].text).get("sessions") != []:
                    raise ValueError("MCP resource did not read the isolated Gateway")
                return {"probe": "opensquilla-desktop-mcp", "tools": names,
                        "sessions": 0, "resources": uris}

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
