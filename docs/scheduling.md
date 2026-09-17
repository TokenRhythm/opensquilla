# Scheduling

OpenSquilla scheduling lets you run recurring or one-time agent work from the
gateway. Use it for reminders, periodic summaries, status checks, channel
updates, and webhook-delivered automation.

Scheduling is managed with the `opensquilla cron` command group.

## Heartbeat Configuration Compatibility

The configuration-driven heartbeat loop and the main-session cron wake modes
`now` and `next-heartbeat` remain available. Dream scheduling and connection
keepalives are unchanged.

`HEARTBEAT.md` no longer supplies instructions or scheduling configuration.
Its frontmatter, quiet hours, and empty-file suppression are ignored.
`heartbeat.config_path` (also `configPath` and its environment-variable
spellings) remains accepted for configuration round-tripping, but is deprecated
and ignored: the referenced path is never read.

Before upgrading, check the effective `heartbeat.enabled` configuration.
If it is true and the old file previously disabled or limited runs, retiring
those restrictions can increase execution or change its hours. If only the
file enabled heartbeat, it stops running when the configuration is false.
The upgrade does not change enabled settings or migrate any tasks into cron.
A startup warning is emitted when heartbeat is enabled or the old path setting
is supplied. Review the configuration explicitly instead of relying on the old
file to suppress execution.

## Requirements

Scheduled jobs run through the gateway:

```sh
opensquilla gateway run
```

For long-lived local use, start the managed gateway:

```sh
opensquilla gateway start --json
opensquilla gateway status
```

## List Jobs

```sh
opensquilla cron list
opensquilla cron list --agent main
opensquilla cron list --json
```

## Add an Interval Job

Run a prompt every hour:

```sh
opensquilla cron add \
  --every 1h \
  --text "Summarize important project updates" \
  --name hourly-project-check
```

Intervals accept values such as `30s`, `5m`, and `1h`.

## Add a Cron Expression

Run on weekdays at 09:00 in a named timezone:

```sh
opensquilla cron add \
  --cron "0 9 * * 1-5" \
  --tz "America/Los_Angeles" \
  --text "Prepare a short morning brief" \
  --name weekday-morning-brief
```

Use `--exact` when you do not want the default stagger.

## Add a One-Time Job

```sh
opensquilla cron add \
  --at "2026-06-01T09:00:00+00:00" \
  --text "Remind me to review the launch checklist" \
  --name launch-checklist-reminder
```

## Choose the Session Target

The default target is an isolated session. For most scheduled work, that is the
least surprising option.

Useful targets:

| Target | Use when |
| --- | --- |
| `isolated` | Each scheduled run should stand alone. |
| `session` | You want to deliver into a specific session configured by the runtime surface. |
| `main` | You want a system event for the main session. |

Example:

```sh
opensquilla cron add \
  --every 30m \
  --session-target isolated \
  --text "Check for urgent channel updates" \
  --name urgent-update-check
```

## Delivery

Disable delivery:

```sh
opensquilla cron add \
  --every 1h \
  --text "Create a private summary" \
  --no-deliver \
  --name private-hourly-summary
```

Deliver through a webhook:

```sh
opensquilla cron add \
  --every 1h \
  --text "Post a compact status summary" \
  --webhook-url https://example.com/hooks/opensquilla \
  --webhook-token-env OPENSQUILLA_WEBHOOK_TOKEN \
  --name webhook-status-summary
```

Prefer `--webhook-token-env` or `--webhook-token-file` over inline tokens so
secrets do not land in shell history.

## Inspect and Run Jobs

```sh
opensquilla cron status <job-id>
opensquilla cron runs <job-id>
opensquilla cron runs <job-id> --limit 50
```

Run a job immediately:

```sh
opensquilla cron run <job-id> --yes
```

## Update or Remove Jobs

```sh
opensquilla cron update <job-id> --enabled
opensquilla cron update <job-id> --disabled
opensquilla cron update <job-id> --every 2h
opensquilla cron remove <job-id> --yes
```

Primary delivery destinations are not patched in place from the CLI. Remove and
re-add a job when the primary channel or webhook destination needs to change.

## Troubleshooting

Check the gateway and job state:

```sh
opensquilla gateway status
opensquilla cron list
opensquilla cron status <job-id>
opensquilla cron runs <job-id>
```

If a job posts to a channel, also check:

```sh
opensquilla channels status
```

Read next:

- [`channels.md`](channels.md)
- [`operations.md`](operations.md)
- [`troubleshooting.md`](troubleshooting.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
