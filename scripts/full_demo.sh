#!/usr/bin/env bash
# Step-by-step KYC demo: a success flow, a failed flow, then a 100-workflow batch with 20 failures.
# Pauses after each step (Enter to continue). Set NO_PAUSE=1 to run straight through.
#
# Prerequisites (each in its own terminal): ./run_stubs.sh and ./run_workers.sh [n]; Garage,
# Postgres and Temporal up; a policy published (python -m temporal.cli.publish_policy 1).
set -euo pipefail
cd "$(dirname "$0")/.."

CLI=".venv/bin/python -m temporal.cli.operator_cli"
WORK=$(mktemp -d)
# If the demo is interrupted while policy v2 is current, put v1 back so later runs start clean.
V2_CURRENT=""
cleanup() {
  rm -rf "$WORK"
  [[ -z "$V2_CURRENT" ]] || .venv/bin/python -m temporal.cli.publish_policy 1 >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Real review timers so a review waits for us instead of expiring in 60s.
export TIMER_PROFILE=normal-v1

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
pause() { [[ -n "${NO_PAUSE:-}" || ! -t 0 ]] || read -r -p $'\n[Enter to continue] ' _; }
state() { $CLI state "$1" | jq '{business_status, current_stage, waiting_reason, policy_version, failed_document_ids, available_actions}'; }
status_of() { $CLI state "$1" | jq -r .business_status; }

# wait_settled <app> [seconds]: poll until the workflow stops moving (done, in review, or in error).
# Prints each status change, and the CLI's error if it cannot query the workflow.
wait_settled() {
  local app="$1" deadline=$((SECONDS + ${2:-60})) s last="" err="$WORK/wait.err"
  echo "Waiting for $app to settle..."
  while (( SECONDS < deadline )); do
    if s=$(status_of "$app" 2>"$err"); then
      [[ "$s" == "$last" ]] || { echo "  status: $s"; last="$s"; }
      case "$s" in ACTIVE|REJECTED|AWAITING_REVIEW|ACTIVATION_ERROR|PROCESSING_ERROR|REVIEW_EXPIRED) return 0 ;; esac
    elif [[ "$last" != "query failed" ]]; then
      echo "  could not query the workflow yet: $(tail -1 "$err")"; last="query failed"
    fi
    sleep 1
  done
  echo "  (still ${s:-unknown} after ${2:-60}s; workers running? try: $CLI state $app)"
}
app_of() { grep -o 'kyc-[0-9a-f-]\{36\}' "$1" | tail -1 | sed 's/^kyc-//'; }

bold "Preflight: stubs"
for hp in ocr:8101 screening:8102 account:8103; do
  curl -sf "localhost:${hp#*:}/health" >/dev/null \
    && echo "  ${hp%%:*} stub (:${hp#*:}) ok" \
    || { echo "  ${hp%%:*} stub on :${hp#*:} is not answering; start ./run_stubs.sh" >&2; exit 1; }
done
echo "  (workers are assumed to be running: ./run_workers.sh)"
bold "Preflight: policy v1 is current (v1 requires average credit age > 48 months)"
.venv/bin/python -m temporal.cli.publish_policy 1
pause

bold "Step 1: success flow - a clean application, no failure packet"
scripts/upload_happy_path.sh 1 0 | tee "$WORK/ok.out"
APP_OK=$(app_of "$WORK/ok.out")
echo "Application id: $APP_OK"
wait_settled "$APP_OK"
state "$APP_OK"
echo "Expected: ACTIVE - OCR, screening and policy all pass, so it auto-approves and the account is created."
pause

bold "Step 2: failed flow - screening stub returns a match, so a human must review"
echo '[{"name":"screening-fail","screening":{"scenario":"fail"}}]' > "$WORK/fail.json"
FAILURE_SCENARIOS="$WORK/fail.json" scripts/upload_happy_path.sh 1 100 | tee "$WORK/fail.out"
APP_FAIL=$(app_of "$WORK/fail.out")
echo "Application id: $APP_FAIL"
wait_settled "$APP_FAIL"
state "$APP_FAIL"
echo "Expected: AWAITING_REVIEW with submit_review_decision available."
pause

bold "Step 3: the reviewer decides on the stuck application"
if [[ -n "${NO_PAUSE:-}" || ! -t 0 ]]; then
  $CLI decide "$APP_FAIL" REJECT --actor demo-reviewer --reason "potential OFAC match" | jq .
else
  echo "Application $APP_FAIL is waiting in review. In a SEPARATE terminal, run:"
  echo
  echo "  cd $PWD"
  echo "  $CLI decide $APP_FAIL REJECT --actor demo-reviewer --reason \"potential OFAC match\""
  echo
  echo "(or approve it instead:  $CLI decide $APP_FAIL APPROVE --actor demo-reviewer --reason ok)"
  echo "(inspect it first with:  $CLI state $APP_FAIL)"
  read -r -p $'\nTry it, then press Enter here to move on... ' _
fi
wait_settled "$APP_FAIL" 15; sleep 2
state "$APP_FAIL"
echo "Expected: REJECTED if you rejected it (no account activated), or ACTIVE if you approved it."
pause

bold "Step 4: policy change - publish v2; new workflows use it, and a waiting review is re-evaluated"
echo "First, an application that will be sitting in review (under policy v1) when v2 is published..."
FAILURE_SCENARIOS="$WORK/fail.json" scripts/upload_happy_path.sh 1 100 | tee "$WORK/wait.out"
APP_WAIT=$(app_of "$WORK/wait.out")
echo "Application id: $APP_WAIT"
wait_settled "$APP_WAIT"
echo "It is waiting in review under policy version: $($CLI state "$APP_WAIT" | jq .policy_version)"
echo
echo "The Step 1 application was evaluated under policy version: $($CLI state "$APP_OK" | jq .policy_version) (unchanged by what follows)."
echo
echo "Publishing policy v2 (requires average credit age > 72; the OCR stub reports exactly 72):"
V2_CURRENT=1
.venv/bin/python -m temporal.cli.publish_policy 2
echo
echo "Starting the same clean application again, now under the new policy..."
scripts/upload_happy_path.sh 1 0 | tee "$WORK/v2.out"
APP_V2=$(app_of "$WORK/v2.out")
echo "Application id: $APP_V2"
wait_settled "$APP_V2"
state "$APP_V2"
echo "Expected: policy_version 2 and AWAITING_REVIEW. The identical application that was ACTIVE in Step 1 now needs"
echo "review, because the new policy was read at evaluation time. Step 1's application kept policy v1."
pause

bold "Step 4b: the waiting review meets the new policy"
echo "Application $APP_WAIT has been in review since before v2 was published. When its reviewer decides,"
echo "the workflow re-reads the current policy. A decision is never applied to a policy the reviewer didn't see."
echo
if [[ -n "${NO_PAUSE:-}" || ! -t 0 ]]; then
  $CLI decide "$APP_WAIT" APPROVE --actor demo-reviewer --reason "ok" 2>&1 | tail -3 || true
else
  echo "In a SEPARATE terminal, run (1st attempt - expect it to be REJECTED with PolicyChanged):"
  echo
  echo "  cd $PWD"
  echo "  $CLI decide $APP_WAIT APPROVE --actor demo-reviewer --reason ok"
  read -r -p $'\nTry it, then press Enter here to move on... ' _
fi
echo
echo "The review was re-evaluated under the new policy:"
state "$APP_WAIT"
echo "Expected: policy_version 2, still AWAITING_REVIEW, with a NEW evaluation and review task."
echo
if [[ -n "${NO_PAUSE:-}" || ! -t 0 ]]; then
  $CLI decide "$APP_WAIT" REJECT --actor demo-reviewer --reason "decided against policy v2" | jq -r .state
else
  echo "Now decide again, against the re-evaluated review (the command picks up the new ids itself):"
  echo
  echo "  $CLI decide $APP_WAIT REJECT --actor demo-reviewer --reason \"decided against policy v2\""
  read -r -p $'\nTry it, then press Enter here to move on... ' _
fi
wait_settled "$APP_WAIT" 15; sleep 2
state "$APP_WAIT"
echo "Expected: REJECTED - the second decision was accepted because the reviewer had seen policy v2."
pause

bold "Restoring policy v1 so the batch below runs against the original policy"
.venv/bin/python -m temporal.cli.publish_policy 1
V2_CURRENT=""
pause

bold "Step 5: batch - 100 workflows, 20 of them with a random failure scenario (up to 20 at once)"
scripts/upload_happy_path.sh 100 20 | tee "$WORK/batch.out"
grep '^  kyc-' "$WORK/batch.out" | sed 's/^  kyc-//' > "$WORK/apps.txt"
echo "Waiting for the workflows to settle..."
sleep 15
pause

bold "Step 6: batch results (scenario, final status; - is the happy path)"
# apps.txt lines are "<application-id>  <scenario>" ("-" = happy path); query up to 20 at a time.
xargs -P 20 -L 1 bash -c 'echo "$1 $(.venv/bin/python -m temporal.cli.operator_cli state "$0" | jq -r .business_status)"' \
  < "$WORK/apps.txt" | sort | uniq -c
echo
echo "Expected: ~80 happy-path ACTIVE; the 20 failures mostly AWAITING_REVIEW (OCR and screening scenarios),"
echo "and ACTIVATION_ERROR for account-* scenarios."
echo "Drill into any one with: $CLI state <application-id>"
