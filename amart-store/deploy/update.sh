#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
bash deploy/backup.sh
git pull --ff-only origin amart-store-server
docker compose up -d --build --wait --wait-timeout 180
echo 'Обновление установлено. Товары, заказы и фотографии сохранены.'
