#!/usr/bin/env bash
# Reclaim Docker disk space without deleting named volumes.
#
# Removes stopped one-shot containers, dangling images, unused networks and
# build cache older than BUILD_CACHE_TTL (default: 7 days). Recent build cache
# is kept so day-to-day rebuilds stay fast.
#
# Named volumes (postgres_data, grafana_data, ingest_cache) are never pruned.
# To also remove unused images, pass --images (e.g. old tags after a rebuild).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

CACHE_TTL="${BUILD_CACHE_TTL:-168h}"
PRUNE_IMAGES=0
if [[ "${1:-}" == "--images" ]]; then
    PRUNE_IMAGES=1
fi

echo "==> Docker usage before"
docker system df

echo "==> Removing stopped containers"
docker container prune -f

echo "==> Removing dangling images"
docker image prune -f

if [[ "$PRUNE_IMAGES" == "1" ]]; then
    echo "==> Removing unused images (not referenced by a container)"
    docker image prune -a -f
fi

echo "==> Removing build cache older than ${CACHE_TTL}"
docker builder prune -f --filter "until=${CACHE_TTL}"

echo "==> Removing unused networks"
docker network prune -f

echo "==> Docker usage after"
docker system df
