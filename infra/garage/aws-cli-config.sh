aws configure set aws_access_key_id GK72715350db33d1ada96b6a97
aws configure set aws_secret_access_key e29c9588dc7c1a644cd7996cda91155b5ed4cfaeca9e5829a13493fcb7d9971c
aws configure set region garage
aws --endpoint-url http://localhost:3900 s3 ls
