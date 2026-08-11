# OpenSquilla ensemble metrics reference stack

This directory is an opt-in, single-host reference deployment for the bounded
ensemble execution JSONL transport. It provisions:

- Grafana Alloy to tail only the reviewed active JSONL file and reject rows
  whose transport, event, metrics schema, or fixed label enums do not match;
- Loki in single-binary filesystem mode with 14-day retention and local ruler
  evaluation;
- Alertmanager with a local UI receiver; and
- Grafana with a provisioned Loki datasource and the
  `OpenSquilla Ensemble Execution` dashboard.

It is deliberately an evaluation/reference stack, not a production HA
observability platform. It has no authenticated remote ingestion, object
storage, cross-host replication, delivery acknowledgement, or external alert
receiver. Configure those independently before using the metrics for a
production SLO or paging policy.

## Prerequisites

- Docker Engine with the Compose plugin.
- A POSIX gateway with the JSONL transport enabled.
- An absolute owner-only metrics directory on the same Docker host.

Create the directory before starting the gateway and keep its mode at `0700`:

```bash
install -d -m 0700 "$HOME/.local/state/opensquilla/ensemble-metrics"
export OPENSQUILLA_ENSEMBLE_METRICS_JSONL=1
export OPENSQUILLA_ENSEMBLE_METRICS_JSONL_DIR="$HOME/.local/state/opensquilla/ensemble-metrics"
```

The application creates the JSONL, backup, and lock files with mode `0600`.
Alloy receives that directory read-only and tails only the active file; it does
not replay rotated backups, which avoids duplicate ingestion when path-based
rotation renames the active inode. The Alloy container must run on the same
host because this reference deployment uses a bind mount rather than a network
transport.

The owner-only directory is a trusted handoff from OpenSquilla's Python
transport contract, which performs the complete field allowlist/type/privacy
validation. Alloy adds schema and fixed-enum filtering but forwards the
validated scalar JSON line so every reviewed value remains queryable; it is not
a second arbitrary-JSON allowlist. Never point this bind mount at a file that
can be written by an untrusted producer.

## Start and verify

Set a non-default Grafana password. Ports bind to loopback unless explicitly
overridden:

```bash
export OPENSQUILLA_GRAFANA_ADMIN_PASSWORD='replace-with-a-secret'
docker compose \
  -f deploy/observability/ensemble-metrics/compose.yaml \
  up -d
```

Open `http://127.0.0.1:3000` and select the `OpenSquilla` folder. The local
service endpoints are:

- Grafana: `127.0.0.1:3000`
- Loki: `127.0.0.1:3100`
- Alertmanager: `127.0.0.1:9093`
- Alloy: `127.0.0.1:12345`

Run the static deployment check without starting containers:

```bash
python deploy/observability/ensemble-metrics/verify_reference_stack.py
```

Run the opt-in end-to-end smoke test to start an isolated Compose project,
inject strict synthetic receipts, query Loki and Grafana, verify ruler and
Alertmanager readiness, and always remove the project and volumes afterward:

```bash
python deploy/observability/ensemble-metrics/verify_reference_stack.py --live
```

The live check pulls the pinned images when they are not already present. It
uses random loopback ports, a temporary `0700` JSONL directory, and a unique
Compose project name, so it does not share state with a running deployment.

## Stop

```bash
docker compose \
  -f deploy/observability/ensemble-metrics/compose.yaml \
  down
```

Add `--volumes` only when the local evaluation history may be discarded.

## Production boundary

Before production use, replace filesystem Loki storage with supported durable
object storage and an HA topology, terminate authenticated ingress, configure
an external Alertmanager receiver, and monitor the collector/backend itself.
The source transport is best-effort and intentionally has no delivery receipt,
so this stack cannot prove end-to-end metrics coverage. Fields documented as
unavailable remain unavailable; dashboards must not coerce them to zero.
