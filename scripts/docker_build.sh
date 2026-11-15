#!/usr/bin/env bash
# Build all default + ingest images and immediately reclaim old Docker data.
# Extra arguments are forwarded to `docker compose build`.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

docker compose --profile ingest build "$@"
./scripts/docker_cleanup.sh
