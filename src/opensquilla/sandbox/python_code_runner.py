"""Execute a code snippet in an already isolated, owned Gateway child process.

This entry point supplies Python code execution for frozen Gateways, whose public
CLI is not a Python interpreter. Approval, environment filtering, sandboxing and
process lifetime remain the caller's responsibility, just as for ``python -c``.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Sequence


def main(args: Sequence[str]) -> int:
    """Run exactly one code operand with a fresh ``__main__`` namespace."""
    if len(args) != 1:
        print("python-code requires exactly one code argument", file=sys.stderr)
        return 2

    code = compile(args[0], "<string>", "exec", dont_inherit=True)
    module = types.ModuleType("__main__")
    sys.modules["__main__"] = module
    sys.argv = ["-c"]
    if not sys.flags.safe_path:
        sys.path.insert(0, "")
    exec(code, module.__dict__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
