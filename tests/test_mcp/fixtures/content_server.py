"""Offline stdio fixture exercising large tool content and malformed frames."""

from __future__ import annotations

import json
import sys
from typing import Any

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6bS8AAAAASUVORK5CYII="


def main() -> None:
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        if message["method"] == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "content-fixture", "version": "1"},
            }
        else:
            arguments = message["params"]["arguments"]
            size = arguments.get("size", 0)
            if arguments.get("unterminated"):
                sys.stdout.write("x" * size)
                sys.stdout.flush()
                continue
            result = {
                "content": [
                    {"type": "text", "text": "x" * size},
                    {"type": "image", "data": PNG, "mimeType": "image/png"},
                    {"type": "resource", "resource": {"uri": "sample:///text", "text": "body"}},
                ],
                "structuredContent": {"size": size},
            }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
