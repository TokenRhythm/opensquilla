# T0A Generic Skill Execution and Protected Publication

This branch extends the T0 MCP content experiment. It is not a production
gateway rollout or a portable release. The paired Knowledge branch is
`codex/research-skill-t0a-20260928`; business validation remains in that
repository's standard `skills/knowledge-research` package.

## Host-Owned Configuration

The standard Skill format remains `SKILL.md` plus optional scripts/resources.
It requires no OpenSquilla plugin manifest. The trusted host separately:

1. Installs the package in a fresh, independently owned directory and pins its
   digest with `SkillScriptGrant.pin` and specific `scripts/*.py` entry points.
2. Constructs `SkillScriptRunner` with separate workspace/input directories,
   an execution ID, timeout and output limits.
3. Supplies that runner on the runtime-only `ToolContext.skill_script_runner`.
4. Supplies an `ArtifactPublicationPolicy` on the runtime-only
   `ToolContext.artifact_publication_policy` and an explicit tool allowlist.

`run_skill_script` is not exposed by default. Its arguments select only a
granted Skill, granted script and bounded CLI arguments; they cannot select a
shell or interpreter. The Linux runner uses bubblewrap and a fixed isolated
system Python, a cleared environment, read-only package/inputs, writable task
output, no network, process/memory/time/output limits and process-group cleanup.
Validator runs additionally have a read-only workspace. It requires trusted
independent roots: path containment checks alone cannot rule out hardlinks, and
there is not a total output-disk quota.

## Publication Gate

Protected `publish_artifact` calls require `bundle=none` and an execution ID.
The host reads a bounded regular file without following symlinks, freezes its
bytes, and requests policy authorization. Authorization binds the candidate
hash to the exact session and execution. The artifact store receives these same
bytes, including the deduplication path. It never rereads the source after
authorization. Mutable workspace paths are not returned as publication links.
Automatic publication based on a final-answer local path is disabled in this
context. Existing unprotected contexts keep their previous behavior.

`SkillArtifactPublicationPolicy` is one generic implementation. It runs a
host-selected installed validator with fixed arguments and checks its
`skill-publication-check/1` result against original input hashes, the requested
file name/media type, and candidate hash. A private receipt binds the package,
entry point, arguments, session/execution, input hashes and outputs. The model
cannot choose the validator or submit an authorization flag.

This is **not a global interceptor of every artifact producer**. A protected
product profile must deny unrelated create/media tools that publish directly.
The paired acceptance driver exposes only five read-only MCP tools, Skill
list/view, workspace `write_file`, the granted script runner and gated
`publish_artifact`. General production profiles have not been migrated.

## MCP Media and Truthful Records

`ToolOutput` carries text, native MCP blocks and structured content through the
normal dispatcher into `ToolResult`. The Agent projects validated inline
PNG/JPEG images into an adjacent user media message, linked to the tool call;
it does not fetch resource URLs. Limits apply to image bytes/pixels, block count
and the per-turn image budget. The model must explicitly support vision. False
or unknown support produces a visible omission record rather than an invalid
image request. Error, unsupported and oversized blocks remain distinguishable.

Preparation does not mean transmission. Real acceptance requires the finalized
provider payload, matching image hashes and a corresponding successful response.
Even those records do not attest model understanding. The first real DeepSeek
attempt exposed a missing capability check and was rejected with HTTP 400;
the subsequent fix is covered with actual adapter mock transports, not another
real-model visual success.

MCP initialization now closes its client on cancellation as well as ordinary
exceptions. The paired driver applies a separate discovery timeout and observes
actual client responses; dispatcher rejections are not promoted into MCP receipts.

## Verification and Remaining Work

Focused tests cover dispatch preservation, three provider wire formats,
image omissions, script isolation and resource bounds, publication tampering,
session/hash mismatch, deduplication, mutable-file replacement and auto-publish
bypass attempts. The paired suite runs the real Agent and Knowledge server with
an offline scripted provider, not an LLM.

The combined 1,050-test regression run produced 1,049 passes and one existing
price-catalog expectation failure in
`test_with_model_usage_cost_fields_prices_unbilled_cache_reads_cache_aware`.
The same failure was reproduced on the unmodified T0 base `309e48dc`.
Final incremental counts and exact source identities are in the
[cross-repository acceptance record](https://github.com/chengsiyangbuaa/drawio-project/blob/codex/rag-baseline-20260928/projects/rag/t0a-agent-acceptance.md).

Still pending: real vision-model acceptance, production session/profile wiring,
recovery/persistence, full business Skill migration, full Knowledge MCP surface,
and another-server installation. No production service or data was changed.
