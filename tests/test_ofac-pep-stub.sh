curl -s localhost:8102/health

# pass
curl -s -XPOST localhost:8102/v1/screen -H 'content-type: application/json' \
 -d '{"application_id":"app-123", "subject":{"name":"Saquon Barkley", "date_of_birth":"1995-04-12","country":"US"}}'

#fail
curl -s -XPOST localhost:8102/v1/screen -H 'content-type: application/json' \
 -d '{"application_id":"app-345", "metadata" :{"scenario":"fail"}}'

