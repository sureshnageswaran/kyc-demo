curl -s localhost:8103/health

# created (default, no metadata)
curl -s -XPOST localhost:8103/v1/accounts -H 'content-type: application/json' -H 'Idempotency-Key: kyc-activate:app-123' \
 -d '{"application_id":"app-123", "evaluation_id":"eval-1", "decision":"APPROVED"}'

# created again with the same key: same account_id (idempotent)
curl -s -XPOST localhost:8103/v1/accounts -H 'content-type: application/json' -H 'Idempotency-Key: kyc-activate:app-123' \
 -d '{"application_id":"app-123", "evaluation_id":"eval-1", "decision":"APPROVED"}'

# created (explicit)
curl -s -XPOST localhost:8103/v1/accounts -H 'content-type: application/json' -H 'Idempotency-Key: kyc-activate:app-345' \
 -d '{"application_id":"app-345", "evaluation_id":"eval-2", "decision":"APPROVED", "metadata":{"scenario":"created"}}'

# rejected (HTTP 409)
curl -s -w ' [HTTP %{http_code}]' -XPOST localhost:8103/v1/accounts -H 'content-type: application/json' -H 'Idempotency-Key: kyc-activate:app-456' \
 -d '{"application_id":"app-456", "evaluation_id":"eval-3", "decision":"APPROVED", "metadata":{"scenario":"rejected"}}'

# unavailable (HTTP 503)
curl -s -w ' [HTTP %{http_code}]' -XPOST localhost:8103/v1/accounts -H 'content-type: application/json' -H 'Idempotency-Key: kyc-activate:app-567' \
 -d '{"application_id":"app-567", "evaluation_id":"eval-4", "decision":"APPROVED", "metadata":{"scenario":"unavailable"}}'
