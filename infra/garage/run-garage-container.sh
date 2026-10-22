#!/usr/bin/env bash

GARAGE_DIR="$HOME/garage"
mkdir -p "$GARAGE_DIR"

if [ ! -f "$GARAGE_DIR/garage.toml" ]; then
  cat << 'EOF' > "$GARAGE_DIR/garage.toml"
metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "sqlite"

# Required by Garage for cluster RPC authentication
rpc_secret = "b64c8d5239a2f2b3e817452d193f48a171d6f29e12b3c4d5e6f7a8b9c0d1e2f3"
replication_factor = 1

rpc_bind_addr = "0.0.0.0:3901"
rpc_public_addr = "127.0.0.1:3901"

[s3_api]
s3_region = "garage"
api_bind_addr = "0.0.0.0:3900"

[web]
bind_addr = "0.0.0.0:3902"
EOF
  echo "Created default configuration at $GARAGE_DIR/garage.toml"
fi

podman volume exists garage-volume-01 || podman volume create garage-volume-01

podman run -d \
  --name temporal-kyc-garage-01 \
  -p 3900:3900 \
  -p 3901:3901 \
  -p 3902:3902 \
  -v "$GARAGE_DIR/garage.toml":/etc/garage.toml:Z \
  -v garage-volume-01:/var/lib/garage:Z \
  docker.io/dxflrs/garage:99923a87182ae9252b421dda65c935b9c06b1444 \
  /garage server

echo "Garage server container 'temporal-kyc-garage-01' started."
