#!/usr/bin/env bash
# Upload the Happy Path submission and its PDFs to the Garage bucket, then start
# a KYC workflow for it. Repeats N times, each with its own application.
#
# Usage: scripts/upload_happy_path.sh [count] [data-dir]     (defaults: 1, ./data)
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
set -euo pipefail
cd "$(dirname "$0")/.."
COUNT="${1:-1}"
DATA_DIR="${2:-data}"
[[ "$COUNT" =~ ^[1-9][0-9]*$ ]] || { echo "usage: $0 [count >= 1] [data-dir]" >&2; exit 1; }
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

SUBMISSION_FILE=$(mktemp); trap 'rm -f "$SUBMISSION_FILE"' EXIT
STARTED=()

run_one() {
  local app sub sub_key sub_sha file key
  app=$(python3 -c 'import uuid; print(uuid.uuid4())')
  sub=$(python3 -c 'import uuid; print(uuid.uuid4())')
  jq --arg oa "$OLD_APP" --arg na "$app" --arg ns "$sub" '
    .application_id = $na | .submission_id = $ns
    | .documents |= map(.object_key |= sub("applications/" + $oa + "/"; "applications/" + $na + "/"))
  ' "$SUB" > "$SUBMISSION_FILE"

  while IFS=$'\t' read -r file key; do
    s3 s3api put-object --bucket "$S3_BUCKET" --key "$key" --body "$DATA_DIR/$file" \
       --content-type application/pdf >/dev/null
  done < <(jq -r '.documents[] | [.original_filename, .object_key] | @tsv' "$SUBMISSION_FILE")

  # Upload the submission last: it is what the workflow reads first.
  sub_key="applications/$app/submission.json"
  sub_sha=$(sha256sum "$SUBMISSION_FILE" | cut -d' ' -f1)
  s3 s3api put-object --bucket "$S3_BUCKET" --key "$sub_key" --body "$SUBMISSION_FILE" \
     --content-type application/json >/dev/null

  .venv/bin/python -m temporal.cli.start_workflow --application-id "$app" \
    --submission-id "$sub" --sha256 "$sub_sha" --timer-profile "$PROFILE"
  STARTED+=("$app")
}

for ((i = 1; i <= COUNT; i++)); do
  echo "[$i/$COUNT] uploading and starting (timer profile $PROFILE)"
  run_one
done

echo
echo "Started ${#STARTED[@]} workflow(s):"
printf '  kyc-%s\n' "${STARTED[@]}"
echo
echo "Check one with:"
echo "  .venv/bin/python -m temporal.cli.operator_cli state <application-id>"
