"""Opt-in live usage matrix controller; credentials remain in process memory.

Use from an interactive terminal. The first (non-echoed) input is a JSON object
with tokenrhythm/openrouter keys. Subsequent inputs are JSON commands. Only
official endpoints are used; the child receives secrets over stdin, never argv.
"""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-usd", type=str, required=True)
    args = parser.parse_args()
    keys = json.loads(getpass.getpass("Provider credentials (JSON, hidden): "))
    if set(keys) != {"tokenrhythm", "openrouter"}:
        raise SystemExit("Expected exactly two provider credentials")
    print("CREDENTIALS_READY", flush=True)
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        command = json.loads(line)
        if command.get("action") == "quit":
            break
        child = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("live_usage_matrix.py")),
                "--output",
                str(args.output),
                "--budget-usd",
                args.budget_usd,
            ],
            input=json.dumps({"keys": keys, "command": command}),
            text=True,
            # Matrix output is deliberately bounded, metadata-only JSON.
            check=False,
        )
        print(json.dumps({"workerExitCode": child.returncode}), flush=True)
    keys.clear()


if __name__ == "__main__":
    main()
