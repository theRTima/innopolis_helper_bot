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
PRUNE_ALL_CACHE=0
for arg in "$@"; do
    case "$arg" in
        --images) PRUNE_IMAGES=1 ;;
        --all-cache) PRUNE_ALL_CACHE=1 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

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
if [[ "$PRUNE_ALL_CACHE" == "1" ]]; then
    docker builder prune -a -f
else
    docker builder prune -f --filter "until=${CACHE_TTL}"
fi

echo "==> Removing unused networks"
docker network prune -f

echo "==> Docker usage after"
docker system df
