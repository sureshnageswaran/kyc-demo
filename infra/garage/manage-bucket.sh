#!/usr/bin/env bash
ACTION="$1"
BUCKET_NAME="$2"
KEY_NAME="${3:-kyc-app-key}"

if [ -z "$ACTION" ]; then
  echo "Usage: ./manage-bucket.sh [create|delete|list] [bucket-name] [key-name]"
  exit 1
fi

if [ "$ACTION" == "create" ]; then
  if [ -z "$BUCKET_NAME" ]; then
    echo "Error: Bucket name required for creation."
    exit 1
  fi
  echo "Creating bucket '$BUCKET_NAME'..."
  podman exec -it temporal-kyc-garage-01 /garage bucket create "$BUCKET_NAME"
  echo "Granting permissions to key '$KEY_NAME'..."
  podman exec -it temporal-kyc-garage-01 /garage bucket allow "$BUCKET_NAME" --read --write --key "$KEY_NAME"
  echo "Bucket '$BUCKET_NAME' is ready."
elif [ "$ACTION" == "delete" ]; then
  if [ -z "$BUCKET_NAME" ]; then
    echo "Error: Bucket name required for deletion."
    exit 1
  fi
  echo "Deleting bucket '$BUCKET_NAME'..."
  podman exec -it temporal-kyc-garage-01 /garage bucket delete "$BUCKET_NAME"
  echo "Bucket '$BUCKET_NAME' deleted."
elif [ "$ACTION" == "list" ]; then
  echo "Listing all Garage buckets..."
  podman exec -it temporal-kyc-garage-01 /garage bucket list
else
  echo "Unknown action: $ACTION. Use 'create', 'delete', or 'list'."
  exit 1
fi
