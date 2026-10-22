#!/usr/bin/env bash

# Create the volume if it doesn't already exist
podman volume exists pgvolume-01 || podman volume create pgvolume-01

podman run -d \
  --name temporal-kyc-postgres-01 \
  -e POSTGRES_USER=pguser \
  -e POSTGRES_PASSWORD=pguserpassword \
  -e POSTGRES_DB=db-kyc \
  -v pgvolume-01:/var/lib/postgresql:Z \
  -p 5432:5432 \
  postgres:latest
