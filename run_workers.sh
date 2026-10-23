#!/usr/bin/env bash
# Start N KYC workers (default 1) polling the same task queue; Ctrl-C stops them all.
# Usage: ./run_workers.sh [n]
cd "$(dirname "$0")"
N="${1:-1}"
[[ "$N" =~ ^[1-9][0-9]*$ ]] || { echo "usage: $0 [number of workers >= 1]" >&2; exit 1; }
trap 'kill 0' EXIT INT TERM
for ((i = 1; i <= N; i++)); do
  .venv/bin/python -m temporal.worker 2>&1 | sed -u "s/^/[w$i] /" &
done
wait
