# Contract toolchains

Install the WebUI dependencies with `npm --prefix opensquilla-webui ci`, then run
`python scripts/contracts/prepare_codegen_toolchains.py` from the repository root.
The preparation command uses `uv.lock` for two separate Python environments:

| Output | Python generator | TypeScript generator |
| --- | --- | --- |
| Ordinary v4 types | datamodel-code-generator 0.81.0 in `.venv` | json-schema-to-typescript 16.0.0 |
| Frozen `sessions.list` types | datamodel-code-generator 0.81.0 in `.venv-contract-legacy` | json-schema-to-typescript 15.0.4 through the `json-schema-to-typescript-legacy` npm alias |

Both production and verification runtime validators use AJV 8.20.0. The original
`sessions.list` schema metadata records its original Python 0.75.1 toolchain and
stays byte-identical. The active Python generator uses the patched 0.81.0 release;
its output was verified against the existing frozen type-body hashes. The
compatibility manifest records the active type and validator toolchains separately
from that historical schema metadata. The generator verifies installed versions
before rendering. Toolchain changes must preserve the frozen schema and type-body
hashes; do not update those hashes to make an upgrade pass. Generator provenance
headers may change to describe the actual generation code.

Run `uv run --no-sync python scripts/contracts/generate_gateway_contracts.py
--write-determinism --jobs 4` to regenerate, or replace `--write-determinism` with
`--check-determinism` to verify. Use `--no-sync` after preparation so that the
ordinary development environment remains separate from the legacy environment.
The integration tests in `tests/contracts` also require
`OPENSQUILLA_RUN_CONTRACT_TOOLCHAIN_INTEGRATION=1` and the repository root plus
`src` on `PYTHONPATH`.

The TypeScript 7 CLI builds the desktop shell and checks WebUI build configuration.
Vue SFC checking and scripts that consume the compiler API explicitly use the
official `@typescript/typescript6` compatibility package. `npm run typecheck` in
`opensquilla-webui` runs the architecture gates, the native CLI, and Vue SFC
checking; it prints the resolved compatibility compiler version and path. This
is a dual toolchain until Vue tooling supports the native compiler directly.
