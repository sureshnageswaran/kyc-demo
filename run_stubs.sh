#!/usr/bin/env bash
# Start the OCR (8101), OFAC/PEP screening (8102) and account (8103) stubs; Ctrl-C stops all.
# Usage: ./run_stubs.sh
cd "$(dirname "$0")"
trap 'kill 0' EXIT INT TERM
for stub in "ocr 8101" "screening 8102" "account 8103"; do
  set -- $stub
  .venv/bin/python -m uvicorn "stubs.$1_stub:app" --port "$2" 2>&1 | sed -u "s/^/[$1] /" &
done
wait
