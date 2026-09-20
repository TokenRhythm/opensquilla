"""Synthetic legacy peer for subprocess transport and negotiation regressions."""

import json
import os
import sys
import time

mode = sys.argv[1]
pending = None


def send(payload):
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
    sys.stdout.buffer.flush()


for line in sys.stdin.buffer:
    request = json.loads(line)
    request_id = request.get("id")
    if request_id is None:
        continue
    method = request["method"]
    if mode == "hang":
        time.sleep(60)
    if method == "server/discover":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "synthetic", "version": "1"},
        }
        if mode == "large":
            result["instructions"] = "x" * (129 * 1024)
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo",
                    "description": "x" * (129 * 1024 if mode == "large" else 1),
                    "inputSchema": {"type": "object"},
                }
            ]
        }
    elif method == "tools/call":
        arguments = request["params"].get("arguments", {})
        if mode == "exit":
            sys.exit(0)
        if mode == "hang_call":
            time.sleep(60)
        if mode == "oversize":
            sys.stdout.buffer.write(b"x" * (18 * 1024 * 1024))
            sys.stdout.buffer.flush()
            time.sleep(60)
        if mode == "environment":
            text = json.dumps(
                {
                    name: os.environ[name]
                    for name in ("OPENSQUILLA_MCP_TEST_INHERITED", "OPENSQUILLA_MCP_TEST_OVERRIDE")
                }
            )
        elif mode == "error":
            text = "Error: upstream API rejected the query"
        elif mode == "large":
            text = "🦑" * (256 * 1024)
        else:
            text = arguments.get("text", "ok")
        result = {"content": [{"type": "text", "text": text}], "isError": mode == "error"}
        if mode == "reorder":
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            if pending is None:
                pending = response
                continue
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/message",
                    "params": {"level": "info", "data": "synthetic notification"},
                }
            )
            send(response)
            send(pending)
            pending = None
            continue
    else:
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )
        continue
    if mode == "noisy":
        sys.stdout.buffer.write(b"\n\xff\nnot JSON\n")
    send({"jsonrpc": "2.0", "id": request_id, "result": result})
