#!/usr/bin/env bash
# Upload the Happy Path submission and its PDFs to the Garage bucket, then start
# a KYC workflow for it. Repeats COUNT times, each with its own application,
# running up to MAX_CONCURRENT (default 20) at once.
#
# Usage: scripts/upload_happy_path.sh [count] [failure-percent] [data-dir]
#        (defaults: 1, 0, ./data)
#
# failure-percent: that share of the COUNT workflows (rounded to nearest, randomly
# chosen) get a "metadata_packet" added to their submission.json. The OCR and
# screening stubs read it to simulate a specific failure. The packet for each is
# picked at random from data/failure_scenarios.json (override with FAILURE_SCENARIOS).
# The rest get no packet and follow the happy path.
#
# Every iteration generates a fresh application_id/submission_id (the workflow ID
# kyc-<application_id> can only be used once), uploads the PDFs and a rewritten
# copy of submission.json under them, then starts the workflow. The source
# file in data/ is never modified.
#
# data/submission.json drives the upload: each document's object_key says where
# its PDF goes (matched to the local file by original_filename), and the
# submission itself goes to applications/<application_id>/submission.json.
# Local file sizes and SHA-256 are verified against the submission before upload.
# Reads S3_* and Temporal settings from the repo-root .env.
# Set TIMER_PROFILE=normal-v1 for real timers (default: demo-v1).
# Set MAX_CONCURRENT=N to change the concurrency cap (default: 20).
set -euo pipefail
cd "$(dirname "$0")/.."
COUNT="${1:-1}"
FAIL_PCT="${2:-0}"
DATA_DIR="${3:-data}"
MAX_CONCURRENT="${MAX_CONCURRENT:-20}"
usage() { echo "usage: $0 [count >= 1] [failure-percent 0-100] [data-dir]" >&2; exit 1; }
[[ "$COUNT" =~ ^[1-9][0-9]*$ ]] || usage
[[ "$FAIL_PCT" =~ ^[0-9]+$ ]] && (( FAIL_PCT <= 100 )) || usage
[[ "$MAX_CONCURRENT" =~ ^[1-9][0-9]*$ ]] || { echo "MAX_CONCURRENT must be >= 1" >&2; exit 1; }
SCENARIOS="${FAILURE_SCENARIOS:-$DATA_DIR/failure_scenarios.json}"
PROFILE="${TIMER_PROFILE:-demo-v1}"

set -a; source .env; set +a
: "${S3_ENDPOINT_URL:?}" "${S3_ACCESS_KEY_ID:?}" "${S3_SECRET_ACCESS_KEY:?}" "${S3_BUCKET:?}"
export AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$S3_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="${S3_REGION:-garage}" AWS_EC2_METADATA_DISABLED=true
s3() { aws --endpoint-url "$S3_ENDPOINT_URL" "$@"; }

SUB="$DATA_DIR/submission.json"
[[ -f "$SUB" ]] || { echo "missing $SUB" >&2; exit 1; }
OLD_APP=$(jq -r .application_id "$SUB")

s3 s3api head-bucket --bucket "$S3_BUCKET" 2>/dev/null || {
  echo "bucket '$S3_BUCKET' not reachable. Create it with:" >&2
  echo "  infra/garage/manage-bucket.sh create $S3_BUCKET" >&2
  exit 1
}

# Verify every local PDF once, up front, so a bad file fails before anything is uploaded.
while IFS=$'\t' read -r file size sha; do
  path="$DATA_DIR/$file"
  [[ -f "$path" ]] || { echo "missing local file $path" >&2; exit 1; }
  [[ "$(stat -c %s "$path")" == "$size" ]] || { echo "size mismatch: $path" >&2; exit 1; }
  [[ "$(sha256sum "$path" | cut -d' ' -f1)" == "$sha" ]] || { echo "sha256 mismatch: $path" >&2; exit 1; }
done < <(jq -r '.documents[] | [.original_filename, .size_bytes, .sha256] | @tsv' "$SUB")

WORK_DIR=$(mktemp -d); trap 'rm -rf "$WORK_DIR"' EXIT

# Pick which iterations get a failure packet: round(COUNT * FAIL_PCT / 100) of them, at random.
N_FAIL=$(( (COUNT * FAIL_PCT + 50) / 100 ))
FAIL_IDX=" "
if (( N_FAIL > 0 )); then
  [[ -f "$SCENARIOS" ]] || { echo "missing $SCENARIOS" >&2; exit 1; }
  N_SCEN=$(jq length "$SCENARIOS")
  (( N_SCEN > 0 )) || { echo "$SCENARIOS has no scenarios" >&2; exit 1; }
  FAIL_IDX=" $(seq 1 "$COUNT" | shuf -n "$N_FAIL" | tr '\n' ' ')"
fi

# Runs in a background subshell; writes "<app>\t<scenario>" to $WORK_DIR/<i>.ok on success.
run_one() {
  local i="$1" app sub sub_key sub_sha file key scenario="-" packet
  local submission_file="$WORK_DIR/$i.submission.json"
  app=$(python3 -c 'import uuid; print(uuid.uuid4())')
  sub=$(python3 -c 'import uuid; print(uuid.uuid4())')
  packet=null
  if [[ "$FAIL_IDX" == *" $i "* ]]; then
    packet=$(jq -c ".[$(( RANDOM % N_SCEN ))]" "$SCENARIOS")
    scenario=$(jq -r .name <<<"$packet")
    packet=$(jq -c 'del(.name)' <<<"$packet")
  fi
  jq --arg oa "$OLD_APP" --arg na "$app" --arg ns "$sub" --argjson mp "$packet" '
    .application_id = $na | .submission_id = $ns
    | .documents |= map(.object_key |= sub("applications/" + $oa + "/"; "applications/" + $na + "/"))
    | if $mp != null then .metadata_packet = $mp else del(.metadata_packet) end
  ' "$SUB" > "$submission_file"

  while IFS=$'\t' read -r file key; do
    s3 s3api put-object --bucket "$S3_BUCKET" --key "$key" --body "$DATA_DIR/$file" \
       --content-type application/pdf >/dev/null
  done < <(jq -r '.documents[] | [.original_filename, .object_key] | @tsv' "$submission_file")

  # Upload the submission last: it is what the workflow reads first.
  sub_key="applications/$app/submission.json"
  sub_sha=$(sha256sum "$submission_file" | cut -d' ' -f1)
  s3 s3api put-object --bucket "$S3_BUCKET" --key "$sub_key" --body "$submission_file" \
     --content-type application/json >/dev/null

  .venv/bin/python -m temporal.cli.start_workflow --application-id "$app" \
    --submission-id "$sub" --sha256 "$sub_sha" --timer-profile "$PROFILE"
  printf '%s\t%s\n' "$app" "$scenario" > "$WORK_DIR/$i.ok"
}

echo "Starting $COUNT workflow(s), $N_FAIL with a failure packet, up to $MAX_CONCURRENT at a time (timer profile $PROFILE)"
for ((i = 1; i <= COUNT; i++)); do
  while (( $(jobs -rp | wc -l) >= MAX_CONCURRENT )); do wait -n || true; done
  ( run_one "$i" 2>&1 | sed -u "s/^/[$i\/$COUNT] /" ) &
done
wait

echo
STARTED=$(cat "$WORK_DIR"/*.ok 2>/dev/null | wc -l)
echo "Started $STARTED of $COUNT workflow(s):"
for ((i = 1; i <= COUNT; i++)); do
  [[ -f "$WORK_DIR/$i.ok" ]] || continue
  IFS=$'\t' read -r app scenario < "$WORK_DIR/$i.ok"
  printf '  kyc-%s  %s\n' "$app" "$scenario"
done
(( STARTED == COUNT )) || { echo "$(( COUNT - STARTED )) failed to start; see output above" >&2; exit 1; }
echo
echo "Check one with:"
echo "  .venv/bin/python -m temporal.cli.operator_cli state <application-id>"
