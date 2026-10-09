#!/usr/bin/env bash
set -euo pipefail

MODE=${1:?mode: ordinary|complex|complete}
ROOT="/data/paper-data/lrk/opensquilla-experiments/claw-swe-timeout600-20261009-${MODE}"
REPORT="$ROOT/reports/batch-run.json"
RECOVERY="${RECOVERY_DIR:-$ROOT/recovery-v2}"
WORKERS="${RECOVERY_WORKERS:-5}"
mkdir -p "$RECOVERY/workspaces" "$RECOVERY/trajectories" "$RECOVERY/state"

TASK_ARGS=()
while IFS= read -r task_id; do
  [ -n "$task_id" ] && TASK_ARGS+=(--task "$task_id")
done < <(python - "$REPORT" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))['results'][0]['report']
for item in report['results']:
    reason = item.get('judge', {}).get('reason', '')
    if any(x in reason for x in (
        'could not load the verified SWE instance image',
        'failed to create a verified SWE instance image archive',
        'benchmark_capacity_lease_lost',
        'native runner failed (rc=1)',
        'swebench harness resolved=',
        'swebench harness report status=error',
    )):
        print(item['task_id'])
PY
)

if [ "${#TASK_ARGS[@]}" -eq 0 ]; then
  echo "No infrastructure-failed tasks for ${MODE}; nothing to recover."
  exit 0
fi

case "$MODE" in
  ordinary) complex=false; single=false; image='claw-swe-opensquilla-runner:claw-swe-timeout600-20261009-ordinary-b901c4c83f52' ;;
  complex)  complex=true;  single=false; image='claw-swe-opensquilla-runner:claw-swe-timeout600-20261009-complex-1602ac49d050' ;;
  complete) complex=false; single=true; image='claw-swe-opensquilla-runner:claw-swe-timeout600-20261009-complete-7ce230dea174' ;;
  *) echo "unknown mode: $MODE" >&2; exit 2 ;;
esac

export DOCKER_HOST=unix:///run/opensquilla-docker.sock
cd /data/paper-data/lrk/opensquilla-experiments/pinchbench-ordinary-rerun-20260929/aef-source
exec /home/lrk/AutoEval-Factory/.venv/bin/aef benchmark controlled-run claw-swe-bench \
  "${TASK_ARGS[@]}" \
  --model opensquilla-four-tier.yaml --harness opensquilla \
  --workspace-root "$RECOVERY/workspaces" \
  --trajectory-root "$RECOVERY/trajectories" \
  --state-root "$RECOVERY/state" \
  --capacity-root "$RECOVERY/capacity" \
  --capacity-pool-id "claw-swe-recovery-${MODE}" \
  --workers "$WORKERS" --global-concurrency "$WORKERS" \
  --benchmarks-dir /home/lrk/AutoEval-Factory/main/benchmarks \
  --image "opensquilla_runner=${image}" --image judge=aef-swebench:local \
  --option "complex_task_mode=${complex}" \
  --option "single_agent_mode=${single}" \
  --option "hf_cache_dir=\"$ROOT/hf-cache\"" \
  --option 'hf_offline=false' --option 'timeout_s=3600' \
  --max-attempts 1 \
  --output "$RECOVERY/result.json" --execute --json
