#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  if [ "$(id -u)" -ne 0 ]; then
    echo 'Для первой установки запустите: sudo bash deploy/install.sh'; exit 1
  fi
  if ! command -v curl >/dev/null; then apt-get update && apt-get install -y curl ca-certificates; fi
  installer_file=$(mktemp)
  trap 'rm -f "$installer_file"' EXIT
  curl --fail --silent --show-error --location https://get.docker.com --output "$installer_file"
  sh "$installer_file"
fi
if [ ! -f .env ]; then
  site_address="${1:-}"
  if [ -z "$site_address" ]; then read -r -p 'Домен магазина (без https://), либо http://IP для проверки: ' site_address; fi
  if [[ "$site_address" =~ ^https?:// ]]; then public_origin="$site_address"; else public_origin="https://$site_address"; fi
  if ! [[ "$site_address" =~ ^(https?://)?[a-zA-Z0-9][a-zA-Z0-9.-]*(:[0-9]{1,5})?$ ]]; then
    echo 'Введите домен латиницей или IP без пути и пробелов.'; exit 1
  fi
  admin_password=$(od -An -N20 -tx1 /dev/urandom | tr -d ' \n')
  telegram_key=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  umask 077
  printf 'SITE_ADDRESS=%s\nPUBLIC_ORIGIN=%s\nADMIN_PASSWORD=%s\nTELEGRAM_CONFIG_KEY=%s\n' "$site_address" "$public_origin" "$admin_password" "$telegram_key" > .env
fi
docker compose up -d --build --wait --wait-timeout 180
echo 'Магазин запущен. Админка: адрес магазина + /admin'
echo 'Пароль администратора (сохраните его):'
sed -n 's/^ADMIN_PASSWORD=//p' .env
echo 'Просмотр логов: docker compose logs --tail=100 app'
