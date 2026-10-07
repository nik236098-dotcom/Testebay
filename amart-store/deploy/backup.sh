#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
umask 077
destination="backups/$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$destination"
snapshot=$(docker compose exec -T app node scripts/backup.mjs)
docker compose cp "app:$snapshot/." "$destination/"
cp .env "$destination/.env"
tar -czf "$destination.tar.gz" -C "$destination" .
echo "Резервная копия: $destination.tar.gz"
