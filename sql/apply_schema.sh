#!/usr/bin/env bash
# Apply the KYC schema to the business Postgres database.
# Reads DATABASE_URL from the environment or the repo-root .env.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -z "${DATABASE_URL:-}" && -f .env ]]; then
  set -a; source .env; set +a
fi
: "${DATABASE_URL:?DATABASE_URL is not set (see .env)}"

for f in sql/[0-9]*.sql; do
  echo "applying $f"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$f"
done
echo "schema applied"
