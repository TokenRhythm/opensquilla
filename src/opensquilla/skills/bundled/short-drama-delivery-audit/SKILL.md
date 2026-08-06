---
name: short-drama-delivery-audit
description: "Internal deterministic delivery gate for meta-short-drama. Verifies real-provider image/video receipts, parent-owned paid-submission dispositions, runtime fallback evidence, decodability, and content-versus-final duration with ffprobe."
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  platform:
    requires:
      bins: ["ffprobe"]
      anyBins: ["python", "python3"]
    install:
      - kind: toolchain
        id: media-ffmpeg
        label: "Install verified FFmpeg toolchain"
        bins: ["ffprobe"]
        os: [darwin, linux, windows]
  opensquilla:
    risk: medium
    capabilities: [filesystem-read, process-control]
entrypoint:
  command: python {baseDir}/scripts/audit_delivery.py
  args:
    - "--run-dir"
    - "{{ with.run_dir }}"
  stdin: "{{ with.runtime | tojson }}"
  parse: json
  timeout: 30
---

# short-drama-delivery-audit

Internal machine-owned gate used immediately before `meta-short-drama`
publishes its final video. It does not call a model and never contacts a media
provider.

The helper parses the canonical `script.txt` to identify active shots and
their content durations. It then requires sanitized real-provider receipts
for the reference image, every active shot image, and every active shot video;
rejects placeholders, missing provider request/job IDs, and any runtime
fallback substitution; and uses `ffprobe` to confirm that shot MP4s and the
final MP4 are decodable and have the promised durations.

When Seedance rejects a shot under provider policy, its sidecar contains only
the bounded reason `provider_policy_rejected` and an allowlisted policy code.
The audit preserves those two fields, marks the shot not generated, and
reports `VIDEO_POLICY_REJECTED`; raw provider text, signed URLs, request IDs,
and secrets are discarded before this boundary.

The parent scheduler supplies one bounded disposition per paid step. The only
accepted values are `safe_no_submit`, `maybe_accepted`, and `receipt`. It also
supplies a separate bounded SHA-256 proof only when the exact bundled paid
subprocess emitted a sanitized receipt on this invocation and the emitted JSON
matched the resulting sidecar. A conclusive generated/policy receipt upgrades
the public asset disposition to `confirmed` only when that current-run proof
matches. The reserved runtime slots cannot be declared as plan steps.

When `safe_no_submit` accompanies a missing receipt and local fallback, the
audit preserves the degraded provenance but does not claim that the provider
may have billed the user. A `maybe_accepted`, `receipt`, missing, or malformed
disposition without a conclusive, current-run-proven receipt remains
fail-closed: the audit emits
`PAID_SUBMISSION_STATUS_UNKNOWN`, a sanitized asset-name list, and a static
instruction to check provider history before starting a replacement. Raw
fallback output, child failure text, and provider text never cross into the
verdict. A stale or forged workspace sidecar by itself is reported as
`RECEIPT_NOT_PROVEN_CURRENT_RUN` and can never become `confirmed`.

Receipt or media evidence for a shot absent from the canonical script is
reported as `UNEXPECTED_PAID_ASSET` and listed in `unexpected_paid_assets`.
An incomplete receipt for that unexpected asset also carries the same
fail-closed unknown-billing warning.

`OVERVIEW.DURATION_S` is story-content duration. The meta workflow adds a
fixed two-second title and two-second ending, so the expected final duration
is content duration plus four seconds. Small encoder timestamp differences are
tolerated, but a three-second final file cannot satisfy a seven-second expected
delivery.

## Inputs

- `with.run_dir`: runtime-owned short-drama output directory.
- `with.runtime.paid_submission_dispositions`: bounded JSON object produced by
  the parent scheduler under its reserved output key. It contains only static
  step IDs and fixed disposition values.
- `with.runtime.paid_submission_receipt_proofs`: bounded JSON object produced
  by the parent scheduler under a second reserved output key. It contains only
  static step IDs and canonical `sha256:<hex>` receipt digests from exact
  bundled subprocess output captured during this run.
- `with.runtime.fallback_outputs`: mapping of shot number to the corresponding
  fallback step output. A non-empty value proves that local substitution ran.

## Output

One JSON verdict with `status` (`verified`, `degraded`, or `blocked`),
`verified`, `media_provenance`, active shots, content/final durations,
whitelisted provider identifiers, per-asset `paid_submission_dispositions`,
`safe_no_submit_assets`, `may_have_been_billed`,
`paid_submission_status_unknown_assets`, `unexpected_paid_assets`,
`billing_guidance`, and bounded issue codes. Raw prompts, provider responses,
fallback output, signed URLs, and credentials are never emitted.
