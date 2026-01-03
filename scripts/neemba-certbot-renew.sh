#!/usr/bin/env bash
set -euo pipefail

LETSENCRYPT_DIR="/srv/neemba/infra/letsencrypt"
WEBROOT_DIR="/srv/neemba/infra/nginx/html"
NGINX_CONTAINER="nginx"

docker run --rm \
  -v "${LETSENCRYPT_DIR}:/etc/letsencrypt" \
  -v "${WEBROOT_DIR}:/var/www/html" \
  certbot/certbot renew --webroot -w /var/www/html --quiet

docker exec "${NGINX_CONTAINER}" nginx -s reload >/dev/null 2>&1 || \
  docker restart "${NGINX_CONTAINER}" >/dev/null 2>&1
