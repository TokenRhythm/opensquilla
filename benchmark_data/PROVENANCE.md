# Benchmark inputs

`sources.json` records the public repositories, immutable revisions, selected
files and content fingerprints needed for these 20 tasks. Preparation downloads
only those inputs; it does not install AgentSkillOS's Agent implementation.
The task order follows the frozen experiment task order.

`plans/` preserves the frozen MetaSkill node objectives, designated Skills,
dependencies, inputs, outputs and access flags. Historical authoring/audit
metadata has been omitted. Rendering keeps the original execution semantics:
every dependency supplies a `prior_outputs` binding truncated to 6,000
characters, and the complete task request is truncated to 12,000 characters.
The 20 graphs contain 88 nodes, 94 dependency edges and 90 explicitly authored
`outputs.*` input references; these last two counts describe different things.

Text fingerprints marked `utf8-lf` normalize CRLF to LF before SHA-256 hashing;
binary fingerprints hash the exact bytes. The handoff's Windows text files and
public source files therefore need not have identical newline bytes. Generated
prepared inputs also receive raw byte fingerprints and are checked before use.
Preparation fails on any other content mismatch instead of silently accepting
a different version. If an upstream revision is unavailable, the downloader
does not substitute the current default branch.

Eight public Skill files differ from the sanitized handoff only in non-secret
API-key examples or printed setup-help strings. The sanitizer replaced those
examples with redaction markers. Preparation uses the original public files;
their verified hashes and paths are listed in `sources.json` under
`public_template_differences`. This is not a claim that the public inputs are
byte-identical to the sanitized handoff.

`skills/sympy/` is the recovered OpenSquilla experiment implementation, including
its hypothesis-testing helper. It is bundled because its exact public upstream
revision is not established. Its Skill metadata declares Apache-2.0; the
repository's license applies to this included implementation. It is not claimed
to be the unpublished AgentSkillOS `skill_200` snapshot. Other Skills retain
their own source files and any included license notices in the prepared cache.

These are executable inputs, not historical runs or results. No previous
outputs, judgments, logs, credentials or machine-specific configuration are
included. Public websites, model services and external task dependencies can
change; successful preparation alone does not reproduce paper scores.

## Runtime adaptations

The standalone host supplies per-invocation artifact and tool-result session
identities, and each benchmark attempt gets separate tool profiles and caches.
The release also fixes error propagation in
`src/opensquilla/skills/meta/executors/agent.py`: a terminal Agent `ErrorEvent`
fails its MetaSkill step, even if the Agent subsequently emits a completion
event containing partial text and usage for accounting. Partial output is not
treated as successful step completion. This is a disclosed runtime fix, so the
release is not a byte-identical runtime snapshot. Frozen plans, task inputs,
Skill source fingerprints and official evaluation rules are unaffected.

On POSIX, the standalone CLI handles SIGTERM by cancelling the run and awaiting
native tool cleanup. It closes tracked background sessions concurrently within
the batch termination grace period; a cancelled native host command also
terminates its owned process group. These lifecycle fixes preserve cancellation
as a failed or interrupted execution rather than successful completion.
