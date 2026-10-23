curl -s localhost:8101/health

# valid_id (default, no metadata)
curl -s -XPOST localhost:8101/v1/extract -H 'content-type: application/json' \
 -d '{"application_id":"app-123", "document_id":"doc-1", "document_type":"drivers_license"}'

# valid_id (explicit)
curl -s -XPOST localhost:8101/v1/extract -H 'content-type: application/json' \
 -d '{"application_id":"app-123", "document_id":"doc-2", "document_type":"drivers_license", "metadata":{"scenario":"valid_id"}}'

# expired_id
curl -s -XPOST localhost:8101/v1/extract -H 'content-type: application/json' \
 -d '{"application_id":"app-345", "document_id":"doc-3", "document_type":"drivers_license", "metadata":{"scenario":"expired_id"}}'

# name_mismatch
curl -s -XPOST localhost:8101/v1/extract -H 'content-type: application/json' \
 -d '{"application_id":"app-345", "document_id":"doc-4", "document_type":"drivers_license", "metadata":{"scenario":"name_mismatch"}}'

# low_confidence
curl -s -XPOST localhost:8101/v1/extract -H 'content-type: application/json' \
 -d '{"application_id":"app-345", "document_id":"doc-5", "document_type":"drivers_license", "metadata":{"scenario":"low_confidence"}}'

# unreadable
curl -s -XPOST localhost:8101/v1/extract -H 'content-type: application/json' \
 -d '{"application_id":"app-345", "document_id":"doc-6", "document_type":"drivers_license", "metadata":{"scenario":"unreadable"}}'

# other document types (utility bill, credit report)
curl -s -XPOST localhost:8101/v1/extract -H 'content-type: application/json' \
 -d '{"application_id":"app-123", "document_id":"doc-7", "document_type":"utility_bill"}'
curl -s -XPOST localhost:8101/v1/extract -H 'content-type: application/json' \
 -d '{"application_id":"app-123", "document_id":"doc-8", "document_type":"other"}'
