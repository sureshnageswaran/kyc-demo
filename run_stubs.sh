#!/usr/bin/env bash
# Start the OCR stub (8101) and the OFAC/PEP screening stub (8102); Ctrl-C stops both.
# Usage: ./run_stubs.sh
cd "$(dirname "$0")"
trap 'kill 0' EXIT INT TERM
.venv/bin/python -m uvicorn stubs.ocr_stub:app --port 8101 2>&1 | sed -u 's/^/[ocr] /' &
.venv/bin/python -m uvicorn stubs.screening_stub:app --port 8102 2>&1 | sed -u 's/^/[screening] /' &
wait
